"""OpenAI-shaped ingress: the Codex CLI's POST /v1/responses, served by
codex-kind Profiles only and relayed untranslated.

Gateway and openai_bridge run for real here; only the HTTPS connection is
faked, so every test sees exactly what would go on the wire."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import activity, usage_history
from claude_unlimited.config import Pool, Profile, Settings, save_pool
from claude_unlimited.gateway import Gateway, is_openai_ingress
from claude_unlimited.openai_credential import StoredOpenAICredential, encode
from claude_unlimited.router import ProfileState
from claude_unlimited.upstream import UpstreamResponse


# ---- fakes -----------------------------------------------------------------

class FakeHTTPResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body
        self._pos = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=None):
        if n is None:
            chunk = self._body[self._pos:]
            self._pos = len(self._body)
            return chunk
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeHTTPSConnection:
    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.requests = []
        self.closed = False
        self._response = None

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})

    def getresponse(self):
        return self._response

    def close(self):
        self.closed = True


class Upstream:
    """Scripted backend: one queued response per connection, keyed by the
    credential's access token so a test can tell which account was asked."""

    def __init__(self, monkeypatch, script):
        self.script = {token: list(responses) for token, responses in script.items()}
        self.conns = []

        def factory(host, port, timeout=None):
            conn = FakeHTTPSConnection(host, port, timeout)
            original_request = conn.request

            def request(method, path, body=None, headers=None):
                original_request(method, path, body, headers)
                token = headers["Authorization"].split(" ", 1)[1]
                conn._response = self.script[token].pop(0)
                if isinstance(conn._response, BaseException):
                    raise conn._response  # a network failure on this account

            conn.request = request
            self.conns.append(conn)
            return conn

        monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)

    @property
    def requests(self):
        return [r for c in self.conns for r in c.requests]


def _no_transport(req):
    raise AssertionError("the Anthropic transport must not be used for /v1/responses")


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


def _codex_cred(token, account="acct-1"):
    return encode(StoredOpenAICredential(access_token=token, refresh_token=None, account_id=account, id_token=None))


@pytest.fixture
def pool_env(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore({
        "c1": _codex_cred("tok-c1", "acct-1"),
        "c2": _codex_cred("tok-c2", "acct-2"),
        "o": "oauth-token",
        "k": "sk-ant-api",
    }))
    return tmp_path


def _codex(pid="c1", name=None, priority=1, **kw):
    return Profile(id=pid, name=name or pid.upper(), kind="codex", auth_mode="chatgpt_subscription",
                   priority=priority, automatic=True, enabled=True, **kw)


def _oauth(pid="o", priority=1):
    return Profile(id=pid, name=pid.upper(), kind="oauth", priority=priority, automatic=True, enabled=True)


def _api(pid="k", priority=1):
    return Profile(id=pid, name=pid.upper(), kind="api", auth_mode="api_key", priority=priority,
                   automatic=True, enabled=True, base_url="https://api.anthropic.com")


CODEX_HEADERS = {
    "x-codex-beta-features": "remote_compaction_v2",
    "x-codex-window-id": "sess:0",
    "x-codex-turn-metadata": '{"turn_id":"t1"}',
    "x-openai-internal-codex-responses-lite": "true",
    "x-client-request-id": "sess",
    "session-id": "sess",
    "thread-id": "sess",
    "accept": "text/event-stream",
    "content-type": "application/json",
    "authorization": "Bearer cu-placeholder",
    "originator": "codex_exec",
    "user-agent": "codex_exec/0.144.0 (Ubuntu; x86_64)",
    "host": "127.0.0.1:4317",
    "content-length": "999",
    "accept-encoding": "gzip, br",
    "connection": "keep-alive",
    "cookie": "session=abc",
    "proxy-authorization": "Basic Zm9v",
}

# Deliberately odd formatting: key order, whitespace and unicode must all
# survive untouched, which a parse/re-serialise would not.
BODY = ('{"model":"gpt-5.6-sol",  "input":[{"type":"message","role":"user","content":"h\\u00e9llo"}],'
        '"stream":true,"store":false,"include":["reasoning.encrypted_content"]}').encode()

SSE = (b"event: response.created\r\ndata: {\"type\":\"response.created\"}\r\n\r\n"
       b": keepalive comment\n\n"
       b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"pong\"}\n\n"
       b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"model\":\"gpt-5.6-sol\","
       b"\"usage\":{\"input_tokens\":1000,\"input_tokens_details\":{\"cached_tokens\":600},"
       b"\"output_tokens\":42,\"total_tokens\":1042}}}\n\n")

OK_HEADERS = {
    "Content-Type": "text/event-stream",
    "x-codex-primary-used-percent": "12",
    "x-codex-primary-window-minutes": "300",
    "x-codex-plan-type": "plus",
    "CF-RAY": "abcd1234",
    "cf-cache-status": "DYNAMIC",
    "Set-Cookie": "__oailb=secret; HttpOnly",
    "Server": "cloudflare",
    "alt-svc": 'h3=":443"',
    "Transfer-Encoding": "chunked",
}


