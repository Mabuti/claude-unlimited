"""The daemon's side of OpenAI-shaped ingress (the Codex CLI's POST
/v1/responses): OpenAI-shaped errors, Anthropic-shaped ones untouched for
/v1/messages, and no keep-alive — a slow Codex request still gets the real
upstream status and headers. A real loopback server on an ephemeral port;
the provider side is always faked."""

import json
import threading
import urllib.error
import urllib.request

import pytest

import claude_unlimited.daemon as daemon
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import placeholder_token
from claude_unlimited.config import Pool, Profile, save_pool
from claude_unlimited.gateway import GatewayResult
from claude_unlimited.openai_credential import StoredOpenAICredential, encode


class FakeSecretStore:
    def __init__(self, tokens):
        self.tokens = tokens

    def get_token(self, profile_id):
        return self.tokens[profile_id]


class FakeHTTPResponse:
    def __init__(self, status, headers, body):
        self.status = status
        self._headers = headers
        self._body = body
        self._pos = 0

    def getheaders(self):
        return list(self._headers.items())

    def read(self, n=None):
        n = len(self._body) - self._pos if n is None else n
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class FakeHTTPSConnection:
    instances: list = []

    def __init__(self, host, port, timeout=None):
        self.requests = []
        self.response = None
        FakeHTTPSConnection.instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.requests.append({"path": path, "body": body, "headers": dict(headers or {})})

    def getresponse(self):
        return self.response

    def close(self):
        pass


@pytest.fixture
def server_with(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_unlimited.activity.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.activity.ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(placeholder_token, "APP_DIR", tmp_path)
    monkeypatch.setattr(placeholder_token, "TOKEN_FILE", tmp_path / "placeholder_token")
    monkeypatch.setattr("claude_unlimited.gateway.runtime_state.RUNTIME_STATE_FILE", tmp_path / "runtime_state.json")
    monkeypatch.setattr("claude_unlimited.gateway.secret_store", FakeSecretStore({
        "c": encode(StoredOpenAICredential(access_token="tok-c", refresh_token=None,
                                           account_id="acct", id_token=None)),
        "o": "oauth-token",
    }))
    started = []

    def start(profiles):
        save_pool(Pool(profiles=profiles))
        server = daemon.make_server(host="127.0.0.1", port=0)
        daemon._gateway._transport = lambda req: (_ for _ in ()).throw(AssertionError("no Anthropic call expected"))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        started.append((server, t))
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server, t in started:
        server.shutdown()
        t.join(timeout=2)
        server.server_close()


def _codex():
    return Profile(id="c", name="C", kind="codex", auth_mode="chatgpt_subscription", automatic=True, enabled=True)


def _oauth():
    return Profile(id="o", name="O", kind="oauth", automatic=True, enabled=True)


BODY = b'{"model":"gpt-5.6-sol","input":[],"stream":true,"store":false}'


def _post(base, path, body=BODY, token=None):
    token = token or placeholder_token.get_or_create()
    return urllib.request.Request(f"{base}{path}", data=body, method="POST",
                                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                           "originator": "codex_exec", "x-codex-window-id": "w:0"})


def _error(req):
    try:
        urllib.request.urlopen(req, timeout=5)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())
    raise AssertionError("expected an HTTP error")


def test_no_codex_profile_is_an_openai_shaped_400(server_with):
    base = server_with([_oauth()])

    status, _headers, payload = _error(_post(base, "/v1/responses"))

    assert status == 400
    assert set(payload) == {"error"}
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == "openai_no_codex_profile"
    assert "Codex/ChatGPT account" in payload["error"]["message"]


def test_messages_refusal_keeps_the_anthropic_envelope(server_with):
    base = server_with([])

    status, _headers, payload = _error(_post(base, "/v1/messages", body=b'{"stream": false}'))

    assert status == 503
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "overloaded_error"


