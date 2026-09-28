"""OpenAI-shaped ingress, fourth round of fixes (K1-K12). Same harness as
test_gateway_openai_ingress.py: the Gateway and openai_bridge run for real,
only the HTTPS connection is faked. The daemon-level cases use a real
loopback server on an ephemeral port, as test_daemon_openai_ingress.py does.
Every state path is redirected to tmp_path (the pool_env / server_with
fixtures plus conftest's autouse isolation)."""

import http.client
import json
import socket
import time
from datetime import datetime, timedelta, timezone

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited import placeholder_token, usage_tracking
from claude_unlimited.config import Pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.observation import QuotaExhausted, ShortRateLimit, UsageSnapshot
from claude_unlimited.openai_bridge import OpenAIBridgeResult
from claude_unlimited.openai_observation import classify, parse_credits
from claude_unlimited.router import ProfileState

from test_gateway_openai_ingress import (  # noqa: F401 - pool_env is a fixture
    BODY, MOVE_BODY, SSE, FakeHTTPResponse, FakeSecretStore, Upstream, _codex, _codex_cred, _drain, _in,
    _no_transport, _quota_429, _session, _set_state, _status, _tokens, pool_env,
)
from test_daemon_openai_ingress import (  # noqa: F401 - server_with is a fixture
    FakeHTTPSConnection as DaemonFakeConnection, _codex as _daemon_codex, _oauth as _daemon_oauth, server_with,
)

_SSE_HEADERS = {"Content-Type": "text/event-stream"}


def _counts(gw):
    return dict(gw._in_flight_count), set(gw._in_flight), dict(gw._in_flight_since)


_BALANCED = ({}, set(), {})


def _four_accounts(monkeypatch):
    ids = ["c1", "c2", "c3", "c4"]
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore(
        {pid: _codex_cred(f"tok-{pid}", f"acct-{pid}") for pid in ids}))
    save_pool(Pool(profiles=[_codex(pid, priority=i) for i, pid in enumerate(ids, start=1)]))


# ---- K1: a Responses stream with no Content-Type is still parsed as SSE --------------

class _TruncatedChunkedClose(FakeHTTPResponse):
    """A 200 stream that sends `body`, then closes without the chunked
    terminator: http.client raises IncompleteRead with nothing left over."""

    def read(self, n=None):
        if self._pos >= len(self._body):
            raise http.client.IncompleteRead(b"")
        return super().read(n)


@pytest.mark.parametrize("content_type", [None, "Text/Event-Stream; charset=UTF-8"])
def test_k1_a_complete_stream_with_a_truncated_close_cools_nothing(pool_env, monkeypatch, content_type):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    # What chatgpt.com sends: no Content-Type at all (measured live).
    first_headers = {"x-codex-turn-state": "ts-c1"}
    if content_type is not None:
        first_headers["Content-Type"] = content_type
    first = _TruncatedChunkedClose(200, first_headers, SSE)
    up = Upstream(monkeypatch, {"tok-c1": [first, FakeHTTPResponse(200, {}, SSE)], "tok-c2": []})
    gw = Gateway(transport=_no_transport)

    turn1 = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert turn1.status == 200
    assert _drain(turn1) == SSE                                  # relayed byte for byte

    assert gw._runtime["c1"].state != ProfileState.COOLDOWN     # no stream-failure cooling
    assert gw._openai_sessions == {"s1": "c1"}                   # session map unchanged

    turn2 = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY)
    assert turn2.status == 200 and turn2.profile_id == "c1"
    _drain(turn2)
    assert _tokens(up) == ["tok-c1", "tok-c1"]
    sent = up.requests[1]
    assert sent["body"] == MOVE_BODY                             # byte-identical, nothing stripped
    assert sent["headers"]["x-codex-turn-state"] == "ts-c1"
    assert gw._openai_sessions == {"s1": "c1"}
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_k1_a_stream_with_no_content_type_cut_before_its_terminal_event_still_cools(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    partial = b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"
    up = Upstream(monkeypatch, {"tok-c1": [_TruncatedChunkedClose(200, {}, partial)], "tok-c2": []})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert _drain(result) == partial

    assert gw._runtime["c1"].state == ProfileState.COOLDOWN     # a real mid-stream failure
    assert _counts(gw) == _BALANCED
    assert up.conns[0].closed


def test_k1_a_json_body_with_no_content_type_is_not_parsed_as_sse():
    class Resp(FakeHTTPResponse):
        pass

    events = []
    conn = type("C", (), {"closed": False, "close": lambda self: setattr(self, "closed", True)})()
    reads = bridge_module._stream_reads(Resp(200, {}, b'{"object":"response","status":"completed"}'), conn,
                                        parse_events=None)
    raw = b""
    for chunk, parsed in reads:
        raw += chunk
        events.extend(parsed)
    assert raw == b'{"object":"response","status":"completed"}' and events == []
    assert conn.closed


# ---- K2: a short 429 with nowhere to go is a local 503 the Codex CLI retries -------

def _short_429(headers=None):
    base = {"content-type": "application/json", "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "300"}
    base.update(headers or {})
    return FakeHTTPResponse(429, base, b'{"error":{"type":"rate_limit_exceeded","message":"slow down"}}')


def test_k2_single_account_short_429_is_a_local_503_with_retry_after(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "1"})]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503
    assert result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "1"
    assert len(up.requests) == 1
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_k2_pinned_short_429_is_a_local_503(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "4"})], "tok-c2": []})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1")

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "4"
    assert _tokens(up) == ["tok-c1"]
    assert _counts(gw) == _BALANCED


