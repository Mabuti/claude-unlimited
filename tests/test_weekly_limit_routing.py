import os
import subprocess
import sys
import time
import types
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_unlimited import gateway, router, runtime_state
from claude_unlimited.config import Pool, Profile, Settings, save_pool
from claude_unlimited.observation import QuotaExhausted, ShortRateLimit, UsageSnapshot
from claude_unlimited.upstream import UpstreamResponse


NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)
FIVE_RESET = NOW + timedelta(hours=1)
WEEK_RESET = NOW + timedelta(days=6)
# Fixed base commit used by the parity reference fixtures.
PARITY_REFERENCE_COMMIT = "4d12232"


def _runtime(profile_id="a", **kwargs):
    kind = kwargs.pop("kind", "oauth")
    return router.ProfileRuntime(
        profile_id=profile_id, priority=1, switch_threshold=95.0,
        automatic=True, kind=kind, **kwargs,
    )


def _pool():
    return router.PoolSnapshot(profiles=[
        _runtime(),
        replace(_runtime("b", last_usage_percent=10.0), priority=2),
    ], current_profile_id="a")


def _usage(primary=0.0, weekly=100.0):
    return UsageSnapshot(
        percent=primary, resets_at=FIVE_RESET if primary is not None else None,
        confidence="measured", percent_7d=weekly,
        resets_at_7d=WEEK_RESET if weekly is not None else None,
    )


def test_weekly_spent_with_empty_5h_routes_to_healthy_peer():
    pool = router.observe(_pool(), "a", _usage(), NOW)

    assert pool.profiles[0].state == router.ProfileState.DRAINING
    assert router.routing_percent(pool.profiles[0]) == 100.0
    assert router.choose(pool, NOW).profile_id == "b"
    assert router.choose_for_new_branch(pool, NOW).profile_id == "b"


def test_exhausted_usage_read_keeps_weekly_hold():
    pool = router.observe(_pool(), "a", QuotaExhausted(FIVE_RESET), NOW)
    assert pool.profiles[0].state == router.ProfileState.EXHAUSTED

    pool = router.observe(pool, "a", _usage(), NOW)

    assert pool.profiles[0].state == router.ProfileState.DRAINING
    assert router.choose(pool, NOW).profile_id == "b"


def test_refusal_reset_preserves_live_weekly_hold():
    pool = router.observe(_pool(), "a", _usage(0.0, 100.0), NOW)
    pool = router.observe(pool, "a", QuotaExhausted(FIVE_RESET), NOW)
    refused = pool.profiles[0]
    assert refused.state == router.ProfileState.EXHAUSTED
    assert router.release_time(refused) == WEEK_RESET
    assert router.recover_expired_cooldowns(
        pool, FIVE_RESET - timedelta(seconds=1),
    ).profiles[0].state == router.ProfileState.EXHAUSTED

    later = FIVE_RESET + timedelta(seconds=1)
    pool = router.recover_expired_cooldowns(pool, later)
    held = pool.profiles[0]

    assert held.state == router.ProfileState.DRAINING
    assert held.last_usage_percent is None
    assert held.resets_at is None
    assert held.last_usage_percent_7d == 100.0
    assert held.resets_at_7d == WEEK_RESET
    assert router.choose(pool, later).profile_id == "b"
    assert router.choose_for_new_branch(pool, later).profile_id == "b"


def test_refusal_reset_releases_5h_hold_with_week_below_threshold():
    pool = router.observe(_pool(), "a", _usage(96.0, 40.0), NOW)
    pool = router.observe(pool, "a", QuotaExhausted(FIVE_RESET), NOW)
    assert pool.profiles[0].state == router.ProfileState.EXHAUSTED
    assert router.release_time(pool.profiles[0]) == FIVE_RESET

    later = FIVE_RESET + timedelta(seconds=1)
    pool = router.recover_expired_cooldowns(pool, later)
    released = pool.profiles[0]

    assert released.state == router.ProfileState.ELIGIBLE
    assert released.last_usage_percent is None
    assert released.resets_at is None
    assert released.last_usage_percent_7d == 40.0
    assert released.resets_at_7d == WEEK_RESET
    assert router.choose(pool, later).profile_id == "a"


def test_5h_only_success_keeps_unexpired_weekly_sample():
    pool = router.observe(_pool(), "a", _usage(), NOW)
    pool = router.observe(pool, "a", _usage(10.0, None), NOW + timedelta(seconds=1))
    held = pool.profiles[0]

    assert held.state == router.ProfileState.DRAINING
    assert held.last_usage_percent == 10.0
    assert held.last_usage_percent_7d == 100.0
    assert held.resets_at_7d == WEEK_RESET
    assert held.no_5h_window is False


@pytest.mark.parametrize("weekly, expected", [
    (94.0, router.ProfileState.ELIGIBLE),
    (95.0, router.ProfileState.DRAINING),
])
def test_fresh_weekly_read_replaces_hold_at_configured_threshold(weekly, expected):
    pool = router.observe(_pool(), "a", _usage(), NOW)
    pool = router.observe(pool, "a", _usage(0.0, weekly), NOW)

    assert pool.profiles[0].state == expected
    assert pool.profiles[0].last_usage_percent_7d == weekly


def test_weekly_hold_survives_5h_reset_until_weekly_reset():
    pool = router.observe(_pool(), "a", _usage(), NOW)
    pool = router.recover_expired_cooldowns(pool, FIVE_RESET)
    held = pool.profiles[0]

    assert held.state == router.ProfileState.DRAINING
    assert held.last_usage_percent is None
    assert held.resets_at is None
    assert held.last_usage_percent_7d == 100.0
    assert held.resets_at_7d == WEEK_RESET
    assert held.no_5h_window is False
    assert router.choose(pool, FIVE_RESET).profile_id == "b"

    released = router.recover_expired_cooldowns(pool, WEEK_RESET).profiles[0]
    assert released.state == router.ProfileState.ELIGIBLE
    assert released.last_usage_percent_7d is None
    assert released.resets_at_7d is None


def test_weekly_reset_does_not_release_a_live_5h_hold():
    pool = router.observe(_pool(), "a", replace(
        _usage(96.0, 100.0), resets_at=WEEK_RESET, resets_at_7d=FIVE_RESET,
    ), NOW)
    held = router.recover_expired_cooldowns(pool, FIVE_RESET).profiles[0]

    assert held.state == router.ProfileState.DRAINING
    assert held.last_usage_percent == 96.0
    assert held.resets_at == WEEK_RESET
    assert held.last_usage_percent_7d is None
    assert router.release_time(held) == WEEK_RESET


def test_expired_weekly_sample_is_not_kept_by_5h_only_success():
    pool = router.observe(_pool(), "a", _usage(), NOW)
    pool = router.observe(pool, "a", _usage(10.0, None), WEEK_RESET)

    assert pool.profiles[0].state == router.ProfileState.ELIGIBLE
    assert pool.profiles[0].last_usage_percent_7d is None
    assert pool.profiles[0].resets_at_7d is None


