"""OpenAI-shaped ingress, third round of fixes (H1-H4). Same harness as
test_gateway_openai_ingress.py: the Gateway and openai_bridge run for real,
only the HTTPS connection is faked. The daemon-level cases use a real
loopback server on an ephemeral port, as test_daemon_openai_ingress.py does."""

import http.client
import json

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import placeholder_token
from claude_unlimited.config import Pool, save_pool
from claude_unlimited.gateway import Gateway, is_openai_ingress
from claude_unlimited.openai_bridge import OpenAIBridgeResult
from claude_unlimited.router import ProfileState
from claude_unlimited.upstream import UpstreamResponse

from test_gateway_openai_ingress import (  # noqa: F401 - pool_env is a fixture
    BODY, CODEX_HEADERS, SSE, Upstream, _codex, _drain, _in, _no_transport, _oauth, _ok, _session,
    _set_state, _status, _tokens, pool_env,
)
from test_gateway_openai_ingress_round2 import _PARTIAL_SSE, _BreakingStream
from test_daemon_openai_ingress import (  # noqa: F401 - server_with is a fixture
    FakeHTTPResponse as DaemonFakeResponse, FakeHTTPSConnection as DaemonFakeConnection,
    _codex as _daemon_codex, _oauth as _daemon_oauth, server_with,
)

_SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _counts(gw):
    return dict(gw._in_flight_count), set(gw._in_flight), dict(gw._in_flight_since)


_BALANCED = ({}, set(), {})


# ---- H1: a single account's cooldown never outlasts the Codex CLI's retries ----------