def test_k2_retry_after_comes_from_the_reset_header_when_there_is_no_retry_after(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_short_429({"x-codex-primary-reset-after-seconds": "7"})]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.headers["retry-after"] == "7"


def test_k2_retry_after_falls_back_to_the_cooldown(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(429, {}, b"")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503
    cooldown = (gw._runtime["c1"].cooldown_until - datetime.now(timezone.utc)).total_seconds()
    assert abs(int(result.headers["retry-after"]) - cooldown) <= 2


def test_k2_the_codex_cli_retry_after_a_short_429_reaches_the_account_again(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "1"}), _status(200, _SSE_HEADERS, SSE)]})
    gw = Gateway(transport=_no_transport)

    assert gw.handle("POST", "/v1/responses", _session("s1"), BODY).status == 503
    retry = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert retry.status == 200 and _drain(retry) == SSE      # the last-resort attempt
    assert len(up.requests) == 2


def test_k2_a_quota_429_is_still_relayed(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_quota_429()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 429 and result.error is None
    assert b"usage_limit_reached" in _drain(result)


def test_k2_a_short_429_with_another_account_still_fails_over(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_short_429()], "tok-c2": [_status(200, _SSE_HEADERS, SSE)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 200 and result.profile_id == "c2"
    _drain(result)
    assert _tokens(up) == ["tok-c1", "tok-c2"]


# ---- K3: the last resort prefers the session's own cooling account -----------------

def test_k3_last_resort_goes_back_to_the_sessions_own_account(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(200, _SSE_HEADERS, SSE)], "tok-c2": []})
    gw = Gateway(transport=_no_transport)
    gw._openai_sessions["s1"] = "c1"
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(40))
    _set_state(gw, "c2", ProfileState.COOLDOWN, cooldown_until=_in(5))      # ends sooner

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY)

    assert result.status == 200 and result.profile_id == "c1"
    _drain(result)
    assert _tokens(up) == ["tok-c1"]
    assert up.requests[0]["body"] == MOVE_BODY                   # no move strip
    assert up.requests[0]["headers"]["x-codex-turn-state"] == "ts-c1"
    assert gw._openai_sessions == {"s1": "c1"}
    assert _counts(gw) == _BALANCED


