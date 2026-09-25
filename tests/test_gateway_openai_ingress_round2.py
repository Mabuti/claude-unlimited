"""OpenAI-shaped ingress, second round of fixes (G1-G7; G8 lives in
test_cli_codex.py). Same harness as test_gateway_openai_ingress.py: the
Gateway and openai_bridge run for real, only the HTTPS connection is faked."""

import json
import threading
import time
from datetime import datetime, timezone

import pytest

import claude_unlimited.gateway as gateway_module
import claude_unlimited.openai_bridge as bridge_module
from claude_unlimited.config import Pool, save_pool
from claude_unlimited.gateway import Gateway, is_openai_ingress
from claude_unlimited.router import ProfileState

from test_gateway_openai_ingress import (  # noqa: F401 - pool_env is a fixture
    BODY, CODEX_HEADERS, OK_HEADERS, REASONING_BODY, SSE, FakeHTTPResponse, Upstream, _codex, _drain,
    _no_transport, _oauth, _ok, _session, _status, _tokens, pool_env,
)


def _turn_state_sent(request):
    return {k.lower(): v for k, v in request["headers"].items()}.get("x-codex-turn-state")


class _BreakingStream(FakeHTTPResponse):
    """A 200 stream that yields `body`, then fails the next read."""

    def read(self, n=None):
        if self._pos >= len(self._body):
            raise ConnectionResetError("upstream reset mid-stream")
        return super().read(n)


_PARTIAL_SSE = b"event: response.created\ndata: {\"type\":\"response.created\"}\n\n"


# ---- G1: the finalizer never blocks on the gateway lock ------------------------------