def _ok(body=SSE, headers=None):
    return FakeHTTPResponse(200, dict(headers or OK_HEADERS), body)


def _quota_429():
    reset_at = int((datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp())
    return FakeHTTPResponse(429, {"x-codex-primary-used-percent": "100", "x-codex-primary-window-minutes": "300",
                                  "x-codex-primary-reset-at": str(reset_at), "content-type": "application/json"},
                            b'{"error":{"type":"usage_limit_reached","message":"The usage limit has been reached"}}')


def _drain(result):
    return b"".join(result.body_chunks)


# ---- detection ---------------------------------------------------------------

@pytest.mark.parametrize("method,path,expected", [
    ("POST", "/v1/responses", True),
    ("POST", "/v1/responses/compact", True),
    ("POST", "/v1/responses?x=1", True),
    ("POST", "/v1/responses/", True),
    ("GET", "/v1/responses", True),       # claimed so it is answered 405 locally, never forwarded
    ("POST", "/v1/responsesx", False),
    ("POST", "/v1/messages", False),
    ("POST", "/backend-api/codex/responses", False),
])
def test_ingress_is_detected_by_path_alone(method, path, expected):
    assert is_openai_ingress(method, path) is expected


# ---- the happy path -----------------------------------------------------------

def test_request_is_relayed_verbatim_both_ways(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex()]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 200
    assert result.profile_id == "c1"
    assert _drain(result) == SSE                      # byte-identical, CRLF and comments included
    [conn] = up.conns
    assert conn.host == "chatgpt.com"
    [sent] = conn.requests
    assert sent["method"] == "POST"
    assert sent["path"] == "/backend-api/codex/responses"
    assert sent["body"] == BODY                        # byte-identical
    sent_headers = {k.lower(): v for k, v in sent["headers"].items()}
    assert sent_headers["authorization"] == "Bearer tok-c1"
    assert sent_headers["chatgpt-account-id"] == "acct-1"
    assert sent_headers["content-length"] == str(len(BODY))
    for kept in ("x-codex-beta-features", "x-codex-window-id", "x-codex-turn-metadata",
                 "x-openai-internal-codex-responses-lite", "x-client-request-id", "session-id",
                 "thread-id", "originator", "user-agent", "accept", "content-type"):
        assert sent_headers[kept] == CODEX_HEADERS[kept], kept
    for dropped in ("host", "connection", "cookie", "proxy-authorization"):
        assert dropped not in sent_headers, dropped
    assert sent_headers["accept-encoding"] == "identity"   # set by the daemon, never the client's
    assert len([k for k in sent["headers"] if k.lower() == "authorization"]) == 1
    assert conn.closed

    client = {k.lower(): v for k, v in result.headers.items()}
    assert client["x-codex-primary-used-percent"] == "12"
    assert client["x-codex-plan-type"] == "plus"
    assert client["content-type"] == "text/event-stream"
    for dropped in ("cf-ray", "cf-cache-status", "set-cookie", "server", "alt-svc", "transfer-encoding"):
        assert dropped not in client, dropped


def test_compact_suffix_routes_to_the_backend_compact_endpoint(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex()]))
    compact = b'{"output":[{"type":"compaction","encrypted_content":"x"}]}'
    up = Upstream(monkeypatch, {"tok-c1": [_ok(compact, {"content-type": "application/json"})]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses/compact", dict(CODEX_HEADERS), BODY)

    assert result.status == 200
    assert _drain(result) == compact
    assert up.requests[0]["path"] == "/backend-api/codex/responses/compact"


def test_api_key_codex_profile_uses_its_base_url(pool_env, monkeypatch):
    save_pool(Pool(profiles=[Profile(id="c1", name="C1", kind="codex", auth_mode="api_key", priority=1,
                                     automatic=True, enabled=True, base_url="https://api.openai.com/v1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses/compact", dict(CODEX_HEADERS), BODY)

    assert result.status == 200
    assert up.conns[0].host == "api.openai.com"
    assert up.requests[0]["path"] == "/v1/responses/compact"
    assert "chatgpt-account-id" not in {k.lower() for k in up.requests[0]["headers"]}


@pytest.mark.parametrize("path", ["/v1/responses/../admin", "/v1/responses/a.b", "/v1/responses/%2e%2e",
                                  "/v1/responses/input_items", "/v1/responses/resp_123", "/v1/responses//",
                                  "/v1/responses/compact/extra", "/v1/responses/compact//"])
def test_unsafe_suffix_is_refused_locally(pool_env, monkeypatch, path):
    save_pool(Pool(profiles=[_codex()]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", path, dict(CODEX_HEADERS), BODY)

    assert result.status == 400
    assert result.error == gateway_module.OPENAI_ERROR_BAD_REQUEST
    assert up.conns == []


def test_oversized_body_is_refused_before_any_attempt(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex()]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    monkeypatch.setattr(gateway_module, "_OPENAI_INGRESS_MAX_BODY_BYTES", 10)
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 400
    assert up.conns == []


def test_eco_and_speech_settings_never_touch_the_body(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex()], settings=Settings(eco_tier="aggressive", speech_level="ultra")))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 200
    assert up.requests[0]["body"] == BODY


def test_no_translator_or_model_mapper_is_ever_called(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex()]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})

    def boom(*a, **kw):
        raise AssertionError("translation must never run on OpenAI ingress")

    monkeypatch.setattr(bridge_module, "map_model", boom)
    monkeypatch.setattr(bridge_module, "run", boom)
    monkeypatch.setattr(gateway_module.openai_models, "claude_effort_for", boom)
    gw = Gateway(transport=_no_transport)

    assert _drain(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)) == SSE


# ---- codex-only, and loud about it ------------------------------------------------

def _assert_refused_without_attempt(result, up, status=400):
    assert result.status == status
    assert result.body_chunks is None
    assert result.error is not None and result.error.startswith("openai_")
    assert result.error_detail
    assert up.conns == []


@pytest.mark.parametrize("profiles", [[_oauth()], [_api()], [_oauth(), _api()]])
def test_only_claude_profiles_is_a_400_and_never_an_upstream_attempt(pool_env, monkeypatch, profiles):
    save_pool(Pool(profiles=profiles))
    up = Upstream(monkeypatch, {})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    _assert_refused_without_attempt(result, up)
    assert result.error == gateway_module.OPENAI_ERROR_NO_CODEX_PROFILE
    assert "cu add-codex-account" in result.error_detail


def test_empty_pool_is_the_same_400(pool_env, monkeypatch):
    save_pool(Pool(profiles=[]))
    up = Upstream(monkeypatch, {})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    _assert_refused_without_attempt(result, up)
    assert result.error == gateway_module.OPENAI_ERROR_NO_CODEX_PROFILE


def test_disabled_codex_profile_counts_as_none(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth(), Profile(id="c1", name="C1", kind="codex", auth_mode="chatgpt_subscription",
                                               priority=1, automatic=True, enabled=False)]))
    up = Upstream(monkeypatch, {})
    gw = Gateway(transport=_no_transport)

    _assert_refused_without_attempt(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY), up)