def test_expired_short_cooldown_returns_to_weekly_hold():
    pool = router.observe(_pool(), "a", _usage(), NOW)
    pool = router.observe(pool, "a", ShortRateLimit(30), NOW)
    assert pool.profiles[0].state == router.ProfileState.COOLDOWN

    later = NOW + timedelta(seconds=31)
    pool = router.recover_expired_cooldowns(pool, later)

    assert pool.profiles[0].state == router.ProfileState.DRAINING
    assert pool.profiles[0].cooldown_until is None
    assert router.choose(pool, later).profile_id == "b"
    assert router.choose_for_new_branch(pool, later).profile_id == "b"


@pytest.mark.parametrize("primary, weekly, expected", [
    (96.0, 40.0, router.ProfileState.DRAINING),
    (10.0, None, router.ProfileState.ELIGIBLE),
])
def test_5h_hold_and_usage_without_a_weekly_sample(primary, weekly, expected):
    pool = router.observe(_pool(), "a", _usage(primary, weekly), NOW)
    assert pool.profiles[0].state == expected

    released = router.recover_expired_cooldowns(pool, FIVE_RESET).profiles[0]
    assert released.state == router.ProfileState.ELIGIBLE
    assert released.last_usage_percent is None
    assert released.last_usage_percent_7d == weekly


@pytest.mark.parametrize("primary, weekly, reset_5h, reset_7d, release", [
    (0.0, 100.0, 10, 20, 20),
    (96.0, 100.0, 10, 20, 20),
    (96.0, 100.0, 20, 10, 20),
    (0.0, 100.0, 10, None, None),
    (96.0, 100.0, None, 20, None),
    (96.0, 100.0, 10, None, None),
    (96.0, 40.0, None, 20, None),
    (96.0, 40.0, 10, 20, 10),
])
def test_release_time_uses_only_holding_windows(primary, weekly, reset_5h, reset_7d, release):
    def deadline(minutes):
        return NOW + timedelta(minutes=minutes) if minutes is not None else None

    held = _runtime(
        state=router.ProfileState.DRAINING,
        last_usage_percent=primary, resets_at=deadline(reset_5h),
        last_usage_percent_7d=weekly, resets_at_7d=deadline(reset_7d),
    )
    expected = deadline(release)

    assert router.release_time(held) == expected
    assert router._reset_sort_key(held) == ((0, expected) if expected else (1, None))
    assert gateway._pool_retry_after_seconds(router.PoolSnapshot(profiles=[held]), NOW) == (
        release * 60 if release is not None else None
    )


def test_draining_fallback_orders_by_weekly_release():
    pool = router.PoolSnapshot(profiles=[
        _runtime(state=router.ProfileState.DRAINING, last_usage_percent=0.0,
                 resets_at=NOW + timedelta(minutes=1),
                 last_usage_percent_7d=100.0, resets_at_7d=WEEK_RESET),
        _runtime("b", state=router.ProfileState.DRAINING,
                 last_usage_percent=96.0, resets_at=FIVE_RESET),
    ])

    assert router.choose(pool, NOW).profile_id == "b"
    assert router.choose(pool, NOW).reason == "drained_fallback"


def test_credit_state_from_usage_read_keeps_codex_exception():
    runtime = _runtime(
        kind="codex", may_spend_credits=True, credits_has=True,
    )

    observed = router.observe(
        router.PoolSnapshot(profiles=[runtime]), "a", _usage(), NOW,
    ).profiles[0]

    assert observed.state == router.ProfileState.ELIGIBLE


def _credit_funded_codex_runtime(implementation, state):
    kwargs = {"kind": "codex"} if implementation is router else {}
    return implementation.ProfileRuntime(
        profile_id="a", priority=1, switch_threshold=95.0, automatic=True,
        state=state, last_usage_percent=100.0, resets_at=FIVE_RESET,
        may_spend_credits=True, credits_has=True, **kwargs,
    )


@pytest.mark.parametrize("recovery", ["refill", "cooldown_expiry"])
def test_credit_funded_codex_recovery_matches_main(main_router, recovery):
    if recovery == "refill":
        initial_state = router.ProfileState.EXHAUSTED
        when = FIVE_RESET + timedelta(seconds=1)
    else:
        initial_state = router.ProfileState.COOLDOWN
        when = NOW + timedelta(seconds=31)

    def recover(implementation):
        runtime = _credit_funded_codex_runtime(implementation, initial_state)
        if recovery == "cooldown_expiry":
            runtime = replace(runtime, cooldown_until=NOW + timedelta(seconds=30))
        return implementation.recover_expired_cooldowns(
            implementation.PoolSnapshot(profiles=[runtime]), when,
        ).profiles[0]

    current = recover(router)
    reference = recover(main_router)

    assert current.state.value == reference.state.value == "eligible"


@pytest.mark.parametrize("recovery", ["restart", "reenable"])
def test_credit_funded_codex_gateway_recovery_matches_main(
        gateway_env, monkeypatch, main_gateway, recovery):
    def recover(implementation):
        profile = Profile(
            id="a", name="A", kind="codex", switch_threshold=95.0,
            automatic=True, enabled=recovery != "reenable",
        )
        settings = Settings(codex_spend_credits=True)
        save_pool(Pool(profiles=[profile], settings=settings))
        runtime_state.save(None, {"a": {
            "last_usage_percent": 100.0,
            "resets_at": FIVE_RESET.isoformat(),
            "no_5h_window": False,
            "credits_has": True,
        }})
        with monkeypatch.context() as patch:
            patch.setattr(implementation, "datetime", gateway.datetime)
            patch.setattr(implementation, "secret_store", gateway.secret_store)
            patch.setattr(implementation.Gateway, "_schedule_credential_checks", lambda *args: None)
            gw = implementation.Gateway(transport=lambda _: _response())
            if recovery == "reenable":
                assert gw.runtime_snapshot()["a"].state.value == "disabled"
                save_pool(Pool(profiles=[replace(profile, enabled=True)], settings=settings))
            return gw.runtime_snapshot()["a"]

    current = recover(gateway)
    reference = recover(main_gateway)

    assert current.state.value == reference.state.value == (
        "draining" if recovery == "restart" else "eligible"
    )


@pytest.mark.parametrize("weekly_reset, expected", [
    (WEEK_RESET, None),
    (None, None),
    (NOW - timedelta(seconds=1), "a"),
])
def test_drained_fallback_excludes_only_a_live_spent_weekly_window(weekly_reset, expected):
    runtime = _runtime(
        state=router.ProfileState.DRAINING,
        last_usage_percent=0.0, resets_at=FIVE_RESET,
        last_usage_percent_7d=100.0, resets_at_7d=weekly_reset,
    )

    decision = router.choose(router.PoolSnapshot(profiles=[runtime]), NOW)

    assert decision.profile_id == expected
    assert decision.reason == ("drained_fallback" if expected else "no_eligible_profile")