def test_h1_after_a_5xx_the_single_accounts_next_request_reaches_upstream(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(503, body=b"overloaded"), _ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert first.status == 503 and first.error is None and _drain(first) == b"overloaded"
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN     # bookkeeping unchanged

    again = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert again.status == 200 and again.error is None and again.profile_id == "c1"
    assert _drain(again) == SSE
    assert _tokens(up) == ["tok-c1", "tok-c1"]
    assert _counts(gw) == _BALANCED


def test_h1_after_a_mid_stream_failure_the_retry_reaches_the_single_account(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    breaking = _BreakingStream(200, dict(_SSE_HEADERS), _PARTIAL_SSE)
    up = Upstream(monkeypatch, {"tok-c1": [breaking, _ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)

    assert _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY)) == _PARTIAL_SSE
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN

    again = gw.handle("POST", "/v1/responses", _session("s1"), BODY)   # the Codex CLI's reconnect

    assert again.status == 200 and again.profile_id == "c1"
    assert _drain(again) == SSE
    assert len(up.requests) == 2
    assert _counts(gw) == _BALANCED


def test_h1_after_a_network_error_the_retry_reaches_the_single_account(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [OSError("connection reset"), _ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert first.status == 502 and first.error == gateway_module.OPENAI_ERROR_UPSTREAM_UNREACHABLE

    again = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert again.status == 200 and again.profile_id == "c1"
    _drain(again)
    assert len(up.requests) == 2
    assert all(c.closed for c in up.conns)
    assert _counts(gw) == _BALANCED


def test_h1_the_last_resort_is_the_cooldown_that_ends_soonest(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [], "tok-c2": [_ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(20))
    _set_state(gw, "c2", ProfileState.COOLDOWN, cooldown_until=_in(1))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 200 and result.profile_id == "c2"
    _drain(result)
    assert _tokens(up) == ["tok-c2"]


def test_h1_a_failed_last_resort_is_relayed_and_is_the_only_attempt(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(502, body=b"bad gateway")], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(0.2))
    _set_state(gw, "c2", ProfileState.COOLDOWN, cooldown_until=_in(10))
    before = gw._runtime["c1"].cooldown_until

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    # The real upstream answer, not a local refusal; ONE last-resort attempt.
    assert result.status == 502 and result.error is None and result.profile_id == "c1"
    assert _drain(result) == b"bad gateway"
    assert _tokens(up) == ["tok-c1"]
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN
    assert gw._runtime["c1"].cooldown_until > before          # re-observed as usual
    assert _counts(gw) == _BALANCED


def test_h1_a_last_resort_network_failure_is_a_502_after_one_attempt(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [OSError("down")], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(0.2))
    _set_state(gw, "c2", ProfileState.COOLDOWN, cooldown_until=_in(10))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 502 and result.error == gateway_module.OPENAI_ERROR_UPSTREAM_UNREACHABLE
    assert _tokens(up) == ["tok-c1"]
    assert _counts(gw) == _BALANCED


@pytest.mark.parametrize("state,changes,expected", [
    (ProfileState.EXHAUSTED, {"resets_at": _in(30)}, 429),
    (ProfileState.AUTH_INVALID, {}, 400),
])
def test_h1_exhausted_and_auth_invalid_accounts_are_never_a_last_resort(pool_env, monkeypatch, state, changes,
                                                                       expected):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", state, **changes)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == expected and result.error is not None
    assert up.conns == []


def test_h1_a_disabled_cooling_account_is_never_a_last_resort(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(1))
    _set_state(gw, "c2", ProfileState.EXHAUSTED, resets_at=_in(30))
    disabled = _codex("c1")
    disabled.enabled = False
    save_pool(Pool(profiles=[disabled, _codex("c2", priority=2)]))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.error is not None and result.status == 429
    assert up.conns == []


def test_h1_an_eligible_account_still_wins_over_a_cooling_one(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [], "tok-c2": [_ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(1))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.profile_id == "c2"
    _drain(result)
    assert _tokens(up) == ["tok-c2"]


def test_h1_a_pinned_cooling_account_is_tried(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=_SSE_HEADERS)], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(1))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1")

    assert result.status == 200 and result.profile_id == "c1"
    _drain(result)
    assert _tokens(up) == ["tok-c1"]


def test_h1_a_credential_read_failure_is_still_a_local_503(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": []})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(0.5))

    class LockedStore:
        def get_token(self, profile_id):
            raise RuntimeError("keychain locked")

    monkeypatch.setattr(gateway_module, "secret_store", LockedStore())

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert int(result.headers["retry-after"]) >= 1
    assert up.conns == []


# ---- H2: an exception after the busy mark releases the slot exactly once -------------

class _Conn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _fail_first_persist(gw, monkeypatch):
    real = gw._persist
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk full once")
        return real()

    monkeypatch.setattr(gw, "_persist", flaky)


_MESSAGES_BODY = json.dumps({"model": "claude-haiku-4-5", "max_tokens": 5, "messages": []}).encode()


def test_h2_anthropic_path_exception_after_busy_releases_and_closes(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth("o")]))
    conns = []

    def transport(req):
        conns.append(_Conn())
        return UpstreamResponse(status=200, headers={"content-type": "application/json"},
                                body_chunks=iter([b"{}"]), connection=conns[-1])

    gw = Gateway(transport=transport)
    _fail_first_persist(gw, monkeypatch)

    with pytest.raises(RuntimeError, match="disk full once"):
        gw.handle("POST", "/v1/messages", {"authorization": "x"}, _MESSAGES_BODY)
    assert _counts(gw) == _BALANCED
    assert conns[0].closed

    for _ in range(3):
        result = gw.handle("POST", "/v1/messages", {"authorization": "x"}, _MESSAGES_BODY)
        assert result.status == 200
        _drain(result)
    assert _counts(gw) == _BALANCED
    assert gw.serving_now_ids() == set()


def test_h2_codex_bridge_path_exception_after_busy_releases_and_closes(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    bodies = []

    class Body:
        def __init__(self):
            self.closed = False
            self._it = iter([b"event: message_stop\ndata: {}\n\n"])

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._it)

        def close(self):
            self.closed = True

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None):
        bodies.append(Body())
        return OpenAIBridgeResult(status=200, headers={"content-type": "text/event-stream"},
                                  body_chunks=bodies[-1])

    monkeypatch.setattr(gateway_module.openai_bridge, "run", fake_run)
    gw = Gateway(transport=_no_transport)
    _fail_first_persist(gw, monkeypatch)

    with pytest.raises(RuntimeError, match="disk full once"):
        gw.handle("POST", "/v1/messages", {}, b'{"stream": true}')
    assert _counts(gw) == _BALANCED
    assert bodies[0].closed

    for _ in range(3):
        result = gw.handle("POST", "/v1/messages", {}, b'{"stream": true}')
        assert result.status == 200
        _drain(result)
    assert _counts(gw) == _BALANCED


def test_h2_ingress_exception_after_busy_releases_and_closes(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=_SSE_HEADERS) for _ in range(4)]})
    gw = Gateway(transport=_no_transport)
    _fail_first_persist(gw, monkeypatch)

    with pytest.raises(RuntimeError, match="disk full once"):
        gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert _counts(gw) == _BALANCED
    assert up.conns[0].closed

    for _ in range(3):
        _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


# ---- H3: every spelling of /v1/responses is the ingress, never a Claude account ------

