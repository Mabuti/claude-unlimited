"""OpenAI-shaped ingress, fifth round of fixes (L1-L2). Same harness as
test_gateway_openai_ingress.py: the Gateway and openai_bridge run for real,
only the HTTPS connection is faked. Every state path is redirected to
tmp_path (the pool_env fixture plus conftest's autouse isolation)."""

import itertools

import pytest

import claude_unlimited.gateway as gateway_module
from claude_unlimited.config import Pool, save_pool
from claude_unlimited.gateway import Gateway
from claude_unlimited.router import ProfileState

from test_gateway_openai_ingress import (  # noqa: F401 - pool_env is a fixture
    BODY, MOVE_BODY, SSE, FakeHTTPResponse, FakeSecretStore, Upstream, _codex, _codex_cred, _drain, _in,
    _no_transport, _quota_429, _session, _set_state, _status, _tokens, pool_env,
)

_SSE_HEADERS = {"Content-Type": "text/event-stream"}
_BALANCED = ({}, set(), {})


def _counts(gw):
    return dict(gw._in_flight_count), set(gw._in_flight), dict(gw._in_flight_since)


def _short_429(headers=None):
    base = {"content-type": "application/json", "x-codex-primary-used-percent": "10",
            "x-codex-primary-window-minutes": "300"}
    base.update(headers or {})
    return FakeHTTPResponse(429, base, b'{"error":{"type":"rate_limit_exceeded","message":"slow down"}}')


def _ok():
    return _status(200, _SSE_HEADERS, SSE)


def _two():
    save_pool(Pool(profiles=[_codex("c1"), _codex("c2", priority=2)]))


def _three(monkeypatch):
    ids = ["c1", "c2", "c3"]
    monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore(
        {pid: _codex_cred(f"tok-{pid}", f"acct-{pid}") for pid in ids}))
    save_pool(Pool(profiles=[_codex(pid, priority=i) for i, pid in enumerate(ids, start=1)]))


def _moved_off_cooling_home(gw, home="c1"):
    """The session is mapped to `home`, which is only briefly cooling down,
    so routing moves it to the next eligible account first."""
    gw._openai_sessions["s1"] = home
    _set_state(gw, home, ProfileState.COOLDOWN, cooldown_until=_in(1))


# ---- L1: a 429 on the account a session moved to goes back to its own account -------

@pytest.mark.parametrize("answer", [lambda: _short_429({"retry-after": "30"}), _quota_429],
                         ids=["short_429", "quota_429"])
def test_l1_a_429_after_a_move_goes_back_to_the_sessions_own_cooling_account(pool_env, monkeypatch, answer):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_ok()], "tok-c2": [answer()]})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)

    result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts-c1"}), MOVE_BODY)

    assert result.status == 200 and result.profile_id == "c1"
    assert _drain(result) == SSE
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert up.requests[0]["body"] != MOVE_BODY                   # c2 was a move: stripped
    assert "x-codex-turn-state" not in up.requests[0]["headers"]
    assert up.requests[1]["body"] == MOVE_BODY                   # back home: not a move, nothing stripped
    assert up.requests[1]["headers"]["x-codex-turn-state"] == "ts-c1"
    assert gw._openai_sessions == {"s1": "c1"}
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l1_a_429_with_no_session_tries_the_soonest_cooling_account(pool_env, monkeypatch):
    _three(monkeypatch)
    up = Upstream(monkeypatch, {"tok-c1": [], "tok-c2": [_short_429()], "tok-c3": [_ok()]})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c1", ProfileState.COOLDOWN, cooldown_until=_in(40))
    _set_state(gw, "c3", ProfileState.COOLDOWN, cooldown_until=_in(2))      # ends sooner

    result = gw.handle("POST", "/v1/responses", {}, BODY)

    assert result.status == 200 and result.profile_id == "c3"
    _drain(result)
    assert _tokens(up) == ["tok-c2", "tok-c3"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l1_the_last_resort_answering_a_short_429_too_is_the_local_503(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "5"})],
                                "tok-c2": [_short_429({"retry-after": "30"})]})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)

    result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "5"                  # the last answer's own wait
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