def test_drained_fallback_does_not_use_the_5h_figure_as_a_weekly_ban():
    runtime = _runtime(
        state=router.ProfileState.DRAINING,
        last_usage_percent=100.0, resets_at=FIVE_RESET,
        last_usage_percent_7d=99.0, resets_at_7d=WEEK_RESET,
    )

    assert router.choose(router.PoolSnapshot(profiles=[runtime]), NOW).profile_id == "a"


def test_drained_fallback_skips_spent_weekly_account_for_5h_drained_peer():
    pool = router.PoolSnapshot(profiles=[
        _runtime(
            state=router.ProfileState.DRAINING,
            last_usage_percent=0.0, resets_at=FIVE_RESET,
            last_usage_percent_7d=100.0, resets_at_7d=WEEK_RESET,
        ),
        replace(_runtime(
            "b", state=router.ProfileState.DRAINING,
            last_usage_percent=96.0, resets_at=FIVE_RESET,
        ), priority=2),
    ])

    assert router.choose(pool, NOW).profile_id == "b"


@pytest.mark.parametrize("kind", ["codex", "api"])
def test_drained_fallback_for_other_kinds_matches_reference(main_router, kind):
    def choice(implementation):
        kwargs = {"kind": kind} if implementation is router else {}
        runtime = implementation.ProfileRuntime(
            profile_id="a", priority=1, switch_threshold=95.0, automatic=True,
            state=implementation.ProfileState.DRAINING,
            last_usage_percent=0.0, resets_at=FIVE_RESET,
            last_usage_percent_7d=100.0, resets_at_7d=WEEK_RESET, **kwargs,
        )
        return implementation.choose(
            implementation.PoolSnapshot(profiles=[runtime]), NOW,
        ).profile_id

    assert choice(router) == choice(main_router) == "a"


@pytest.fixture
def gateway_env(monkeypatch):
    current_time = [NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_time[0].astimezone(tz)

    monkeypatch.setattr(gateway, "datetime", Clock)
    monkeypatch.setattr(gateway, "secret_store", types.SimpleNamespace(get_token=lambda _: "test-token"))
    monkeypatch.setattr(gateway.Gateway, "_schedule_credential_checks", lambda *args: None)
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", priority=1, switch_threshold=95.0, automatic=True),
        Profile(id="b", name="B", kind="oauth", priority=2, switch_threshold=95.0, automatic=True),
    ]))
    return current_time


def _response(headers=None):
    return UpstreamResponse(
        status=200, headers=headers or {}, body_chunks=iter([b"ok"]),
        connection=types.SimpleNamespace(close=lambda: None),
    )


def _weekly_headers():
    return {
        "anthropic-ratelimit-unified-5h-utilization": "0.0",
        "anthropic-ratelimit-unified-5h-reset": str(FIVE_RESET.timestamp()),
        "anthropic-ratelimit-unified-7d-utilization": "1.0",
        "anthropic-ratelimit-unified-7d-reset": str(WEEK_RESET.timestamp()),
    }


@pytest.mark.parametrize("last_usage_percent", [None, 10.0], ids=["no-5h-sample", "below-threshold-5h-sample"])
def test_unexpired_provider_refusal_survives_restart_without_weekly_sample(
        gateway_env, last_usage_percent):
    refusal_reset = NOW + timedelta(hours=2)
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", switch_threshold=95.0, automatic=True),
    ]))
    runtime_state.save(None, {"a": {
        "state": router.ProfileState.EXHAUSTED.value,
        "resets_at": refusal_reset.isoformat(),
        "last_usage_percent": last_usage_percent,
        "no_5h_window": False,
    }})

    restarted = gateway.Gateway(transport=lambda _: _response())
    restored = restarted.runtime_snapshot()["a"]

    assert restored.state == router.ProfileState.EXHAUSTED
    assert restored.resets_at == refusal_reset


@pytest.mark.parametrize("kind, no_5h_window, primary, weekly", [
    pytest.param("oauth", True, None, 100.0, id="oauth-no-5h-weekly-high"),
    pytest.param("codex", False, 100.0, 0.0, id="codex-5h-high"),
    pytest.param("api", False, 100.0, 0.0, id="api-5h-high"),
])
def test_unexpired_provider_refusal_outside_gate_rederives_like_reference(
        gateway_env, main_gateway, monkeypatch, kind, no_5h_window, primary, weekly):
    def restart(implementation):
        profile = Profile(
            id="a", name="A", kind=kind, switch_threshold=95.0, automatic=True,
        )
        save_pool(Pool(profiles=[profile]))
        runtime_state.save(None, {"a": {
            "state": router.ProfileState.EXHAUSTED.value,
            "resets_at": (FIVE_RESET + timedelta(hours=1)).isoformat(),
            "last_usage_percent": primary,
            "resets_at_7d": WEEK_RESET.isoformat(),
            "last_usage_percent_7d": weekly,
            "no_5h_window": no_5h_window,
        }})
        with monkeypatch.context() as patch:
            patch.setattr(implementation, "datetime", gateway.datetime)
            patch.setattr(implementation, "secret_store", gateway.secret_store)
            patch.setattr(implementation.Gateway, "_schedule_credential_checks", lambda *args: None)
            return implementation.Gateway(transport=lambda _: _response()).runtime_snapshot()["a"]

    current = restart(gateway)
    reference = restart(main_gateway)

    assert current.state.value == reference.state.value == "draining"
    assert current.resets_at == reference.resets_at


@pytest.mark.parametrize("resets_at", [
    NOW - timedelta(seconds=1), None, "not-a-deadline",
], ids=["past", "missing", "malformed"])
def test_expired_or_unknown_provider_refusal_is_rederived_on_restart(gateway_env, resets_at):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind="oauth", switch_threshold=95.0, automatic=True),
    ]))
    runtime_state.save(None, {"a": {
        "state": router.ProfileState.EXHAUSTED.value,
        "resets_at": resets_at.isoformat() if isinstance(resets_at, datetime) else resets_at,
        "last_usage_percent": 10.0,
        "no_5h_window": False,
    }})

    restarted = gateway.Gateway(transport=lambda _: _response())
    restored = restarted.runtime_snapshot()["a"]

    assert restored.state == router.ProfileState.ELIGIBLE
    assert restored.resets_at is None