def test_bad_local_token_on_responses_is_openai_shaped(server_with):
    base = server_with([_codex()])

    status, _headers, payload = _error(_post(base, "/v1/responses", token="wrong"))

    assert status == 401
    assert set(payload) == {"error"}
    assert payload["error"]["message"] == "[claude-unlimited] invalid local credential"


def test_bad_local_token_on_messages_is_unchanged(server_with):
    base = server_with([_codex()])

    status, _headers, payload = _error(_post(base, "/v1/messages", token="wrong"))

    assert status == 401
    assert payload == {"type": "error", "error": {"type": "authentication_error",
                                                  "message": "[claude-unlimited] invalid local credential"}}


def test_quota_refusal_is_a_single_shot_429_with_retry_after(server_with, monkeypatch):
    base = server_with([_codex()])
    monkeypatch.setattr(daemon._gateway, "handle", lambda *a, **kw: GatewayResult(
        status=429, headers={"retry-after": "120"}, body_chunks=None, profile_id=None,
        error="openai_codex_exhausted", error_detail="Every Codex/ChatGPT account is out of capacity."))

    status, headers, payload = _error(_post(base, "/v1/responses", body=b'{"stream": false}'))

    assert status == 429
    assert {k.lower(): v for k, v in headers.items()}["retry-after"] == "120"
    assert payload == {"error": {"message": "Every Codex/ChatGPT account is out of capacity.",
                                 "type": "usage_limit_reached", "code": "openai_codex_exhausted",
                                 "resets_in_seconds": 120}}


@pytest.mark.parametrize("path", ["/v1/responses", "/v1/messages"])
def test_a_refusal_sends_retry_after_exactly_once(server_with, monkeypatch, path):
    # _send_json used to write every extra header twice (one unfiltered loop,
    # then a filtered one), so each refusal carried two Retry-After headers.
    base = server_with([_codex()])
    monkeypatch.setattr(daemon._gateway, "handle", lambda *a, **kw: GatewayResult(
        status=429, headers={"retry-after": "120"}, body_chunks=None, profile_id=None,
        error="openai_codex_exhausted" if path == "/v1/responses" else "no_eligible_profile"))

    try:
        urllib.request.urlopen(_post(base, path, body=b'{"stream": false}'), timeout=5)
    except urllib.error.HTTPError as e:
        assert e.code == 429
        assert e.headers.get_all("Retry-After") == ["120"]
    else:
        raise AssertionError("expected an HTTP error")


def test_end_to_end_relay_through_the_daemon(server_with, monkeypatch):
    FakeHTTPSConnection.instances = []
    sse = (b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
           b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{}}\n\n")

    def factory(host, port, timeout=None):
        conn = FakeHTTPSConnection(host, port, timeout)
        conn.response = FakeHTTPResponse(200, {"content-type": "text/event-stream",
                                               "x-codex-primary-used-percent": "5", "cf-ray": "x"}, sse)
        return conn

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    base = server_with([_codex()])

    with urllib.request.urlopen(_post(base, "/v1/responses"), timeout=5) as resp:
        assert resp.status == 200
        assert resp.headers["x-codex-primary-used-percent"] == "5"
        assert resp.headers.get("cf-ray") is None
        assert resp.read() == sse

    [conn] = FakeHTTPSConnection.instances
    sent = conn.requests[0]
    assert sent["body"] == BODY
    assert sent["headers"]["Authorization"] == "Bearer tok-c"
    assert sent["headers"]["Originator"] == "codex_exec"   # the client's own header, forwarded


# ---- keep-alive ------------------------------------------------------------------

@pytest.fixture
def fast_keepalive(monkeypatch):
    monkeypatch.setattr(daemon, "_KEEPALIVE_AFTER_SECONDS", 0.05)
    monkeypatch.setattr(daemon, "_KEEPALIVE_INTERVAL_SECONDS", 0.05)


def _slow_handle(result, released):
    def handle(*a, **kw):
        released.wait(5)
        return result
    return handle