# Answer precedence 1: a short 429 from any attempt wins over a quota 429 —
# the short-limited account recovers in seconds, and the Codex CLI retries the
# local 503 but stops on a relayed 429.
def test_l1_a_later_short_429_outranks_a_quota_429(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "5"})], "tok-c2": [_quota_429()]})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)

    result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "5"
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l1_a_quota_429_on_the_last_resort_yields_to_the_earlier_short_429(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_short_429({"retry-after": "30"})]})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)

    result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "30"
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l1_the_last_resort_after_a_429_is_used_once(pool_env, monkeypatch):
    _three(monkeypatch)
    up = Upstream(monkeypatch, {"tok-c1": [_short_429()], "tok-c2": [_short_429()], "tok-c3": []})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)
    _set_state(gw, "c3", ProfileState.COOLDOWN, cooldown_until=_in(1))

    result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)

    assert result.status == 503
    assert _tokens(up) == ["tok-c2", "tok-c1"]                   # c3 (also cooling) is never tried
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l1_a_pinned_short_429_still_never_leaves_its_account(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_short_429()], "tok-c2": [_ok()]})
    gw = Gateway(transport=_no_transport)
    _set_state(gw, "c2", ProfileState.COOLDOWN, cooldown_until=_in(1))

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY, forced_profile_id="c1")

    assert result.status == 503
    assert _tokens(up) == ["tok-c1"]
    assert _counts(gw) == _BALANCED


_BEHAVIOURS = {
    "ok": lambda: [_ok()],
    "5xx": lambda: [_status(503, body=b"busy")],
    "net": lambda: [OSError("down")],
    "q429": lambda: [_quota_429()],
    "s429": lambda: [_short_429()],
    "401": lambda: [_status(401)],
    "enc5xx": lambda: [_status(400, body=b'{"error":{"message":"Encrypted content could not be decrypted."}}'),
                       _status(503, body=b"busy")],
}


def test_l1_every_combination_stays_within_the_budget_and_balanced(pool_env, monkeypatch):
    """Four accounts, the session's home (c1) cooling: every combination of
    per-account behaviour stays within MAX_ROTATION_ATTEMPTS upstream
    requests, releases every in-flight slot and closes every connection."""
    ids = ["c1", "c2", "c3", "c4"]
    worst = 0
    for i, combo in enumerate(itertools.product(list(_BEHAVIOURS), repeat=4)):
        monkeypatch.setattr(gateway_module.runtime_state, "RUNTIME_STATE_FILE", pool_env / f"rs{i}.json")
        monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore(
            {pid: _codex_cred(f"tok-{pid}", f"acct-{pid}") for pid in ids}))
        save_pool(Pool(profiles=[_codex(pid, priority=n) for n, pid in enumerate(ids, start=1)]))
        up = Upstream(monkeypatch, {f"tok-{pid}": _BEHAVIOURS[b]() for pid, b in zip(ids, combo)})
        gw = Gateway(transport=_no_transport)
        _moved_off_cooling_home(gw)
        result = gw.handle("POST", "/v1/responses", _session("s1", **{"x-codex-turn-state": "ts"}), MOVE_BODY)
        if result.body_chunks is not None:
            _drain(result)
        worst = max(worst, len(up.requests))
        assert len(up.requests) <= gateway_module.MAX_ROTATION_ATTEMPTS, (combo, _tokens(up))
        assert _counts(gw) == _BALANCED, combo
        assert all(c.closed for c in up.conns), combo
    assert worst == gateway_module.MAX_ROTATION_ATTEMPTS


# ---- L2: a later network failure does not hide a real quota answer -----------------