@pytest.mark.parametrize("primary, weekly, expected", [
    (0.0, 100.0, router.ProfileState.DRAINING),
    (10.0, 20.0, router.ProfileState.ELIGIBLE),
], ids=["weekly-held", "below-threshold"])
def test_reenable_reapplies_oauth_five_hour_weekly_hold(
        gateway_env, primary, weekly, expected):
    profile = Profile(
        id="a", name="A", kind="oauth", switch_threshold=95.0,
        automatic=True, enabled=False,
    )
    save_pool(Pool(profiles=[profile]))
    runtime_state.save(None, {"a": {
        "last_usage_percent": primary,
        "resets_at": FIVE_RESET.isoformat(),
        "last_usage_percent_7d": weekly,
        "resets_at_7d": WEEK_RESET.isoformat(),
        "no_5h_window": False,
    }})
    gw = gateway.Gateway(transport=lambda _: _response())
    assert gw.runtime_snapshot()["a"].state == router.ProfileState.DISABLED

    save_pool(Pool(profiles=[replace(profile, enabled=True)]))
    reenabled = gw.runtime_snapshot()["a"]

    assert reenabled.state == expected


@pytest.mark.parametrize("kind, no_5h_window, primary, weekly", [
    pytest.param("oauth", True, None, 100.0, id="oauth-no-5h-weekly-high"),
    pytest.param("codex", False, 100.0, 0.0, id="codex-5h-high"),
    pytest.param("api", False, 100.0, 0.0, id="api-5h-high"),
])
def test_reenable_outside_oauth_five_hour_gate_matches_main(
        gateway_env, main_gateway, monkeypatch, kind, no_5h_window, primary, weekly):
    def reenable(implementation):
        profile = Profile(
            id="a", name="A", kind=kind, switch_threshold=95.0,
            automatic=True, enabled=False,
        )
        settings = Settings(codex_spend_credits=kind == "codex")
        save_pool(Pool(profiles=[profile], settings=settings))
        runtime_state.save(None, {"a": {
            "last_usage_percent": primary,
            "resets_at": FIVE_RESET.isoformat() if primary is not None else None,
            "last_usage_percent_7d": weekly,
            "resets_at_7d": WEEK_RESET.isoformat(),
            "no_5h_window": no_5h_window,
        }})
        with monkeypatch.context() as patch:
            patch.setattr(implementation, "datetime", gateway.datetime)
            patch.setattr(implementation, "secret_store", gateway.secret_store)
            patch.setattr(implementation.Gateway, "_schedule_credential_checks", lambda *args: None)
            gw = implementation.Gateway(transport=lambda _: _response())
            assert gw.runtime_snapshot()["a"].state.value == "disabled"
            save_pool(Pool(profiles=[replace(profile, enabled=True)], settings=settings))
            return gw.runtime_snapshot()["a"]

    current = reenable(gateway)
    reference = reenable(main_gateway)

    assert current.state.value == reference.state.value == "eligible"


def test_gateway_runtime_constructors_pass_profile_kind_on_every_path(gateway_env):
    profile = Profile(id="a", name="A", kind="oauth", priority=1, automatic=True)
    save_pool(Pool(profiles=[profile]))
    runtime_state.save(None, {})

    first_sight = gateway.Gateway(transport=lambda _: _response())
    assert first_sight.runtime_snapshot()["a"].kind == "oauth"

    runtime_state.save("a", {"a": {
        "last_usage_percent": 0.0,
        "resets_at": FIVE_RESET.isoformat(),
    }})
    restart_restore = gateway.Gateway(transport=lambda _: _response())
    assert restart_restore.runtime_snapshot()["a"].kind == "oauth"

    save_pool(Pool(profiles=[replace(profile, priority=2)]))
    assert restart_restore.runtime_snapshot()["a"].kind == "oauth"


def test_weekly_hold_survives_real_persist_and_restart_after_5h_reset(gateway_env):
    original = gateway.Gateway(transport=lambda _: _response(_weekly_headers()))
    result = original.handle("POST", "/v1/messages", {}, b"{}")
    list(result.body_chunks)
    assert result.profile_id == "a"
    assert original.runtime_snapshot()["a"].state == router.ProfileState.DRAINING
    saved = runtime_state.load()["profiles"]["a"]
    assert saved["last_usage_percent"] == 0.0
    assert saved["last_usage_percent_7d"] == 100.0
    assert saved["no_5h_window"] is False

    gateway_env[0] = FIVE_RESET + timedelta(seconds=1)
    restarted = gateway.Gateway(transport=lambda _: _response())
    restored = restarted.runtime_snapshot()["a"]

    assert restored.kind == "oauth"
    assert restored.no_5h_window is False
    assert restored.last_usage_percent is None
    assert restored.resets_at is None
    assert restored.last_usage_percent_7d == 100.0
    assert restored.resets_at_7d == WEEK_RESET
    assert restored.state == router.ProfileState.DRAINING
    result = restarted.handle("POST", "/v1/messages", {}, b"{}")
    list(result.body_chunks)
    assert result.profile_id == "b"


def test_takeover_serves_weekly_held_account_until_next_usage_observation(gateway_env):
    headers = _weekly_headers()
    gw = gateway.Gateway(transport=lambda _: _response(dict(headers)))
    first = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(first.body_chunks)
    assert gw.runtime_snapshot()["a"].state == router.ProfileState.DRAINING

    assert gw.force_active("a") is True
    assert gw.runtime_snapshot()["a"].state == router.ProfileState.ELIGIBLE
    headers.pop("anthropic-ratelimit-unified-7d-utilization")
    headers.pop("anthropic-ratelimit-unified-7d-reset")
    after_takeover = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(after_takeover.body_chunks)

    assert after_takeover.status == 200
    assert after_takeover.profile_id == "a"
    assert gw.runtime_snapshot()["a"].state == router.ProfileState.DRAINING
    following = gw.handle("POST", "/v1/messages", {}, b"{}")
    list(following.body_chunks)
    assert following.profile_id == "b"


def _parity_source(path):
    worktree = Path(__file__).resolve().parents[1]
    try:
        subprocess.run(
            ["git", "cat-file", "-e", f"{PARITY_REFERENCE_COMMIT}^{{commit}}"],
            cwd=worktree, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        message = f"pinned parity reference commit {PARITY_REFERENCE_COMMIT} is unavailable"
        if os.environ.get("CI") == "true":
            pytest.fail(f"{message}; CI checkout must include full history")
        pytest.skip(message)
    try:
        return subprocess.check_output(
            ["git", "show", f"{PARITY_REFERENCE_COMMIT}:{path}"],
            cwd=worktree, text=True, stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        message = f"pinned parity reference source {path} at {PARITY_REFERENCE_COMMIT} is unavailable"
        if os.environ.get("CI") == "true":
            pytest.fail(f"{message}; CI checkout must include full history")
        pytest.skip(message)


@pytest.fixture
def main_router():
    source = _parity_source("claude_unlimited/router.py")
    module = types.ModuleType("claude_unlimited._main_router_parity")
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, module.__name__, module)
        exec(compile(
            source, f"{PARITY_REFERENCE_COMMIT}:claude_unlimited/router.py", "exec",
        ), module.__dict__)
        yield module


@pytest.fixture
def main_gateway(main_router):
    source = _parity_source("claude_unlimited/gateway.py")
    module = types.ModuleType("claude_unlimited._main_gateway_parity")
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, module.__name__, module)
        with pytest.MonkeyPatch.context() as imports:
            imports.setitem(sys.modules, "claude_unlimited.router", main_router)
            exec(compile(
                source, f"{PARITY_REFERENCE_COMMIT}:claude_unlimited/gateway.py", "exec"),
                module.__dict__,
            )
        yield module