def test_pinned_to_a_claude_profile_is_a_400_with_zero_attempts(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth(), _codex()]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY, forced_profile_id="o")

    _assert_refused_without_attempt(result, up)
    assert "O" in result.error_detail


def test_pinned_codex_profile_that_needs_reauth_is_refused_not_substituted(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    gw.runtime_snapshot()
    assert gw.wait_for_credential_checks()
    gw._runtime["c1"] = gateway_module._replace_runtime(gw._runtime["c1"], state=ProfileState.AUTH_INVALID)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY, forced_profile_id="c1")

    _assert_refused_without_attempt(result, up)
    assert "re-authentication" in result.error_detail


def test_pinned_codex_profile_out_of_quota_relays_the_real_429(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY, forced_profile_id="c1")

    assert result.status == 429
    assert result.error is None
    assert b"usage_limit_reached" in _drain(result)
    assert len(up.requests) == 1                     # never substituted
    assert gw._runtime["c1"].state == ProfileState.EXHAUSTED


# ---- rotation ----------------------------------------------------------------------

@pytest.mark.parametrize("prior_pointer", [None, "o"])
def test_quota_429_rotates_to_the_next_codex_profile_and_never_moves_the_shared_pointer(
        pool_env, monkeypatch, prior_pointer):
    save_pool(Pool(profiles=[_oauth(priority=0), _codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    gw._current_profile_id = prior_pointer
    announced = []
    monkeypatch.setattr(gw, "_announce_rotation", lambda *a: announced.append(a))

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 200
    assert result.profile_id == "c2"
    assert _drain(result) == SSE
    assert [c.requests[0]["headers"]["Authorization"] for c in up.conns] == ["Bearer tok-c1", "Bearer tok-c2"]
    assert all(c.closed for c in up.conns)
    assert gw._runtime["c1"].state == ProfileState.EXHAUSTED
    assert gw._current_profile_id == prior_pointer
    assert announced == []
    assert gw._openai_ingress_profile_id == "c2"
    assert gw.serving_now_ids() == set()             # no in-flight slot leaked


def test_auth_invalid_rotates_when_another_codex_profile_exists(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(401, {}, b'{"error":{"message":"bad token"}}')],
                                "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 200 and result.profile_id == "c2"
    assert gw._runtime["c1"].state == ProfileState.AUTH_INVALID
    assert len(up.requests) == 2


def test_auth_invalid_with_nothing_else_relays_the_real_401(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(401, {}, b'{"error":{"message":"bad token"}}')]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 401
    assert _drain(result) == b'{"error":{"message":"bad token"}}'


def test_every_codex_profile_exhausted_relays_the_last_real_429_then_answers_locally(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth(priority=0), _codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_quota_429()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    # An upstream answered: its own 429 is the truth, not a local refusal.
    assert result.status == 429
    assert result.error is None
    assert result.profile_id == "c2"
    assert b"usage_limit_reached" in _drain(result)
    assert len(up.requests) == 2
    assert all(c.closed for c in up.conns)
    assert gw.serving_now_ids() == set()

    # The next request makes no upstream attempt at all: a local 429.
    again = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)
    assert again.status == 429
    assert again.error == gateway_module.OPENAI_ERROR_CODEX_EXHAUSTED
    assert 0 < int(again.headers["retry-after"]) <= 600
    assert "available again at" in again.error_detail
    assert len(up.requests) == 2


def test_network_failure_rotates_then_502_when_nothing_is_left(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))

    def factory(host, port, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 502
    assert result.error == gateway_module.OPENAI_ERROR_UPSTREAM_UNREACHABLE
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN
    assert gw.serving_now_ids() == set()


def test_mixed_pool_codex_ingress_and_claude_ingress_keep_their_own_accounts(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth(priority=1), _codex("c1", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    anthropic_calls = []

    def transport(req):
        anthropic_calls.append(req)

        class Conn:
            def close(self):
                pass

        return UpstreamResponse(status=200, headers={"content-type": "application/json"},
                                body_chunks=iter([b'{"ok":true}']), connection=Conn())

    gw = Gateway(transport=transport)

    codex = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)
    assert codex.profile_id == "c1"
    _drain(codex)
    assert gw._current_profile_id is None

    claude = gw.handle("POST", "/v1/messages", {"content-type": "application/json"}, b'{"model":"claude-sonnet-5"}')
    assert claude.profile_id == "o"
    assert len(anthropic_calls) == 1
    assert anthropic_calls[0].headers["Authorization"] == "Bearer oauth-token"
    assert len(up.requests) == 1
    assert gw._current_profile_id == "o"

    # ...and Codex stays on its own account afterwards.
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    assert gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY).profile_id == "c1"
    assert gw._current_profile_id == "o"


def test_ingress_pointer_change_writes_one_activity_line(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1", name="Work GPT")]))
    Upstream(monkeypatch, {"tok-c1": [_ok(), _ok()]})
    lines = []
    monkeypatch.setattr(activity, "record", lambda category, text, meta=None: lines.append((category, text)))
    gw = Gateway(transport=_no_transport)

    _drain(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY))
    _drain(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY))

    assert lines.count(("rotation", "Codex CLI now served by Work GPT")) == 1
    assert not any(text.startswith("Rotated") for _c, text in lines)


# ---- encrypted reasoning fallback --------------------------------------------------

REASONING_BODY = json.dumps({
    "model": "gpt-5.6-sol",
    "input": [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-from-another-account"},
        {"type": "message", "role": "assistant", "content": "hello"},
        {"type": "message", "role": "user", "content": "again"},
    ],
    "stream": True, "store": False,
}).encode()


def test_encrypted_reasoning_refusal_is_retried_once_without_it(pool_env, monkeypatch, capsys):
    save_pool(Pool(profiles=[_codex("c1")]))
    refusal = FakeHTTPResponse(400, {"content-type": "application/json"}, json.dumps({"error": {
        "message": "The encrypted content for item rs_1 could not be verified.",
        "type": "invalid_request_error", "code": "invalid_encrypted_content"}}).encode())
    up = Upstream(monkeypatch, {"tok-c1": [refusal, _ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), REASONING_BODY)

    assert result.status == 200
    assert len(up.requests) == 2
    assert up.requests[0]["body"] == REASONING_BODY
    retried = json.loads(up.requests[1]["body"])
    assert [item["type"] for item in retried["input"]] == ["message", "message", "message"]
    assert "encrypted_content" not in up.requests[1]["body"].decode()
    assert up.requests[1]["headers"]["Content-Length"] == str(len(up.requests[1]["body"]))
    assert "encrypted reasoning" in capsys.readouterr().err


def test_a_second_encrypted_refusal_is_not_retried_again(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    refusal = json.dumps({"error": {"message": "Encrypted content could not be decrypted or parsed."}}).encode()
    up = Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(400, {}, refusal), FakeHTTPResponse(400, {}, refusal)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), REASONING_BODY)

    assert result.status == 400
    assert len(up.requests) == 2
    assert _drain(result) == refusal


def test_a_different_400_is_not_retried(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    other = json.dumps({"error": {"message": "The 'foo' model is not supported", "type": "invalid_request_error",
                                  "param": "model"}}).encode()
    up = Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(400, {}, other)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), REASONING_BODY)

    assert result.status == 400
    assert len(up.requests) == 1
    assert _drain(result) == other


def test_encrypted_refusal_without_encrypted_input_is_not_retried(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    refusal = json.dumps({"error": {"message": "Encrypted content could not be decrypted."}}).encode()
    up = Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(400, {}, refusal)]})
    gw = Gateway(transport=_no_transport)

    assert gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY).status == 400
    assert len(up.requests) == 1