def test_k3_back_to_the_sessions_own_account_after_another_one_failed_is_not_a_move(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(200, _SSE_HEADERS, SSE)], "tok-c2": [OSError("reset")]})
    gw = Gateway(transport=_no_transport)
    gw._openai_sessions["s1"] = "c1"
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(40))

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY)

    assert result.status == 200 and result.profile_id == "c1"
    _drain(result)
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert up.requests[0]["body"] != MOVE_BODY                   # c2 was a move: stripped
    assert up.requests[1]["body"] == MOVE_BODY                   # back home: byte-identical
    assert up.requests[1]["headers"]["x-codex-turn-state"] == "ts-c1"
    assert _counts(gw) == _BALANCED


# ---- K4: a later network failure does not leave the client with a 4xx --------------

# A saved QUOTA 429 and a saved SHORT 429 are no longer here: since round 5 the
# quota one is relayed as-is and the short one is the local 503 — see
# test_gateway_openai_ingress_round5.py.
@pytest.mark.parametrize("first", [
    lambda: _status(401),
])
def test_k4_a_saved_4xx_then_a_network_failure_is_a_local_502(pool_env, monkeypatch, first):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [first()], "tok-c2": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 502 and result.error == gateway_module.OPENAI_ERROR_UPSTREAM_UNREACHABLE
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_k4_a_saved_5xx_then_a_network_failure_is_still_relayed(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    Upstream(monkeypatch, {"tok-c1": [_status(503, body=b"busy")], "tok-c2": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error is None and _drain(result) == b"busy"


# ---- K5: the body cap is enforced before the body is read ---------------------------

def _raw(base, request: bytes, timeout=3.0) -> bytes:
    host, port = base.rsplit("//", 1)[1].split(":")
    sock = socket.create_connection((host, int(port)), timeout=timeout)
    try:
        sock.sendall(request)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return data
            data += chunk
    finally:
        sock.close()


def _head(content_length_lines: str) -> bytes:
    token = placeholder_token.get_or_create()
    return (f"POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n"
            f"Content-Type: application/json\r\n{content_length_lines}\r\n").encode()


@pytest.mark.parametrize("lines", [
    "Content-Length: 2000000000\r\n",
    f"Content-Length: {gateway_module._OPENAI_INGRESS_MAX_BODY_BYTES + 1}\r\n",
    "Content-Length: -1\r\n",
    "Content-Length: 12abc\r\n",
    "",                                                     # missing on a POST
    "Transfer-Encoding: chunked\r\n",
    "Content-Length: 5\r\nContent-Length: 6\r\n",
])
def test_k5_a_bad_or_oversized_length_is_refused_before_the_body_is_read(server_with, monkeypatch, lines):
    DaemonFakeConnection.instances = []
    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", DaemonFakeConnection)
    base = server_with([_daemon_oauth(), _daemon_codex()])   # Anthropic transport raises if used

    # No body is ever sent: a server that waited for it would time out here.
    started = time.monotonic()
    data = _raw(base, _head(lines))
    assert time.monotonic() - started < 2.5

    head, _, payload = data.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 400") or head.startswith(b"HTTP/1.0 400")
    assert b"connection: close" in head.lower()
    error = json.loads(payload)["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == gateway_module.OPENAI_ERROR_BAD_REQUEST
    assert DaemonFakeConnection.instances == []                  # nothing went upstream


def test_k5_a_body_within_the_cap_is_still_read_and_relayed(server_with, monkeypatch):
    from test_daemon_openai_ingress import FakeHTTPResponse as DaemonFakeResponse

    DaemonFakeConnection.instances = []

    def factory(host, port, timeout=None):
        conn = DaemonFakeConnection(host, port, timeout)
        conn.response = DaemonFakeResponse(200, {"content-type": "text/event-stream"}, SSE)
        return conn

    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection", factory)
    base = server_with([_daemon_oauth(), _daemon_codex()])

    data = _raw(base, _head(f"Content-Length: {len(BODY)}\r\nConnection: close\r\n") + BODY)

    assert data.startswith(b"HTTP/1.")
    assert b" 200 " in data.split(b"\r\n", 1)[0]
    assert data.endswith(SSE)
    [conn] = DaemonFakeConnection.instances
    assert conn.requests[0]["body"] == BODY


# ---- K6: an upstream error body is read only up to a cap ----------------------------

class _EndlessErrorBody(FakeHTTPResponse):
    """An error response whose body never ends. read() with no size would
    never return, so it is refused outright."""

    def __init__(self, status, headers, prefix=b""):
        super().__init__(status, headers, b"")
        self._prefix = prefix
        self.bytes_read = 0

    def read(self, n=None):
        if n is None:
            raise AssertionError("an unbounded read of an endless error body")
        out = (self._prefix[self.bytes_read:] + b"x" * n)[:n]
        self.bytes_read += len(out)
        return out


def _cred_c1():
    return _codex_cred("tok-c1", "acct-1")


def test_k6_an_endless_error_body_is_truncated_and_the_connection_closed(pool_env, monkeypatch):
    endless = _EndlessErrorBody(503, {"content-type": "application/json"})
    up = Upstream(monkeypatch, {"tok-c1": [endless]})

    result = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", BODY, {})

    assert result.status == 503
    body = b"".join(result.body_chunks)
    assert len(body) == bridge_module._MAX_ERROR_BODY_BYTES
    assert endless.bytes_read <= bridge_module._MAX_ERROR_BODY_BYTES + 1
    assert up.conns[0].closed


def test_k6_the_encrypted_state_retry_reads_a_bounded_body_too(pool_env, monkeypatch):
    refusal = _EndlessErrorBody(400, {"content-type": "application/json"},
                                prefix=b'{"error":{"message":"could not decrypt the encrypted content"}}')
    up = Upstream(monkeypatch, {"tok-c1": [refusal, _status(200, _SSE_HEADERS, SSE)]})

    result = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", MOVE_BODY,
                                           {"x-codex-turn-state": "ts"})

    assert result.status == 200 and b"".join(result.body_chunks) == SSE
    assert len(up.requests) == 2
    assert refusal.bytes_read <= bridge_module._MAX_ERROR_BODY_BYTES + 1
    assert all(c.closed for c in up.conns)


# ---- K7: the encrypted-state retry counts against the request's budget -------------

def _encrypted_refusal():
    return _status(400, body=b'{"error":{"message":"Encrypted content could not be decrypted."}}')


def test_k7_the_encrypted_state_retry_is_charged_to_the_attempt_budget(pool_env, monkeypatch):
    _four_accounts(monkeypatch)
    up = Upstream(monkeypatch, {
        "tok-c1": [_encrypted_refusal(), _status(503, body=b"busy-c1")],
        "tok-c2": [_status(503, body=b"busy-c2")],
        "tok-c3": [_status(503, body=b"busy-c3")],
        "tok-c4": [_status(503, body=b"busy-c4")],
    })
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts"}), MOVE_BODY)

    assert len(up.requests) == gateway_module.MAX_ROTATION_ATTEMPTS
    assert _tokens(up) == ["tok-c1", "tok-c1", "tok-c2", "tok-c3"]
    assert result.status == 503 and result.error is None and result.profile_id == "c3"
    assert _drain(result) == b"busy-c3"
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_k7_no_retry_when_the_budget_has_no_request_left(pool_env, monkeypatch):
    _four_accounts(monkeypatch)
    up = Upstream(monkeypatch, {
        "tok-c1": [_status(503, body=b"busy-c1")],
        "tok-c2": [_status(503, body=b"busy-c2")],
        "tok-c3": [_status(503, body=b"busy-c3")],
        "tok-c4": [_encrypted_refusal(), _status(200, _SSE_HEADERS, SSE)],
    })
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts"}), MOVE_BODY)

    assert len(up.requests) == gateway_module.MAX_ROTATION_ATTEMPTS
    assert result.status == 400 and result.error is None and result.profile_id == "c4"
    assert b"decrypted" in _drain(result)
    assert _counts(gw) == _BALANCED


def test_k7_run_passthrough_reports_how_many_requests_it_sent(pool_env, monkeypatch):
    Upstream(monkeypatch, {"tok-c1": [_encrypted_refusal(), _status(200, _SSE_HEADERS, SSE)]})
    result = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", MOVE_BODY, {})
    assert result.upstream_requests == 2
    _ = b"".join(result.body_chunks)

    Upstream(monkeypatch, {"tok-c1": [_encrypted_refusal()]})
    single = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", MOVE_BODY, {},
                                           max_upstream_requests=1)
    assert single.status == 400 and single.upstream_requests == 1


# ---- K8: usage capture matches Content-Type case-insensitively; no false OpenAI ----

_OPENAI_SSE = (b"event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":"
               b"{\"model\":\"gpt-5.6-sol\",\"usage\":{\"input_tokens\":10,\"input_tokens_details\":"
               b"{\"cached_tokens\":4},\"output_tokens\":3,\"total_tokens\":13}}}\n\n")


@pytest.mark.parametrize("content_type", ["Text/Event-Stream", "text/event-stream; charset=utf-8",
                                          "TEXT/EVENT-STREAM"])
def test_k8_a_case_variant_sse_content_type_records_usage(content_type):
    capture = usage_tracking.UsageCapture()
    assert b"".join(capture.wrap(iter([_OPENAI_SSE]), content_type)) == _OPENAI_SSE
    assert capture.model == "gpt-5.6-sol"
    assert capture.usage == {"input_tokens": 6, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 4, "output_tokens": 3}


def test_k8_a_generic_json_usage_with_total_tokens_keeps_the_anthropic_reading():
    body = b'{"model":"m","usage":{"input_tokens":3,"output_tokens":2,"total_tokens":5}}'
    capture = usage_tracking.UsageCapture()
    assert b"".join(capture.wrap(iter([body]), None)) == body
    assert capture.model == "m"
    assert capture.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


def test_k8_a_json_body_marked_as_a_response_object_is_read_as_openai():
    body = b'{"object":"response","model":"gpt-5.6-sol","usage":{"input_tokens":3,"output_tokens":2}}'
    capture = usage_tracking.UsageCapture()
    b"".join(capture.wrap(iter([body]), None))
    assert capture.usage == {"input_tokens": 3, "cache_creation_input_tokens": 0,
                             "cache_read_input_tokens": 0, "output_tokens": 2}


def test_k8_openai_usage_details_still_mark_an_openai_json_body():
    body = (b'{"model":"gpt-5.6-sol","usage":{"input_tokens":10,"input_tokens_details":{"cached_tokens":4},'
            b'"output_tokens":3}}')
    capture = usage_tracking.UsageCapture()
    b"".join(capture.wrap(iter([body]), "application/json"))
    assert capture.usage["cache_read_input_tokens"] == 4 and capture.usage["input_tokens"] == 6


# ---- K9: non-finite numeric headers never raise --------------------------------------

@pytest.mark.parametrize("upstream", [
    lambda: FakeHTTPResponse(429, {"content-type": "application/json", "x-codex-primary-used-percent": "100",
                                   "x-codex-primary-reset-at": "inf"}, b'{"error":{}}'),
    lambda: FakeHTTPResponse(429, {"content-type": "application/json", "x-codex-primary-used-percent": "10",
                                   "retry-after": "nan"}, b'{"error":{}}'),
    lambda: FakeHTTPResponse(429, {"content-type": "application/json", "x-codex-primary-used-percent": "100",
                                   "x-codex-primary-reset-at": "1e300"}, b'{"error":{}}'),
    lambda: FakeHTTPResponse(200, {"content-type": "text/event-stream", "x-codex-primary-used-percent": "nan",
                                   "x-codex-primary-reset-at": "inf", "retry-after": "nan",
                                   "x-codex-credits-balance": "inf"}, SSE),
])
def test_k9_non_finite_headers_on_the_ingress_give_a_normal_response(pool_env, monkeypatch, upstream):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [upstream()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status in (200, 429, 503)
    if result.body_chunks is not None:
        _drain(result)
    assert _counts(gw) == _BALANCED


@pytest.mark.parametrize("status,headers", [
    (429, {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-at": "inf"}),
    (429, {"x-codex-primary-used-percent": "10", "retry-after": "nan"}),
    (200, {"x-codex-primary-used-percent": "nan", "x-codex-credits-balance": "inf"}),
])
def test_k9_non_finite_headers_on_the_translated_codex_path_give_a_normal_response(pool_env, monkeypatch,
                                                                                  status, headers):
    save_pool(Pool(profiles=[_codex("c1")]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None, **kw):
        return OpenAIBridgeResult(status=status, headers=dict(headers), body_chunks=iter([b'{"ok":true}']))

    monkeypatch.setattr(bridge_module, "run", fake_run)
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/messages", {}, b'{"stream":true}')

    assert result.status in (200, 429, 503)
    if result.body_chunks is not None:
        _drain(result)


def test_k9_the_header_filter_drops_non_finite_and_overflowing_values():
    filtered = gateway_module._filter_openai_headers({
        "X-Codex-Primary-Used-Percent": "nan", "x-codex-primary-reset-at": "1e300",
        "x-codex-secondary-reset-after-seconds": "1e300", "retry-after": "inf",
        "x-codex-secondary-used-percent": "42", "x-codex-plan-type": "plus", "x-codex-credits-balance": "-inf",
    })
    assert filtered == {"x-codex-secondary-used-percent": "42", "x-codex-plan-type": "plus"}


# ---- K10: a stream that keeps flowing never ages out of in-flight -------------------

class _SlowStream(FakeHTTPResponse):
    pass


def test_k10_a_long_live_stream_still_counts_as_activity(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    frames = [b"event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\"}\n\n"] * 3
    body = b"".join(frames) + SSE
    monkeypatch.setattr(bridge_module, "CHUNK_READ_SIZE", len(frames[0]))
    monkeypatch.setattr(gateway_module, "_IN_FLIGHT_TOUCH_SECONDS", 0.0, raising=False)
    Upstream(monkeypatch, {"tok-c1": [_SlowStream(200, dict(_SSE_HEADERS), body)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    chunks = iter(result.body_chunks)
    next(chunks)
    # The stream has now been open longer than the leaked-slot cap...
    with gw._lock:
        gw._in_flight_since["c1"] -= gw._IN_FLIGHT_MAX_SECONDS + 1
    next(chunks)                    # ...but it is still delivering bytes.

    assert gw.serving_now_ids() == {"c1"}
    assert gw.seconds_since_last_activity() == 0.0
    assert not gw.is_idle(900)

    for _ in chunks:
        pass
    assert _counts(gw) == _BALANCED


def test_k10_a_slot_with_no_bytes_flowing_still_ages_out(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_status(200, _SSE_HEADERS, SSE)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)   # never read
    with gw._lock:
        gw._in_flight_since["c1"] -= gw._IN_FLIGHT_MAX_SECONDS + 1

    assert gw.serving_now_ids() == set()             # a leaked slot stops blocking
    result.body_chunks.close()
    assert _counts(gw) == _BALANCED


def test_k10_the_touch_is_throttled(pool_env, monkeypatch):
    touches = []
    body = gateway_module._InFlightBody(iter([b"a", b"b", b"c"]), lambda: None, (),
                                        touch=lambda: touches.append(1))
    assert list(body) == [b"a", b"b", b"c"]
    assert len(touches) == 1         # three chunks inside one second: one lock acquisition


# ---- K12: openai_observation reads either window's exhaustion ----------------------

NOW = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)


def test_k12_secondary_only_exhaustion_is_quota_exhausted_with_the_secondary_reset():
    weekly = int((NOW + timedelta(days=4)).timestamp())
    obs = classify(429, {"x-codex-primary-used-percent": "10",
                         "x-codex-primary-reset-at": str(int((NOW + timedelta(hours=2)).timestamp())),
                         "x-codex-secondary-used-percent": "100",
                         "x-codex-secondary-reset-at": str(weekly)}, NOW)
    assert obs == QuotaExhausted(resets_at=datetime.fromtimestamp(weekly, tz=timezone.utc))


def test_k12_both_exhausted_takes_the_later_reset():
    soon, later = NOW + timedelta(hours=1), NOW + timedelta(days=3)
    obs = classify(429, {"x-codex-primary-used-percent": "100",
                         "x-codex-primary-reset-at": str(int(soon.timestamp())),
                         "x-codex-secondary-used-percent": "99.9",
                         "x-codex-secondary-reset-after-seconds": str(int((later - NOW).total_seconds()))}, NOW)
    assert obs == QuotaExhausted(resets_at=later)


def test_k12_primary_exhausted_with_only_reset_after_seconds():
    obs = classify(429, {"x-codex-primary-used-percent": "100",
                         "x-codex-primary-reset-after-seconds": "600"}, NOW)
    assert obs == QuotaExhausted(resets_at=NOW + timedelta(seconds=600))


def test_k12_both_windows_under_the_threshold_is_still_a_short_rate_limit():
    obs = classify(429, {"x-codex-primary-used-percent": "50", "x-codex-secondary-used-percent": "99",
                         "x-codex-secondary-reset-after-seconds": "600", "retry-after": "12"}, NOW)
    assert obs == ShortRateLimit(retry_after_seconds=12.0)


@pytest.mark.parametrize("headers", [
    {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-at": "inf"},
    {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-at": "1e300"},
    {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-after-seconds": "nan"},
    {"x-codex-primary-used-percent": "100", "x-codex-primary-reset-after-seconds": "1e300"},
    {"x-codex-secondary-used-percent": "inf", "x-codex-secondary-reset-at": "-inf"},
    {"x-codex-primary-used-percent": "nan", "retry-after": "nan"},
    {"x-codex-primary-used-percent": "10", "retry-after": "inf"},
])
def test_k12_non_finite_values_never_raise(headers):
    for status in (200, 429, 503):
        classify(status, headers, NOW)
    obs = classify(429, headers, NOW)
    assert isinstance(obs, (QuotaExhausted, ShortRateLimit))
    if isinstance(obs, ShortRateLimit):
        assert obs.retry_after_seconds is None
    assert parse_credits({"x-codex-credits-balance": "nan"}).balance is None


def test_k12_a_200_with_finite_headers_is_unchanged():
    obs = classify(200, {"x-codex-primary-used-percent": "43", "x-codex-primary-window-minutes": "300",
                         "x-codex-secondary-used-percent": "12", "x-codex-secondary-window-minutes": "10080"}, NOW)
    assert isinstance(obs, UsageSnapshot) and obs.percent == 43.0 and obs.percent_7d == 12.0


def test_k12_a_secondary_exhausted_codex_account_is_parked_on_the_translated_path(pool_env, monkeypatch):
    weekly = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    headers = {"x-codex-primary-used-percent": "10", "x-codex-primary-window-minutes": "300",
               "x-codex-secondary-used-percent": "100", "x-codex-secondary-window-minutes": "10080",
               "x-codex-secondary-reset-at": str(int(weekly.timestamp()))}
    save_pool(Pool(profiles=[_codex("c1")]))

    def fake_run(profile, credential, body, timeout=120, parity=None, context=None, **kw):
        return OpenAIBridgeResult(status=429, headers=dict(headers), body_chunks=iter([b'{"error":{}}']))

    monkeypatch.setattr(bridge_module, "run", fake_run)
    gw = Gateway(transport=_no_transport)
    gw.handle("POST", "/v1/messages", {}, b'{"stream":true}')

    rt = gw.runtime_snapshot()["c1"]
    assert rt.state == ProfileState.EXHAUSTED
    assert rt.resets_at == weekly


def test_k12_a_secondary_exhausted_account_on_the_ingress_is_parked_and_its_429_relayed(pool_env, monkeypatch):
    weekly = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=3)
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [FakeHTTPResponse(429, {
        "content-type": "application/json", "x-codex-primary-used-percent": "10",
        "x-codex-secondary-used-percent": "100", "x-codex-secondary-reset-at": str(int(weekly.timestamp()))},
        b'{"error":{"type":"usage_limit_reached"}}')]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 429 and result.error is None      # quota: relayed, not a 503
    _drain(result)
    assert gw._runtime["c1"].state == ProfileState.EXHAUSTED
    assert gw._runtime["c1"].resets_at == weekly