def test_parity_fixture_skips_when_reference_commit_is_missing(monkeypatch, request):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(sys.modules[__name__], "PARITY_REFERENCE_COMMIT", "missing-parity-commit")

    with pytest.raises(pytest.skip.Exception, match="pinned parity reference commit"):
        request.getfixturevalue("main_router")


def test_parity_fixture_fails_in_ci_when_reference_commit_is_missing(monkeypatch, request):
    monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(sys.modules[__name__], "PARITY_REFERENCE_COMMIT", "missing-parity-commit")

    with pytest.raises(pytest.fail.Exception, match="CI checkout must include full history"):
        request.getfixturevalue("main_router")


def _threshold_notice(implementation, monkeypatch, kind, primary, weekly, path="/v1/messages"):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind=kind, switch_threshold=95.0, automatic=True),
    ], settings=Settings(notifications_enabled=True, notify_approaching_threshold=True)))
    runtime_state.save(None, {})
    headers = {}
    for window, percent, reset in (("5h", primary, FIVE_RESET), ("7d", weekly, WEEK_RESET)):
        if percent is not None:
            headers[f"anthropic-ratelimit-unified-{window}-utilization"] = str(percent / 100)
            headers[f"anthropic-ratelimit-unified-{window}-reset"] = str(reset.timestamp())
    notices = []
    with monkeypatch.context() as patch:
        patch.setattr(implementation, "datetime", gateway.datetime)
        patch.setattr(implementation, "secret_store", gateway.secret_store)
        patch.setattr(implementation.Gateway, "_schedule_credential_checks", lambda *args: None)
        patch.setattr(implementation.notifications, "send_macos_notification",
                      lambda title, message: notices.append(message))

        def bridge_result(*args, **kwargs):
            return implementation.openai_bridge.OpenAIBridgeResult(
                status=200,
                headers={
                    "x-codex-primary-used-percent": str(primary),
                    "x-codex-primary-reset-at": str(FIVE_RESET.timestamp()),
                    "x-codex-primary-window-minutes": "300",
                    "x-codex-secondary-used-percent": str(weekly),
                    "x-codex-secondary-reset-at": str(WEEK_RESET.timestamp()),
                    "x-codex-secondary-window-minutes": "10080",
                },
                body_chunks=iter([b"ok"]),
            )

        patch.setattr(implementation.openai_bridge, "run", bridge_result)
        patch.setattr(implementation.openai_bridge, "run_passthrough", bridge_result)
        gw = implementation.Gateway(transport=lambda _: _response(headers))
        result = gw.handle("POST", path, {}, b"{}")
        assert result.status == 200
        assert result.profile_id == "a"
        list(result.body_chunks)
    assert len(notices) == 1
    return notices[0]


@pytest.mark.parametrize("primary, weekly, window", [
    (10.0, 94.0, "weekly"), (94.0, 10.0, "5-hour"),
])
def test_approaching_threshold_notice_names_window(gateway_env, monkeypatch, primary, weekly, window):
    notice = _threshold_notice(gateway, monkeypatch, "oauth", primary, weekly)

    assert notice == f"A is approaching its switch threshold ({window} window: 94% / 95%)."


@pytest.mark.parametrize("kind, primary, weekly, path", [
    ("oauth", None, 94.0, "/v1/messages"),
    ("codex", 94.0, 99.0, "/v1/messages"),
    ("codex", 94.0, 99.0, "/v1/responses"),
    ("api", 94.0, 99.0, "/v1/messages"),
])
def test_other_profile_threshold_notice_matches_main(
        gateway_env, monkeypatch, main_gateway, kind, primary, weekly, path):
    assert _threshold_notice(gateway, monkeypatch, kind, primary, weekly, path) == _threshold_notice(
        main_gateway, monkeypatch, kind, primary, weekly, path,
    )


@pytest.mark.parametrize("kind, no_5h_window", [
    ("oauth", True), ("codex", False), ("api", False),
])
@pytest.mark.parametrize("primary, weekly", [
    (0.0, 100.0), (100.0, 0.0), (99.0, 99.0), (None, 99.0), (10.0, None),
], ids=["0-100", "100-0", "99-99", "7d-only", "5h-only"])
def test_other_profile_snapshot_parity_with_main(
        gateway_env, monkeypatch, main_router, kind, no_5h_window, primary, weekly):
    """Restore the same samples and capability flag with each router's rules."""
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind=kind, priority=1, switch_threshold=95.0, automatic=True),
        Profile(id="b", name="B", kind="api", priority=1, switch_threshold=95.0, automatic=True),
    ]))
    runtime_state.save("a", {
        "a": {
            "last_usage_percent": primary,
            "resets_at": FIVE_RESET.isoformat() if primary is not None else None,
            "last_usage_percent_7d": weekly,
            "resets_at_7d": WEEK_RESET.isoformat() if weekly is not None else None,
            "no_5h_window": no_5h_window,
        },
        "b": {"last_usage_percent": 40.0, "resets_at": WEEK_RESET.isoformat()},
    })

    def result(implementation):
        # Startup derives state through routing_percent; use the reference
        # router there too, before comparing both selectors and the deadline.
        with monkeypatch.context() as patch:
            patch.setattr(gateway, "routing_percent", implementation.routing_percent)
            runtimes = gateway.Gateway(transport=lambda _: _response()).runtime_snapshot()
        snapshot = implementation.PoolSnapshot(profiles=list(runtimes.values()), current_profile_id="a")
        return (
            runtimes["a"].state.value,
            implementation.routing_percent(runtimes["a"]),
            implementation.choose(snapshot, NOW).profile_id,
            implementation.choose_for_new_branch(snapshot, NOW).profile_id,
            implementation._reset_sort_key(runtimes["a"]),
        )

    assert result(router) == result(main_router)