# ---- x-codex-turn-state stickiness -------------------------------------------------

def test_turn_state_issued_by_another_profile_is_dropped(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    issued = dict(OK_HEADERS, **{"x-codex-turn-state": "ts-from-c1"})
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=issued), _quota_429()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)
    assert first.headers["x-codex-turn-state"] == "ts-from-c1"
    _drain(first)

    second = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS, **{"x-codex-turn-state": "ts-from-c1"}), BODY)
    assert second.profile_id == "c2"
    to_c1, to_c2 = up.requests[1], up.requests[2]
    assert to_c1["headers"]["x-codex-turn-state"] == "ts-from-c1"   # same issuer: kept
    assert "x-codex-turn-state" not in {k.lower() for k in to_c2["headers"]}


def test_turn_state_memory_is_bounded(pool_env, monkeypatch):
    monkeypatch.setattr(gateway_module, "_TURN_STATE_MEMORY", 3)
    gw = Gateway(transport=_no_transport)
    for i in range(5):
        gw._remember_turn_state_issuer(f"ts{i}", "c1")
    assert list(gw._turn_state_issuers) == ["ts2", "ts3", "ts4"]


# ---- usage --------------------------------------------------------------------------

def test_usage_is_recorded_from_the_responses_stream(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    _drain(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY))

    [event] = usage_history.list_events()
    assert event.profile_id == "c1"
    assert event.model == "gpt-5.6-sol"
    assert event.input_tokens == 400
    assert event.cache_read_input_tokens == 600
    assert event.cache_creation_input_tokens == 0
    assert event.output_tokens == 42
    assert event.quota_5h_percent == 12.0