@pytest.mark.parametrize("path,served", [
    ("/v1/responses%3Fx=1", True),
    ("/v1/responses%23frag", True),
    ("/V1/responses", True),
    ("/v1/Responses/compact", True),
    ("/v1/responses%00", False),
    ("/v1/responses%0a", False),
    ("/v1/responses\x00", False),
    ("/v1//responses", False),
    ("/v1/./responses", False),
    ("/v1/messages/../responses", False),
    ("/v1/%2E/responses", False),
    ("/v1%2F%2Fresponses", False),
    ("/v1/responses%253Fx=1", False),
])
def test_h3_path_variants_never_reach_a_claude_account(pool_env, monkeypatch, path, served):
    save_pool(Pool(profiles=[_oauth("o"), _codex("c1", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)      # raises if the Anthropic transport is used

    assert is_openai_ingress("POST", path)
    result = gw.handle("POST", path, dict(CODEX_HEADERS), BODY)

    if served:
        assert result.status == 200 and result.profile_id == "c1"
        assert up.requests[0]["path"] in ("/backend-api/codex/responses", "/backend-api/codex/responses/compact")
        _drain(result)
    else:
        assert result.status == 400 and result.error == gateway_module.OPENAI_ERROR_BAD_REQUEST
        assert up.conns == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/responses/resp_123"),
    ("GET", "/v1/responses"),
    ("DELETE", "/v1/responses/resp_123"),
    ("GET", "/V1//responses/resp_123?x=1"),
])
def test_h3_any_other_method_is_a_local_405(pool_env, monkeypatch, method, path):
    save_pool(Pool(profiles=[_oauth("o"), _codex("c1", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": []})
    gw = Gateway(transport=_no_transport)

    assert is_openai_ingress(method, path)
    result = gw.handle(method, path, dict(CODEX_HEADERS), b"")

    assert result.status == 405 and result.error == gateway_module.OPENAI_ERROR_METHOD_NOT_ALLOWED
    assert result.headers == {"allow": "POST"}
    assert up.conns == []


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages?beta=true", "/v1/messages%3Fbeta=true",
                                  "/v1/messages/count_tokens", "/v1/responsesx"])
def test_h3_messages_paths_still_take_the_anthropic_path_unchanged(pool_env, path):
    save_pool(Pool(profiles=[_oauth("o")]))
    sent = []

    def transport(req):
        sent.append((req.method, req.url))
        return UpstreamResponse(status=200, headers={"content-type": "application/json"},
                                body_chunks=iter([b"{}"]), connection=_Conn())

    gw = Gateway(transport=transport)

    assert is_openai_ingress("POST", path) is False
    result = gw.handle("POST", path, {"authorization": "x"}, _MESSAGES_BODY)
    _drain(result)

    assert result.profile_id == "o"
    assert [m for m, _ in sent] == ["POST"]
    assert sent[0][1].startswith("https://api.anthropic.com" + path.split("?")[0])


def _raw_request(base, method, target, body=b""):
    host, port = base.rsplit("//", 1)[1].split(":")
    conn = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        conn.putrequest(method, target, skip_accept_encoding=True)
        conn.putheader("Authorization", f"Bearer {placeholder_token.get_or_create()}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        resp = conn.getresponse()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
    finally:
        conn.close()


def test_h3_daemon_a_leading_double_slash_is_still_the_codex_ingress(server_with, monkeypatch):
    DaemonFakeConnection.instances = []

    def factory(host, port, timeout=None):
        conn = DaemonFakeConnection(host, port, timeout)
        conn.response = DaemonFakeResponse(200, {"content-type": "text/event-stream"}, SSE)
        return conn

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    base = server_with([_daemon_oauth(), _daemon_codex()])   # Anthropic transport raises if used

    status, _headers, data = _raw_request(base, "POST", "//v1/responses", BODY)

    assert status == 200 and data == SSE
    [conn] = DaemonFakeConnection.instances
    assert conn.requests[0]["path"] == "/backend-api/codex/responses"


def test_h3_daemon_get_on_the_responses_route_is_an_openai_shaped_405(server_with):
    base = server_with([_daemon_oauth(), _daemon_codex()])   # Anthropic transport raises if used

    status, headers, data = _raw_request(base, "GET", "/v1/responses/resp_123")

    assert status == 405
    assert headers["allow"] == "POST"
    payload = json.loads(data)
    assert payload["error"]["type"] == "invalid_request_error"
    assert payload["error"]["code"] == gateway_module.OPENAI_ERROR_METHOD_NOT_ALLOWED


# ---- H4: long session ids are hashed, never truncated --------------------------------

def test_h4_long_session_ids_sharing_a_prefix_never_collide(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(headers=_SSE_HEADERS)], "tok-c2": [_ok(headers=_SSE_HEADERS)]})
    gw = Gateway(transport=_no_transport)
    a, b = "x" * 200 + "A", "x" * 200 + "B"

    _drain(gw.handle("POST", "/v1/responses", _session(a), BODY))
    _set_state(gw, "c1", ProfileState.DRAINING)   # keeps a session already there, avoided by a new one
    gw._openai_ingress_profile_id = None

    result = gw.handle("POST", "/v1/responses", _session(b), BODY)
    _drain(result)

    assert result.profile_id == "c2"              # b is a NEW session, not a's
    assert len(gw._openai_sessions) == 2
    assert _tokens(up) == ["tok-c1", "tok-c2"]


def test_h4_session_keys_are_bounded_and_depend_on_the_whole_id():
    long_a = gateway_module._openai_session_key({"session-id": "y" * 10_000 + "1"})
    long_b = gateway_module._openai_session_key({"session-id": "y" * 10_000 + "2"})
    fallback = gateway_module._openai_session_key({"thread-id": "t" * 500})

    assert long_a != long_b
    assert all(k.startswith("sha256:") and len(k) == 71 for k in (long_a, long_b, fallback))
    assert gateway_module._openai_session_key({"session-id": "  short  "}) == "short"