@pytest.mark.parametrize("kind, no_5h_window", [
    ("oauth", False), ("oauth", True), ("codex", False), ("api", False),
])
@pytest.mark.parametrize("weekly", [40.0, 94.0, 95.0, 100.0])
@pytest.mark.parametrize("reset", [None, FIVE_RESET, WEEK_RESET])
def test_provider_refusal_storage_and_recovery_match_main(
        main_router, kind, no_5h_window, weekly, reset):
    def refused_pool(implementation):
        kwargs = {"kind": kind} if implementation is router else {}
        rt = implementation.ProfileRuntime(
            profile_id="a", priority=1, switch_threshold=95.0, automatic=True,
            state=implementation.ProfileState.DRAINING,
            no_5h_window=no_5h_window,
            last_usage_percent=None if no_5h_window else 0.0,
            resets_at=None if no_5h_window else FIVE_RESET,
            last_usage_percent_7d=weekly, resets_at_7d=WEEK_RESET, **kwargs,
        )
        return implementation.observe(
            implementation.PoolSnapshot(profiles=[rt]), "a", QuotaExhausted(reset), NOW,
        )

    def comparable_fields(runtime):
        fields = vars(runtime).copy()
        fields.pop("kind", None)
        fields["state"] = runtime.state.value
        return fields

    current = refused_pool(router)
    reference = refused_pool(main_router)
    assert comparable_fields(current.profiles[0]) == comparable_fields(reference.profiles[0])

    for when in (
        NOW,
        FIVE_RESET - timedelta(seconds=1), FIVE_RESET, FIVE_RESET + timedelta(seconds=1),
        WEEK_RESET - timedelta(seconds=1), WEEK_RESET, WEEK_RESET + timedelta(seconds=1),
    ):
        recovered = router.recover_expired_cooldowns(current, when).profiles[0]
        main_recovered = main_router.recover_expired_cooldowns(reference, when).profiles[0]
        assert (recovered.state == router.ProfileState.EXHAUSTED) == (
            main_recovered.state == main_router.ProfileState.EXHAUSTED
        ), when

        expected = comparable_fields(main_recovered)
        if (kind == "oauth" and not no_5h_window
                and main_recovered.state != main_router.ProfileState.EXHAUSTED
                and weekly >= main_recovered.switch_threshold and when < WEEK_RESET):
            assert recovered.state == router.ProfileState.DRAINING
            expected["state"] = router.ProfileState.DRAINING.value
        assert comparable_fields(recovered) == expected, when


@pytest.mark.parametrize("primary, weekly, weekly_reset, release, retry_after, state", [
    (0.0, 100.0, WEEK_RESET, WEEK_RESET, None, router.ProfileState.DRAINING),
    (96.0, 40.0, WEEK_RESET, NOW + timedelta(minutes=10), 600, router.ProfileState.ELIGIBLE),
    (0.0, 100.0, None, None, None, router.ProfileState.DRAINING),
])
def test_refusal_release_time_and_retry_after(primary, weekly, weekly_reset, release, retry_after, state):
    refusal_reset = NOW + timedelta(minutes=10)
    pool = router.observe(
        router.PoolSnapshot(profiles=[_runtime()]), "a",
        replace(_usage(primary, weekly), resets_at_7d=weekly_reset), NOW,
    )
    pool = router.observe(pool, "a", QuotaExhausted(refusal_reset), NOW)

    assert pool.profiles[0].state == router.ProfileState.EXHAUSTED
    assert router.release_time(pool.profiles[0]) == release
    assert gateway._pool_retry_after_seconds(pool, NOW) == retry_after

    recovered = router.recover_expired_cooldowns(pool, refusal_reset + timedelta(seconds=1))
    assert recovered.profiles[0].state == state
    if state == router.ProfileState.DRAINING:
        assert router.release_time(recovered.profiles[0]) == release


@pytest.mark.parametrize("hold, primary, reset_5h, weekly, reset_7d, refusal_reset, cooldown_seconds, expected_release", [
    pytest.param("refusal", 0.0, 60, 100.0, 8640, 10, None, 8640, id="weekly-six-days"),
    pytest.param("refusal", 96.0, 60, 40.0, 8640, 10, None, 10, id="primary-held-week-low"),
    pytest.param("refusal", 0.0, 60, 100.0, None, 10, None, None, id="weekly-reset-unknown"),
    pytest.param("refusal", 96.0, 60, 100.0, 120, 10, None, 120, id="both-held-week-later"),
    pytest.param("refusal", 96.0, 60, 100.0, 10, 120, None, 120, id="both-held-refusal-later"),
    pytest.param("refusal", 0.0, 60, 100.0, 10, 20, None, 20, id="refusal-after-weekly-reset"),
    pytest.param("refusal", 96.0, 60, 100.0, 10, 10, None, 10, id="simultaneous-resets"),
    pytest.param("refusal", 0.0, 60, 95.0, 20, 10, None, 20, id="weekly-at-threshold"),
    pytest.param("refusal", 0.0, 60, 94.0, 20, 10, None, 10, id="weekly-below-threshold"),
    pytest.param("refusal", 95.0, 60, 40.0, 120, 10, None, 10, id="primary-at-threshold"),
    pytest.param("refusal", 40.0, 60, 40.0, 120, 10, None, 10, id="both-below-threshold"),
    pytest.param("refusal", 96.0, 60, None, None, 10, None, 10, id="weekly-sample-absent"),
    pytest.param("refusal", None, None, 100.0, 20, 10, None, 20, id="primary-sample-absent"),
    pytest.param("refusal", 96.0, None, 40.0, 120, 10, None, 10, id="primary-reset-unknown"),
    pytest.param("refusal", 96.0, None, 100.0, None, 10, None, None, id="both-usage-resets-unknown"),
    pytest.param("refusal", 0.0, 60, 100.0, -10, 10, None, 10, id="weekly-already-expired"),
    pytest.param("refusal", 0.0, 60, 100.0, 0, 10, None, 10, id="weekly-expires-now"),
    pytest.param("refusal", 96.0, 60, 40.0, None, 10, None, 10, id="low-weekly-reset-unknown"),
    pytest.param("refusal", 0.0, 60, 100.0, 20, None, None, None, id="refusal-unknown-week-held"),
    pytest.param("refusal", 96.0, 60, 40.0, 20, None, None, None, id="refusal-unknown-week-low"),
    pytest.param("refusal", 96.0, None, 100.0, None, None, None, None, id="all-resets-unknown"),
    pytest.param("refusal", None, None, None, None, 10, None, 10, id="no-usage-samples"),
    pytest.param("refusal", 96.0, 10, 100.0, 20, 30, None, 30, id="primary-weekly-refusal-order"),
    pytest.param("refusal", 96.0, 20, 100.0, 30, 10, None, 30, id="refusal-primary-weekly-order"),
    pytest.param("usage", 96.0, 60, 40.0, 8640, None, 30, 60, id="cooldown-primary-hold"),
    pytest.param("usage", 0.0, 60, 100.0, 8640, None, 30, 8640, id="cooldown-weekly-hold"),
    pytest.param("usage", 96.0, 60, 100.0, 120, None, 30, 120, id="cooldown-both-holds"),
    pytest.param("refusal", 10.0, 60, 20.0, 8640, 10, 30, 0.5, id="cooldown-refusal"),
    pytest.param("refusal", 0.0, 60, 100.0, 120, 10, 30, 120, id="cooldown-refusal-weekly"),
    pytest.param("refusal", 96.0, 60, 40.0, 8640, 10, 30, 10, id="cooldown-refusal-primary"),
    pytest.param("usage", 96.0, 5, 100.0, 10, None, 1800, 30, id="cooldown-outlasts-both-holds"),
    pytest.param("usage", 0.0, 60, 100.0, None, None, 30, None, id="cooldown-weekly-reset-unknown"),
    pytest.param("usage", 96.0, None, 40.0, 8640, None, 30, None, id="cooldown-primary-reset-unknown"),
    pytest.param("usage", 95.0, 60, 95.0, 120, None, 30, 120, id="cooldown-both-at-threshold"),
])
def test_refusal_release_time_property(
        hold, primary, reset_5h, weekly, reset_7d, refusal_reset, cooldown_seconds, expected_release):
    """Without new observations, every promised deadline matches eligibility."""
    def deadline(minutes):
        return NOW + timedelta(minutes=minutes) if minutes is not None else None

    pool = router.PoolSnapshot(profiles=[_runtime(
        state=router.ProfileState.DRAINING,
        last_usage_percent=primary, resets_at=deadline(reset_5h),
        last_usage_percent_7d=weekly, resets_at_7d=deadline(reset_7d),
    )])
    if hold == "refusal":
        pool = router.observe(pool, "a", QuotaExhausted(deadline(refusal_reset)), NOW)
    if cooldown_seconds is not None:
        pool = router.observe(pool, "a", ShortRateLimit(cooldown_seconds), NOW)
    else:
        assert router.release_time(pool.profiles[0]) == deadline(expected_release)
    initial_release = gateway._profile_return_time(pool.profiles[0])
    steps = {NOW, NOW + timedelta(seconds=1)}
    for minutes in (reset_5h, reset_7d, refusal_reset, expected_release):
        if minutes is not None:
            steps.update(deadline(minutes) + timedelta(seconds=offset) for offset in (-1, 0, 1))
    if cooldown_seconds is not None:
        steps.update(NOW + timedelta(seconds=cooldown_seconds + offset) for offset in (-1, 0, 1))

    promises = set()
    for when in sorted(step for step in steps if step >= NOW):
        pool = router.recover_expired_cooldowns(pool, when)
        runtime = pool.profiles[0]
        for promised in promises:
            if when < promised:
                assert runtime.state != router.ProfileState.ELIGIBLE, (when, promised)
            elif when >= promised + timedelta(seconds=1):
                assert runtime.state == router.ProfileState.ELIGIBLE, (when, promised)
        release = gateway._profile_return_time(runtime)
        if release is not None:
            assert runtime.state != router.ProfileState.ELIGIBLE, (when, release)
            assert release + timedelta(seconds=1) in steps
            promises.add(release)

    assert initial_release == deadline(expected_release)
    if expected_release is None:
        assert pool.profiles[0].state != router.ProfileState.ELIGIBLE