def test_g1_finalizing_an_unreleased_body_on_a_lock_holding_thread_does_not_deadlock(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert gw.serving_now_ids() == {"c1"}
    holder = [result.body_chunks]
    del result
    finalized = threading.Event()

    def drop_last_reference_under_the_lock():
        with gw._lock:
            holder.clear()          # the body's __del__ runs right here, lock held
            finalized.set()

    worker = threading.Thread(target=drop_last_reference_under_the_lock, daemon=True)
    worker.start()
    worker.join(timeout=3)
    # Checked before anything that takes gw._lock: on the deadlocking code the
    # worker still holds it, and the test must fail rather than hang.
    assert finalized.is_set() and not worker.is_alive(), "finalizer blocked on the gateway lock"

    deadline = time.monotonic() + 3
    while gw.serving_now_ids() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert gw.serving_now_ids() == set()     # released once the lock was free
    assert up.conns[0].closed


def test_g1_finalizing_with_the_lock_free_releases_inline(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    helpers = []
    real_thread = threading.Thread
    monkeypatch.setattr(gateway_module.threading, "Thread",
                        lambda *a, **kw: helpers.append(kw.get("name")) or real_thread(*a, **kw))

    del result

    assert gw.serving_now_ids() == set()          # released synchronously, on this thread
    assert "cu-in-flight-release" not in helpers   # no helper thread was needed


# ---- G2: a percent-encoded /v1/responses path is still the Codex ingress -------------

@pytest.mark.parametrize("path,backend", [
    ("/v1/responses%2Fcompact", "/backend-api/codex/responses/compact"),
    ("/v1/responses%2fcompact", "/backend-api/codex/responses/compact"),
    ("/v1%2Fresponses", "/backend-api/codex/responses"),
    ("/v1/%72esponses%2Fcompact?x=1", "/backend-api/codex/responses/compact"),
    ("/v1/responses%2F", "/backend-api/codex/responses"),
])
def test_g2_percent_encoded_paths_are_served_by_codex_with_the_decoded_suffix(pool_env, monkeypatch,
                                                                              path, backend):
    save_pool(Pool(profiles=[_oauth("o"), _codex("c1", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    assert is_openai_ingress("POST", path)
    result = gw.handle("POST", path, dict(CODEX_HEADERS), BODY)

    assert result.status == 200 and result.profile_id == "c1"
    assert up.requests[0]["path"] == backend      # never an encoded suffix
    _drain(result)


@pytest.mark.parametrize("path", [
    "/v1/responses%252Fcompact",          # double-encoded: refused, never relayed
    "/v1/responses%2F..%2Fadmin",
    "/v1/responses%2Finput_items",
    "/v1/responses%2Fcompact%2Fextra",
    "/v1/responses%2F%2E%2E",
])
def test_g2_encoded_paths_with_a_bad_suffix_are_refused_locally(pool_env, monkeypatch, path):
    save_pool(Pool(profiles=[_oauth("o"), _codex("c1", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)

    assert is_openai_ingress("POST", path)
    result = gw.handle("POST", path, dict(CODEX_HEADERS), BODY)

    assert result.status == 400
    assert result.error == gateway_module.OPENAI_ERROR_BAD_REQUEST
    assert up.conns == []


def test_g2_encoded_path_never_reaches_a_claude_account(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth("o")]))
    gw = Gateway(transport=_no_transport)   # raises if the Anthropic transport is used

    result = gw.handle("POST", "/v1/responses%2Fcompact", dict(CODEX_HEADERS), BODY)

    assert result.status == 400
    assert result.error == gateway_module.OPENAI_ERROR_NO_CODEX_PROFILE
    assert result.profile_id is None


@pytest.mark.parametrize("path", ["/v1/messages", "/v1/messages%3Fbeta=true", "/v1/responsesx",
                                  "/v1/responses%78"])
def test_g2_other_paths_are_still_not_the_ingress(path):
    assert is_openai_ingress("POST", path) is False


# ---- G3: in-request failover applies the move strip ----------------------------------

@pytest.mark.parametrize("first", [lambda: OSError("connection reset"), lambda: _status(503)])
def test_g3_failover_without_a_session_map_entry_strips_account_bound_state(pool_env, monkeypatch, first):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [first()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    assert gw._openai_sessions == {}          # e.g. just after a daemon restart

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}),
                       REASONING_BODY)

    assert result.status == 200 and result.profile_id == "c2"
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    to_c1, to_c2 = up.requests
    assert to_c1["body"] == REASONING_BODY                  # first attempt: byte-identical
    assert _turn_state_sent(to_c1) == "ts-c1"
    assert to_c2["body"] == bridge_module.without_foreign_reasoning(REASONING_BODY)
    assert b"encrypted_content" not in to_c2["body"]
    assert _turn_state_sent(to_c2) is None
    _drain(result)


def test_g3_failover_with_nothing_to_strip_keeps_the_body_byte_identical(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(502)], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))

    assert [r["body"] for r in up.requests] == [BODY, BODY]


# ---- G4: any 400 mentioning encryption triggers the single retry ---------------------

class _ScriptedConnection:
    def __init__(self, requests, responses):
        self._requests, self._responses = requests, responses

    def request(self, method, path, body=None, headers=None):
        self._requests.append({"body": body, "headers": dict(headers or {})})

    def getresponse(self):
        return self._responses.pop(0)

    def close(self):
        pass


@pytest.mark.parametrize("message", [
    "encrypted content is invalid",
    "Could not decrypt the reasoning item.",
    '{"code":"invalid_encrypted_content"}',
    "ENCRYPTED payload rejected",
])
def test_g4_any_400_mentioning_encryption_is_retried_once_without_it(monkeypatch, message):
    requests = []
    responses = [FakeHTTPResponse(400, {"content-type": "application/json"},
                                  json.dumps({"error": {"message": message}}).encode()),
                 FakeHTTPResponse(200, {"content-type": "application/json"}, b"{}")]
    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection",
                        lambda *a, **k: _ScriptedConnection(requests, responses))

    result = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", REASONING_BODY, {})

    assert result.status == 200
    assert len(requests) == 2
    assert b"encrypted_content" not in requests[1]["body"]


def test_g4_a_400_that_does_not_mention_encryption_is_not_retried(monkeypatch):
    requests = []
    responses = [FakeHTTPResponse(400, {}, b'{"error":{"message":"reasoning summary is malformed"}}')]
    monkeypatch.setattr(bridge_module.http.client, "HTTPSConnection",
                        lambda *a, **k: _ScriptedConnection(requests, responses))

    result = bridge_module.run_passthrough(_codex("c1"), _cred_c1(), "POST", "", REASONING_BODY, {})

    assert result.status == 400 and len(requests) == 1


def _cred_c1():
    from test_gateway_openai_ingress import _codex_cred
    return _codex_cred("tok-c1", "acct-1")


# ---- G5: a stream that dies mid-response cools its account ---------------------------

def test_g5_mid_stream_failure_cools_the_account_and_the_reconnect_moves(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    breaking = _BreakingStream(200, {"Content-Type": "text/event-stream"}, _PARTIAL_SSE)
    up = Upstream(monkeypatch, {"tok-c1": [breaking], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    first = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert first.status == 200 and first.profile_id == "c1"
    assert _drain(first) == _PARTIAL_SSE               # nothing injected, the stream just ends

    assert gw._runtime["c1"].state == ProfileState.COOLDOWN
    assert gw._runtime["c1"].cooldown_until > datetime.now(timezone.utc)
    assert gw.serving_now_ids() == set()
    assert up.conns[0].closed

    again = gw.handle("POST", "/v1/responses", _session("s1"), BODY)   # the Codex CLI's reconnect
    assert again.profile_id == "c2"
    _drain(again)


def test_g5_a_failure_after_the_terminal_event_cools_nothing(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    breaking = _BreakingStream(200, {"Content-Type": "text/event-stream"}, SSE)
    Upstream(monkeypatch, {"tok-c1": [breaking]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    assert _drain(result) == SSE

    assert gw._runtime["c1"].state != ProfileState.COOLDOWN
    assert gw.serving_now_ids() == set()


def test_g5_a_clean_stream_and_a_client_that_leaves_cool_nothing(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_ok(), _BreakingStream(200, {"Content-Type": "text/event-stream"},
                                                            _PARTIAL_SSE)]})
    gw = Gateway(transport=_no_transport)

    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY))
    left = gw.handle("POST", "/v1/responses", _session("s1"), BODY)
    next(left.body_chunks)
    left.body_chunks.close()                            # the client went away

    assert gw._runtime["c1"].state == ProfileState.ELIGIBLE
    assert gw.serving_now_ids() == set()


def test_g5_a_pinned_session_is_not_cooled_by_a_mid_stream_failure(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_BreakingStream(200, {"Content-Type": "text/event-stream"},
                                                       _PARTIAL_SSE)]})
    gw = Gateway(transport=_no_transport)

    _drain(gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1"))

    assert gw._runtime["c1"].state == ProfileState.ELIGIBLE


# ---- G6: an unmapped 5xx fails over AND cools ----------------------------------------

@pytest.mark.parametrize("status", [504, 520, 521, 522, 523, 524])
def test_g6_unmapped_5xx_fails_over_and_cools_the_account(pool_env, monkeypatch, status):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_status(status)], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.profile_id == "c2"
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    assert gw._runtime["c1"].state == ProfileState.COOLDOWN
    _drain(result)


def test_g6_unmapped_5xx_honours_retry_after(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1")]))
    Upstream(monkeypatch, {"tok-c1": [_status(504, {"content-type": "application/json", "retry-after": "7"})]})
    gw = Gateway(transport=_no_transport)
    before = datetime.now(timezone.utc)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 504 and result.error is None    # the real answer, relayed
    rt = gw._runtime["c1"]
    assert rt.state == ProfileState.COOLDOWN
    assert 6 <= (rt.cooldown_until - before).total_seconds() <= 9


# ---- G7: the Dashboard's "Take over" reaches Codex traffic ---------------------------

def test_g7_a_taken_over_manual_only_codex_profile_is_servable(pool_env, monkeypatch):
    manual = _codex("c1")
    manual.automatic = False
    save_pool(Pool(profiles=[manual]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    assert gw.force_active("c1") is True

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 200 and result.profile_id == "c1"
    _drain(result)


def test_g7_take_over_wins_for_new_sessions_but_not_over_affinity(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c1": [_ok(), _ok()], "tok-c2": [_ok(), _ok()]})
    gw = Gateway(transport=_no_transport)
    _drain(gw.handle("POST", "/v1/responses", _session("old"), BODY))          # old session on c1

    assert gw.force_active("c2") is True
    fresh = gw.handle("POST", "/v1/responses", _session("new"), BODY)
    kept = gw.handle("POST", "/v1/responses", _session("old"), BODY)
    no_session = gw.handle("POST", "/v1/responses", {"content-type": "application/json"}, BODY)

    assert fresh.profile_id == "c2"
    assert kept.profile_id == "c1"                    # session affinity still wins
    assert no_session.profile_id == "c2"
    assert gw._openai_ingress_profile_id == "c2"
    assert _tokens(up) == ["tok-c1", "tok-c2", "tok-c1", "tok-c2"]
    for r in (fresh, kept, no_session):
        _drain(r)


def test_g7_a_take_over_of_a_claude_account_does_not_touch_codex_routing(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_oauth("o"), _codex("c1", priority=2), _codex("c2", priority=3)]))
    Upstream(monkeypatch, {"tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    assert gw.force_active("o") is True

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.profile_id == "c1"
    assert gw._manual_profile_id == "o"               # still standing for Claude traffic
    _drain(result)


def test_g7_a_taken_over_codex_profile_that_fails_still_fails_over(pool_env, monkeypatch):
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))
    up = Upstream(monkeypatch, {"tok-c2": [_status(503)], "tok-c1": [_ok()]})
    gw = Gateway(transport=_no_transport)
    assert gw.force_active("c2") is True

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.profile_id == "c1"
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    _drain(result)