# ---- Anthropic ingress is untouched -------------------------------------------------

def test_messages_on_a_codex_profile_still_goes_through_the_translating_bridge(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    calls = []

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        calls.append(body)
        return bridge_module.OpenAIBridgeResult(status=200, headers={}, body_chunks=iter([b"x"]))

    monkeypatch.setattr(bridge_module, "run", fake_run)
    monkeypatch.setattr(bridge_module, "run_passthrough",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no passthrough for /v1/messages")))
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/messages", {}, b'{"stream":true}')

    assert result.status == 200 and calls == [b'{"stream":true}']
    assert gw._current_profile_id == "c1"


@pytest.mark.parametrize("profile", [_oauth("o"), _api("k")])
def test_messages_on_claude_profiles_is_unchanged(pool_env, monkeypatch, profile):
    save_pool(Pool(profiles=[profile]))
    monkeypatch.setattr(bridge_module, "run_passthrough",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("no passthrough for /v1/messages")))
    sent = []

    def transport(req):
        sent.append(req)

        class Conn:
            def close(self):
                pass

        return UpstreamResponse(status=200, headers={"content-type": "application/json"},
                                body_chunks=iter([b'{"ok":true}']), connection=Conn())

    gw = Gateway(transport=transport)
    body = b'{"model":"claude-sonnet-5","messages":[]}'
    result = gw.handle("POST", "/v1/messages", {"content-type": "application/json"}, body)

    assert result.status == 200
    assert _drain(result) == b'{"ok":true}'
    assert sent[0].url.endswith("/v1/messages")
    assert gw._current_profile_id == profile.id


# ==== review fixes ===================================================================

def _session(sid, **extra):
    headers = dict(CODEX_HEADERS)
    headers["session-id"] = sid
    headers["thread-id"] = sid
    headers.update(extra)
    return headers


def _set_state(gw, pid, state, **changes):
    gw.runtime_snapshot()
    assert gw.wait_for_credential_checks()
    gw._runtime[pid] = gateway_module._replace_runtime(gw._runtime[pid], state=state, **changes)


def _tokens(up):
    return [r["headers"]["Authorization"].split(" ", 1)[1] for r in up.requests]


def _in(minutes):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


# A successful stream whose headers carry no usage: classify() says Unknown,
# so a state a test set by hand survives the request.
def _ok_no_usage():
    return _ok(headers={"Content-Type": "text/event-stream"})


def _status(status, headers=None, body=b'{"error":{"message":"upstream said no"}}'):
    return FakeHTTPResponse(status, dict(headers or {"content-type": "application/json"}), body)


# ---- F1: session affinity ------------------------------------------------------------

def test_a_session_stays_on_its_account_when_that_account_starts_draining(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(), _ok_no_usage()], "tok-c2": [_ok_no_usage()]})
    gw = Gateway(transport=_no_transport)

    assert _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY)) == SSE
    _set_state(gw, "c1", ProfileState.DRAINING, last_usage_percent=99.0)

    kept = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    fresh = gw.handle("POST", "/v1/responses", _session("s2"), BODY)
    _drain(kept)
    _drain(fresh)

    assert kept.profile_id == "c1"      # an existing session keeps its account
    assert fresh.profile_id == "c2"     # a new one avoids the draining account
    assert _tokens(up) == ["tok-c1", "tok-c1", "tok-c2"]
    assert gw._openai_sessions == {"s1": "c1", "s2": "c2"}