def _limited_response(implementation, monkeypatch, kind, primary, weekly, weekly_reset, observation):
    save_pool(Pool(profiles=[
        Profile(id="a", name="A", kind=kind, switch_threshold=95.0, automatic=True),
    ]))
    runtime_state.save(None, {})

    def unexpected_transport(_):
        pytest.fail("An exhausted pool must reject locally.")

    with monkeypatch.context() as patch:
        patch.setattr(implementation, "datetime", gateway.datetime)
        patch.setattr(implementation, "secret_store", gateway.secret_store)
        patch.setattr(implementation.Gateway, "_schedule_credential_checks", lambda *args: None)
        gw = implementation.Gateway(transport=unexpected_transport)
        gw.runtime_snapshot()
        gw._observe("a", replace(_usage(primary, weekly), resets_at_7d=weekly_reset), NOW)
        gw._observe("a", observation, NOW)
        result = gw.handle("POST", "/v1/messages", {}, b"{}")
    assert result.status == 429
    return result


@pytest.mark.parametrize("weekly_reset, expected_retry_after, reset_format", [
    (NOW + timedelta(minutes=20), "1200", "%H:%M"),
    (WEEK_RESET, None, "%H:%M on %A"),
    (None, None, None),
])
def test_refusal_response_message_agrees_with_retry_after(
        gateway_env, monkeypatch, weekly_reset, expected_retry_after, reset_format):
    result = _limited_response(
        gateway, monkeypatch, "oauth", 0.0, 100.0, weekly_reset,
        QuotaExhausted(NOW + timedelta(minutes=10)),
    )

    assert result.error == "all_profiles_exhausted"
    assert result.headers.get("retry-after") == expected_retry_after
    if weekly_reset is None:
        assert result.error_detail == gateway.MESSAGE_ALL_EXHAUSTED
    else:
        assert result.error_detail == (
            f"{gateway.MESSAGE_ALL_EXHAUSTED} The first one resets at "
            f"{weekly_reset.astimezone().strftime(reset_format)}."
        )
        if expected_retry_after is not None:
            assert NOW + timedelta(seconds=int(result.headers["retry-after"])) == weekly_reset


def test_weekly_hold_cooldown_response_uses_weekly_deadline(gateway_env, monkeypatch):
    result = _limited_response(
        gateway, monkeypatch, "oauth", 0.0, 100.0, WEEK_RESET, ShortRateLimit(30),
    )

    assert result.headers.get("retry-after") is None
    assert result.error == "all_profiles_exhausted"
    assert result.error_detail == (
        f"{gateway.MESSAGE_ALL_EXHAUSTED} The first one resets at {WEEK_RESET.astimezone():%H:%M on %A}."
    )


def test_unheld_cooldown_response_matches_main(gateway_env, monkeypatch, main_gateway):
    current = _limited_response(
        gateway, monkeypatch, "oauth", 10.0, 20.0, WEEK_RESET, ShortRateLimit(30),
    )
    reference = _limited_response(
        main_gateway, monkeypatch, "oauth", 10.0, 20.0, WEEK_RESET, ShortRateLimit(30),
    )

    assert current.headers == reference.headers == {"retry-after": "30"}
    assert current.error == reference.error == "no_eligible_profile"
    assert current.error_detail == reference.error_detail


def test_weekly_hold_cooldown_with_unknown_reset_has_no_deadline(gateway_env, monkeypatch):
    result = _limited_response(
        gateway, monkeypatch, "oauth", 0.0, 100.0, None, ShortRateLimit(30),
    )

    assert result.headers.get("retry-after") is None
    assert result.error == "all_profiles_exhausted"
    assert result.error_detail == gateway.MESSAGE_ALL_EXHAUSTED