def _last_event(data):
    last = data.rstrip(b"\n").split(b"\n\n")[-1]
    event, _, payload = last.partition(b"\ndata: ")
    return event, json.loads(payload)


@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/responses", b'{"stream": true}'),
    ("POST", "/v1/responses/compact", b'{"stream": true}'),
    ("POST", "/v1/responses/", b'{"stream": true}'),
    ("POST", "/v1/responses", b'{"stream": false}'),
    ("POST", "/v1/responses", b"not json"),
    ("GET", "/v1/responses", b'{"stream": true}'),
])
def test_responses_calls_never_use_the_keepalive(method, path, body):
    assert daemon._wants_event_stream(method, path, body) is False


def test_a_slow_upstream_error_keeps_its_real_status_headers_and_body(server_with, monkeypatch, fast_keepalive):
    base = server_with([_codex()])
    released = threading.Event()
    upstream = json.dumps({"error": {"type": "usage_limit_reached",
                                     "message": "The usage limit has been reached"}}).encode()
    monkeypatch.setattr(daemon._gateway, "handle", _slow_handle(GatewayResult(
        status=429, headers={"content-type": "application/json", "x-codex-primary-used-percent": "100",
                             "retry-after": "60"},
        body_chunks=iter([upstream]), profile_id="c"), released))
    threading.Timer(0.3, released.set).start()   # six keep-alive intervals

    status, headers, payload = _error(_post(base, "/v1/responses"))

    assert status == 429
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["x-codex-primary-used-percent"] == "100"
    assert lowered["retry-after"] == "60"
    assert payload == json.loads(upstream)


def test_a_slow_stream_keeps_its_upstream_headers_and_gets_no_pings(server_with, monkeypatch, fast_keepalive):
    base = server_with([_codex()])
    released = threading.Event()
    sse = b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{}}\n\n"
    monkeypatch.setattr(daemon._gateway, "handle", _slow_handle(GatewayResult(
        status=200, headers={"content-type": "text/event-stream", "x-codex-turn-state": "ts-1"},
        body_chunks=iter([sse]), profile_id="c"), released))
    threading.Timer(0.3, released.set).start()

    with urllib.request.urlopen(_post(base, "/v1/responses"), timeout=5) as resp:
        assert resp.headers["x-codex-turn-state"] == "ts-1"
        data = resp.read()

    assert data == sse                     # no `event: ping` in front of it


def test_a_local_cooldown_refusal_is_an_openai_shaped_503(server_with, monkeypatch):
    base = server_with([_codex()])
    monkeypatch.setattr(daemon._gateway, "handle", lambda *a, **kw: GatewayResult(
        status=503, headers={"retry-after": "12"}, body_chunks=None, profile_id=None,
        error="openai_codex_temporarily_unavailable", error_detail="Every account is briefly cooling down."))

    status, headers, payload = _error(_post(base, "/v1/responses"))

    assert status == 503
    assert {k.lower(): v for k, v in headers.items()}["retry-after"] == "12"
    assert payload == {"error": {"message": "Every account is briefly cooling down.", "type": "server_error",
                                 "code": "openai_codex_temporarily_unavailable"}}


def test_messages_late_refusal_is_still_the_anthropic_error_event(server_with, monkeypatch, fast_keepalive):
    base = server_with([_codex()])
    released = threading.Event()
    monkeypatch.setattr(daemon._gateway, "handle", _slow_handle(GatewayResult(
        status=429, headers={}, body_chunks=None, profile_id=None, error="no_eligible_profile"), released))
    threading.Timer(0.3, released.set).start()

    with urllib.request.urlopen(_post(base, "/v1/messages", body=b'{"stream": true}'), timeout=5) as resp:
        data = resp.read()

    event, payload = _last_event(data)
    assert event == b"event: error"
    assert payload == {"type": "error", "error": {"type": "rate_limit_error",
                                                  "message": "[claude-unlimited] No eligible Profile is available right now."}}