def test_l2_a_saved_quota_429_then_a_network_failure_is_relayed(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 429 and result.error is None and result.profile_id == "c1"
    assert b"usage_limit_reached" in _drain(result)
    assert "x-codex-primary-reset-at" in result.headers             # when it resets
    assert gw._runtime["c1"].state == ProfileState.EXHAUSTED
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l2_a_short_429_outranks_a_quota_429_and_a_network_failure(pool_env, monkeypatch):
    _three(monkeypatch)
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_short_429()],
                                "tok-c3": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert _tokens(up) == ["tok-c1", "tok-c2", "tok-c3"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_l2_a_saved_short_429_then_a_network_failure_is_the_local_503(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_short_429()], "tok-c2": [OSError("no route to host")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert _tokens(up) == ["tok-c1", "tok-c2"]
    assert _counts(gw) == _BALANCED


# ---- Precedence at the final attempt, not only after the loop --------------------------

@pytest.mark.parametrize("home_answer", [lambda: _status(401), lambda: _status(503, body=b"busy")],
                         ids=["401", "5xx"])
def test_a_short_429_then_a_failing_last_resort_is_still_the_local_503(pool_env, monkeypatch, home_answer):
    _two()
    up = Upstream(monkeypatch, {"tok-c2": [_short_429({"retry-after": "8"})], "tok-c1": [home_answer()]})
    gw = Gateway(transport=_no_transport)
    _moved_off_cooling_home(gw)

    result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "8"
    assert _tokens(up) == ["tok-c2", "tok-c1"]
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


@pytest.mark.parametrize("last", [lambda: _status(401), lambda: _status(503, body=b"busy")], ids=["401", "5xx"])
def test_a_short_429_then_a_failing_final_account_is_still_the_local_503(pool_env, monkeypatch, last):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_short_429({"retry-after": "7"})], "tok-c2": [last()]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error == gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE
    assert result.headers["retry-after"] == "7"
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_a_quota_429_outranks_a_final_401(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_status(401)]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 429 and result.error is None and result.profile_id == "c1"
    assert b"usage_limit_reached" in _drain(result)
    assert _counts(gw) == _BALANCED
    assert all(c.closed for c in up.conns)


def test_a_final_5xx_after_a_quota_429_is_relayed(pool_env, monkeypatch):
    _two()
    up = Upstream(monkeypatch, {"tok-c1": [_quota_429()], "tok-c2": [_status(503, body=b"busy")]})
    gw = Gateway(transport=_no_transport)

    result = gw.handle("POST", "/v1/responses", _session("s1"), BODY)

    assert result.status == 503 and result.error is None and result.profile_id == "c2"
    assert _drain(result) == b"busy"
    assert _counts(gw) == _BALANCED


def test_answer_precedence_holds_in_every_combination(pool_env, monkeypatch):
    """Four accounts, session home (c1) cooling. When the request fails: any
    short 429 among the answers -> the local 503; otherwise any quota 429 with
    a final outcome that is not a 5xx -> that quota 429 relayed."""
    ids = ["c1", "c2", "c3", "c4"]
    behaviours = ["ok", "5xx", "net", "q429", "s429", "401"]
    for i, combo in enumerate(itertools.product(behaviours, repeat=4)):
        monkeypatch.setattr(gateway_module.runtime_state, "RUNTIME_STATE_FILE", pool_env / f"rp{i}.json")
        monkeypatch.setattr(gateway_module, "secret_store", FakeSecretStore(
            {pid: _codex_cred(f"tok-{pid}", f"acct-{pid}") for pid in ids}))
        save_pool(Pool(profiles=[_codex(pid, priority=n) for n, pid in enumerate(ids, start=1)]))
        by_token = {f"tok-{pid}": b for pid, b in zip(ids, combo)}
        up = Upstream(monkeypatch, {t: _BEHAVIOURS[b]() for t, b in by_token.items()})
        gw = Gateway(transport=_no_transport)
        _moved_off_cooling_home(gw)
        result = gw.handle("POST", "/v1/responses", _session("s1"), MOVE_BODY)
        # What upstream actually answered, in order (a network failure is no answer).
        seen = [by_token[t] for t in _tokens(up) if by_token[t] != "net"]
        if result.status != 200:
            if "s429" in seen:
                assert (result.status, result.error) == (503, gateway_module.OPENAI_ERROR_CODEX_UNAVAILABLE), combo
            elif "q429" in seen and seen[-1] not in ("5xx", "ok"):
                assert result.status == 429 and result.error is None, combo
        if result.body_chunks is not None:
            _drain(result)
        assert _counts(gw) == _BALANCED, combo
        assert all(c.closed for c in up.conns), combo