@pytest.mark.parametrize("kind, no_5h_window", [
    ("oauth", True), ("codex", False), ("api", False),
])
@pytest.mark.parametrize("primary, weekly, reset_5h, reset_7d, cooldown_seconds", [
    (10.0, 20.0, FIVE_RESET, WEEK_RESET, 30),
    (96.0, 100.0, FIVE_RESET, WEEK_RESET, 30),
    (96.0, 100.0, None, None, 30),
    (96.0, 100.0, NOW + timedelta(seconds=10), WEEK_RESET, 30),
])
def test_other_profile_cooldown_deadlines_match_main(
        main_gateway, kind, no_5h_window, primary, weekly, reset_5h, reset_7d, cooldown_seconds):
    runtime = router.ProfileRuntime(
        profile_id="a", priority=1, switch_threshold=95.0, automatic=True,
        kind=kind, no_5h_window=no_5h_window, state=router.ProfileState.COOLDOWN,
        last_usage_percent=None if no_5h_window else primary,
        resets_at=None if no_5h_window else reset_5h,
        last_usage_percent_7d=weekly, resets_at_7d=reset_7d,
        cooldown_until=NOW + timedelta(seconds=cooldown_seconds),
    )
    snapshot = router.PoolSnapshot(profiles=[runtime])
    pool = Pool(profiles=[Profile(id="a", name="A", kind=kind)])

    assert gateway._profile_return_time(runtime) == runtime.cooldown_until
    assert gateway._pool_retry_after_seconds(snapshot, NOW) == main_gateway._pool_retry_after_seconds(snapshot, NOW)
    assert gateway._capacity_exhaustion(snapshot, pool) == main_gateway._capacity_exhaustion(snapshot, pool)


@pytest.mark.parametrize("kind, no_5h_window", [
    ("oauth", False), ("oauth", True), ("codex", False), ("api", False),
])
def test_same_day_exhaustion_sentence_matches_main(
        gateway_env, monkeypatch, main_gateway, kind, no_5h_window):
    primary = None if no_5h_window else 100.0
    weekly = 100.0 if no_5h_window else 20.0
    weekly_reset = NOW + timedelta(minutes=20)
    refusal = QuotaExhausted(NOW + timedelta(minutes=10))
    current = _limited_response(
        gateway, monkeypatch, kind, primary, weekly, weekly_reset, refusal,
    )
    reference = _limited_response(
        main_gateway, monkeypatch, kind, primary, weekly, weekly_reset, refusal,
    )

    assert current.error == reference.error == "all_profiles_exhausted"
    assert current.headers == reference.headers
    assert current.error_detail.encode() == reference.error_detail.encode()


@pytest.mark.parametrize("days_away, reset_format", [
    (0, "%H:%M"),
    (1, "%H:%M on %A"),
    (6, "%H:%M on %A"),
    (7, "%H:%M on %A, %Y-%m-%d"),
    (50, "%H:%M on %A, %Y-%m-%d"),
])
def test_exhaustion_sentence_includes_day_and_date_when_needed(gateway_env, days_away, reset_format):
    reset = NOW + timedelta(days=days_away, seconds=30)

    assert gateway._exhaustion_message(reset, NOW) == (
        f"{gateway.MESSAGE_ALL_EXHAUSTED} The first one resets at {reset.astimezone().strftime(reset_format)}."
    )


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="Local timezone changes require tzset.")
@pytest.mark.parametrize("local_zone, offset_hours", [("MST7", -7), ("JST-9", 9)])
@pytest.mark.parametrize("seconds, reset_format", [(5, "%H:%M"), (30, "%H:%M on %A")])
def test_exhaustion_sentence_uses_local_calendar_day(
        gateway_env, monkeypatch, local_zone, offset_hours, seconds, reset_format):
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", local_zone)
            time.tzset()
            local_now = NOW.astimezone(timezone(timedelta(hours=offset_hours))).replace(
                hour=23, minute=59, second=50,
            )
            gateway_env[0] = local_now.astimezone(timezone.utc)
            local_reset = local_now + timedelta(seconds=seconds)

            assert gateway._exhaustion_message(
                local_reset.astimezone(timezone.utc), gateway_env[0],
            ) == (
                f"{gateway.MESSAGE_ALL_EXHAUSTED} The first one resets at {local_reset.strftime(reset_format)}."
            )
    finally:
        time.tzset()


@pytest.mark.parametrize("kind, no_5h_window", [
    ("oauth", True), ("codex", False), ("api", False),
])
@pytest.mark.parametrize("primary, weekly, weekly_reset, refusal_reset", [
    (0.0, 100.0, WEEK_RESET, NOW + timedelta(minutes=10)),
    (96.0, 40.0, WEEK_RESET, NOW + timedelta(minutes=10)),
    (0.0, 100.0, None, NOW + timedelta(minutes=10)),
    (96.0, 100.0, NOW + timedelta(minutes=5), NOW + timedelta(minutes=10)),
    (96.0, 100.0, WEEK_RESET, None),
    (0.0, 100.0, None, None),
])
def test_other_profile_exhausted_release_and_sentence_match_main(
        gateway_env, monkeypatch, main_router, main_gateway, kind, no_5h_window,
        primary, weekly, weekly_reset, refusal_reset):
    primary = None if no_5h_window else primary

    def refused_runtime(implementation):
        kwargs = {"kind": kind} if implementation is router else {}
        runtime = implementation.ProfileRuntime(
            profile_id="a", priority=1, switch_threshold=95.0, automatic=True,
            no_5h_window=no_5h_window,
            last_usage_percent=primary, resets_at=None if no_5h_window else FIVE_RESET,
            last_usage_percent_7d=weekly, resets_at_7d=weekly_reset, **kwargs,
        )
        return implementation.observe(
            implementation.PoolSnapshot(profiles=[runtime]), "a", QuotaExhausted(refusal_reset), NOW,
        ).profiles[0]

    current = refused_runtime(router)
    reference = refused_runtime(main_router)
    assert current.state == router.ProfileState.EXHAUSTED
    assert reference.state == main_router.ProfileState.EXHAUSTED
    assert router.release_time(current) == main_router._reset_sort_key(reference)[1]

    current_response = _limited_response(
        gateway, monkeypatch, kind, primary, weekly, weekly_reset, QuotaExhausted(refusal_reset),
    )
    reference_response = _limited_response(
        main_gateway, monkeypatch, kind, primary, weekly, weekly_reset, QuotaExhausted(refusal_reset),
    )
    assert current_response.error == reference_response.error == "all_profiles_exhausted"
    assert current_response.headers == reference_response.headers
    if no_5h_window and refusal_reset is None and weekly_reset is not None:
        assert current_response.error_detail == (
            f"{gateway.MESSAGE_ALL_EXHAUSTED} The first one resets at "
            f"{weekly_reset.astimezone():%H:%M on %A}."
        )
    else:
        assert current_response.error_detail == reference_response.error_detail