@pytest.mark.parametrize("state,changes", [
    (ProfileState.EXHAUSTED, {"resets_at": _in(30)}),
    (ProfileState.COOLDOWN, {"cooldown_until": _in(30)}),
    (ProfileState.AUTH_INVALID, {}),
])
def test_a_session_moves_when_its_account_cannot_serve(pool_env, monkeypatch, state, changes):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))
    _set_state(gw, "c1", state, **changes)

    moved = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert moved.profile_id == "c2"
    _drain(moved)
    assert gw._openai_sessions["s1"] == "c2"
    assert _tokens(up) == ["tok-c1", "tok-c2"]


def test_a_session_moves_when_its_account_is_disabled_or_removed(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    Upstream(monkeypatch, {"tok-c1": [_ok()], "tok-c2": [_ok(), _ok()]})
    gw = Gateway(transport=_no_transport)
    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))

    save_pool(Pool(profiles=[Profile(id="c1", name="C1", kind="codex", auth_mode="chatgpt_subscription",
                                     priority=1, automatic=True, enabled=False), _codex("c2", priority=2)]))
    assert gw.handle("POST", "/v1/responses", _session("s1"), BODY).profile_id == "c2"

    gw._openai_sessions["s1"] = "gone"
    assert gw.handle("POST", "/v1/responses", _session("s1"), BODY).profile_id == "c2"


def test_a_session_keeps_its_account_even_when_another_is_preferred(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    gw._openai_sessions["s1"] = "c2"

    assert gw.handle("POST", "/v1/responses", _session("s1"), BODY).profile_id == "c2"
    assert _tokens(up) == ["tok-c2"]


def test_a_pinned_request_neither_reads_nor_writes_the_session_map(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    gw._openai_sessions["s1"] = "c2"

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1")

    assert result.profile_id == "c1"
    assert _tokens(up) == ["tok-c1"]
    assert gw._openai_sessions == {"s1": "c2"}


def test_thread_id_keys_the_session_when_session_id_is_absent(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    headers = {k: v for k, v in CODEX_HEADERS.items() if k != "session-id"}
    headers["thread-id"] = "t-1"

    _drain(gw.handle("POST", "/v1/responses", headers, BODY))

    assert gw._openai_sessions == {"t-1": "c1"}


def test_the_session_map_is_a_bounded_lru(pool_env, monkeypatch):
    monkeypatch.setattr(gateway_module, "_OPENAI_SESSION_MEMORY", 3)
    gw = Gateway(transport=_no_transport)
    for key in ("s0", "s1", "s2"):
        gw._remember_openai_session(key, "c1")
    gw._remember_openai_session("s0", "c1")   # touched: now the newest
    gw._remember_openai_session("s3", "c1")

    assert list(gw._openai_sessions) == ["s2", "s0", "s3"]


# ---- F2: account move hygiene ------------------------------------------------------

MOVE_BODY = json.dumps({
    "model": "gpt-5.6-sol",
    "input": [
        {"type": "message", "role": "user", "content": "hi"},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "gAAAA-c1-only"},
        {"type": "reasoning", "id": "rs_2", "summary": [{"type": "summary_text", "text": "thought"}],
         "encrypted_content": "gAAAA-c1-summary"},
        {"type": "compaction", "encrypted_content": "gAAAA-compacted"},
        {"type": "message", "role": "user", "content": "again"},
    ],
    "stream": True, "store": False,
}).encode()


def test_a_moved_session_leaves_turn_state_and_encrypted_reasoning_behind(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    issued = dict(OK_HEADERS, **{"x-codex-turn-state": "ts-c1"})
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=issued)], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))
    _set_state(gw, "c1", ProfileState.EXHAUSTED, resets_at=_in(30))

    moved = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY)

    assert moved.profile_id == "c2"
    _drain(moved)
    assert len(up.requests) == 2                      # stripped BEFORE the first send, no 400 round trip
    sent = up.requests[1]
    assert "x-codex-turn-state" not in {k.lower() for k in sent["headers"]}
    items = json.loads(sent["body"])["input"]
    assert [item["type"] for item in items] == ["message", "reasoning", "compaction", "message"]
    assert items[1] == {"type": "reasoning", "id": "rs_2", "summary": [{"type": "summary_text", "text": "thought"}]}
    assert items[2] == {"type": "compaction", "encrypted_content": "gAAAA-compacted"}   # kept
    assert sent["headers"]["Content-Length"] == str(len(sent["body"]))


