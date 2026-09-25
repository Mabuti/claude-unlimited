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
    ("GET", "/v1/responses", False),
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
    for dropped in ("host", "accept-encoding", "connection", "cookie", "proxy-authorization"):
        assert dropped not in sent_headers, dropped
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


@pytest.mark.parametrize("path", ["/v1/responses/../admin", "/v1/responses/a.b", "/v1/responses/%2e%2e"])
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


def test_every_codex_profile_exhausted_is_a_429_with_retry_after(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth(priority=0), _codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_quota_429()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)

    assert result.status == 429
    assert result.error == gateway_module.OPENAI_ERROR_CODEX_EXHAUSTED
    assert 0 < int(result.headers["retry-after"]) <= 600
    assert "available again at" in result.error_detail
    assert len(up.requests) == 2
    assert gw.serving_now_ids() == set()

    # And the next request makes no upstream attempt at all.
    again = gw.handle("POST", "/v1/responses", dict(CODEX_HEADERS), BODY)
    assert again.status == 429
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