def test_a_session_on_its_own_account_goes_out_byte_identical(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    issued = dict(OK_HEADERS, **{"x-codex-turn-state": "ts-c1"})
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=issued), _ok()]})
    gw = Gateway(transport=_no_transport)
    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))

    _drain(gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY))

    sent = up.requests[1]
    assert sent["body"] == MOVE_BODY
    assert sent["headers"]["x-codex-turn-state"] == "ts-c1"


def test_the_body_is_untouched_when_a_move_has_nothing_to_strip(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    gw._openai_sessions["s1"] = "c1"
    _set_state(gw, "c1", ProfileState.AUTH_INVALID)

    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))

    assert up.requests[0]["body"] == BODY             # never re-serialised


def test_the_single_retry_also_drops_compaction_and_turn_state(pool_env, monkeypatch, capsys):
    save_pool(Pool(profiles=[_codex("c1")]))
    refusal = _status(400, body=json.dumps({"error": {
        "message": "The encrypted content for item cmp_1 could not be verified.",
        "type": "invalid_request_error", "code": "invalid_encrypted_content"}}).encode())
    up = Upstream(monkeypatch, {"tok-c1": [refusal, _ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-unknown"}), MOVE_BODY)

    assert result.status == 200
    first, retry = up.requests
    assert first["body"] == MOVE_BODY
    assert first["headers"]["x-codex-turn-state"] == "ts-unknown"
    assert [item["type"] for item in json.loads(retry["body"])["input"]] == ["message", "message"]
    assert "x-codex-turn-state" not in {k.lower() for k in retry["headers"]}
    assert "compacted history" in capsys.readouterr().err


def test_without_foreign_reasoning_returns_none_when_there_is_nothing_to_strip():
    assert bridge_module.without_foreign_reasoning(BODY) is None
    assert bridge_module.without_foreign_reasoning(b"not json") is None


# ---- F3: in-request failover ---------------------------------------------------------

@pytest.mark.parametrize("first", [
    lambda: _status(500), lambda: _status(502), lambda: _status(503), lambda: _status(504),
    lambda: _status(529),
    lambda: _status(429, {"content-type": "application/json", "retry-after": "5"}),   # not a quota 429
    lambda: _status(429),
    lambda: OSError("connection reset"),
])
def test_a_transient_failure_moves_the_request_to_the_next_codex_account(pool_env, monkeypatch, first):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [first()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 200
    assert result.profile_id == "c2"
    assert _drain(result) == SSE
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    assert all(c.closed for c in up.conns)
    assert gw.serving_now_ids() == set()


def test_failover_is_bounded_and_returns_the_last_real_answer(pool_env, monkeypatch):
    ids = [f"c{i}" for i in range(1, 7)]
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore(
        {pid: _codex_cred(f"tok-{pid}", f"acct-{pid}") for pid in ids}))
    save_pool(Pool(profiles=[_codex(pid, priority=i) for i, pid in enumerate(ids, start=1)]))
    up = Upstream(monkeypatch, {f"tok-{pid}": [_status(503, body=f"busy-{pid}".encode())] for pid in ids})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert len(up.requests) == gateway_module.MAX_ROTATION_ATTEMPTS
    last = f"c{gateway_module.MAX_ROTATION_ATTEMPTS}"
    assert result.status == 503 and result.error is None
    assert result.profile_id == last
    assert _drain(result) == f"busy-{last}".encode()
    assert all(c.closed for c in up.conns)
    assert gw.serving_now_ids() == set()


# ---- F4: truthful terminal answer ----------------------------------------------------

def test_the_last_upstream_answer_wins_over_a_later_network_failure(pool_env, monkeypatch):
    # A saved 5xx (which the Codex CLI retries) is relayed; a saved 4xx after
    # a network failure is a local 502 instead (round 4, K4 — see
    # test_gateway_openai_ingress_round4.py).
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    busy = _status(503, {"content-type": "application/json", "x-codex-primary-used-percent": "40",
                         "set-cookie": "x=y"}, body=b'{"error":{"message":"upstream_overloaded"}}')
    up = Upstream(monkeypatch, {"tok-c1": [busy], "tok-c2": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503
    assert result.error is None
    assert result.profile_id == "c1"
    assert b"upstream_overloaded" in _drain(result)
    assert "set-cookie" not in {k.lower() for k in result.headers}
    assert result.headers["x-codex-primary-used-percent"] == "40"
    assert all(c.closed for c in up.conns)
    assert gw.serving_now_ids() == set()


def test_a_single_account_5xx_is_relayed_and_the_next_try_is_a_last_resort_attempt_not_a_429(pool_env,
                                                                                          monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(503, body=b"overloaded"), _status(503, body=b"still")]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert first.status == 503 and first.error is None
    assert _drain(first) == b"overloaded"

    # The account is cooling down and nothing else can serve: it gets one
    # last-resort attempt, and its real answer is relayed (never a 429).
    again = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert again.status == 503 and again.error is None
    assert _drain(again) == b"still"
    assert len(up.requests) == 2


def test_an_account_that_is_only_cooling_down_gets_one_last_resort_attempt(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok_no_usage()], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(0.5))
    _set_state(gw, "c2", ProfileState.EXHAUSTED, resets_at=_in(60))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 200 and result.error is None and result.profile_id == "c1"
    _drain(result)
    assert _tokens(up) == ["tok-c1"]            # never the EXHAUSTED one


def test_a_credential_read_failure_is_a_503_not_a_429(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {})

    class LockedStore:
        def get_token(self, profile_id):
            raise RuntimeError("keychain locked")

    monkeypatch.setattr(gateway_module, "secret_store", LockedStore())
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    pinned = gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1")

    for refused in (result, pinned):
        _assert_refused_without_attempt(refused, up, status=503)
        assert int(refused.headers["retry-after"]) >= 1
    assert result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE


# ---- F6/F7: in-flight accounting -----------------------------------------------------

def test_a_result_discarded_unread_releases_its_slot_and_closes_the_upstream(pool_env, monkeypatch):
    import claude_unlimited.daemon as daemon

    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert gw.serving_now_ids() == {"c1"}
    daemon._discard_result(result)                   # never read a byte of it

    assert gw.serving_now_ids() == set()
    assert up.conns[0].closed
    daemon._discard_result(result)                   # idempotent
    assert gw.serving_now_ids() == set()


def test_two_open_responses_on_one_account_keep_it_in_flight_until_both_finish(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_ok(), _ok()]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    second = gw.handle("POST", "/v1/responses", _session("s2"), BODY)
    _drain(first)

    assert gw.serving_now_ids() == {"c1"}             # the second is still streaming
    assert gw.seconds_since_last_activity() == 0.0
    assert not gw.is_idle(1)

    _drain(second)
    assert gw.serving_now_ids() == set()


# ---- F8: allowlists ------------------------------------------------------------------

def test_only_allowlisted_request_headers_go_upstream(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    extra = {"x-openai-foo": "1", "forwarded": "for=1.2.3.4", "via": "1.1 proxy", "x-api-key": "sk-local",
             "openai-organization": "org", "x-forwarded-for": "1.2.3.4", "chatgpt-account-id": "someone-else",
             "openai-beta": "responses=v1", "version": "0.144.0", "x-openai-subagent": "review",
             "session_id": "legacy", "x-codex-anything": "a", "x-openai-internal-codex-other": "b"}

    _drain(gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS, **extra), BODY))

    sent = {k.lower(): v for k, v in up.requests[0]["headers"].items()}
    for dropped in ("x-openai-foo", "forwarded", "via", "x-api-key", "openai-organization", "x-forwarded-for"):
        assert dropped not in sent, dropped
    assert sent["chatgpt-account-id"] == "acct-1"     # the account's own, never the client's
    for kept in ("openai-beta", "version", "x-openai-subagent", "session_id", "x-codex-anything",
                 "x-openai-internal-codex-other"):
        assert sent[kept] == extra[kept], kept


def test_only_allowlisted_response_headers_reach_the_client(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    upstream_headers = dict(OK_HEADERS, **{
        "Via": "1.1 envoy", "X-Envoy-Upstream-Service-Time": "12", "Set-Cookie2": "a=b",
        "Strict-Transport-Security": "max-age=1", "X-Request-Id": "req_1", "OpenAI-Model": "gpt-5.6-sol",
        "OpenAI-Processing-Ms": "40", "x-ratelimit-remaining-requests": "99", "Retry-After": "3"})
    Upstream(monkeypatch, {"tok-c1": [_ok(headers=upstream_headers)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert {k.lower() for k in result.headers} == {
        "content-type", "x-codex-primary-used-percent", "x-codex-primary-window-minutes", "x-codex-plan-type",
        "x-request-id", "openai-model", "openai-processing-ms", "x-ratelimit-remaining-requests", "retry-after"}


@pytest.mark.parametrize("path,backend", [("/v1/responses/", "/backend-api/codex/responses"),
                                          ("/v1/responses/compact/", "/backend-api/codex/responses/compact")])
def test_one_trailing_slash_is_ignored(pool_env, monkeypatch, path, backend):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    assert gw.handle("POST", path, dict(CODEX_HEADERS), BODY).status == 200
    assert up.requests[0]["path"] == backend
