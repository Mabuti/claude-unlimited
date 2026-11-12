from datetime import datetime, timedelta, timezone

import pytest

from claude_unlimited import daemon
from claude_unlimited.config import Pool, Profile
from claude_unlimited.gateway import (
    _capacity_exhaustion,
    _pool_retry_after_seconds,
    _restorable_usage_fields,
)
from claude_unlimited.observation import UsageSnapshot
from claude_unlimited.router import (
    PoolSnapshot,
    ProfileRuntime,
    ProfileState,
    _reset_sort_key,
    choose_for_new_branch,
    observe,
    recover_expired_cooldowns,
    routing_percent,
)


NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)
FUTURE = NOW + timedelta(hours=4)


def _profile(profile_id, *, threshold=95.0, priority=1, **usage):
    return ProfileRuntime(
        profile_id=profile_id,
        priority=priority,
        switch_threshold=threshold,
        automatic=True,
        **usage,
    )


def test_max_at_96_percent_7d_is_over_threshold_and_not_chosen_over_team():
    pool = PoolSnapshot(profiles=[_profile("max-profile"), _profile("team-profile", priority=2)])
    pool = observe(pool, "max-profile", UsageSnapshot(
        percent=None, resets_at=None, confidence="measured", percent_7d=96.0,
    ), NOW)
    pool = observe(pool, "team-profile", UsageSnapshot(
        percent=10.0, resets_at=FUTURE, confidence="measured", percent_7d=99.0,
    ), NOW)

    assert pool.profiles[0].state == ProfileState.DRAINING
    assert pool.profiles[0].last_usage_percent is None
    assert pool.profiles[0].no_5h_window is True
    assert choose_for_new_branch(pool, NOW).profile_id == "team-profile"


def test_team_5h_percent_wins_over_7d_for_routing():
    team = observe(
        PoolSnapshot(profiles=[_profile("team-profile")]),
        "team-profile",
        UsageSnapshot(percent=10.0, resets_at=FUTURE, confidence="measured", percent_7d=99.0),
        NOW,
    ).profiles[0]

    assert routing_percent(team) == 10.0
    assert team.state == ProfileState.ELIGIBLE


def test_least_utilized_order_uses_7d_when_5h_is_absent():
    decision = choose_for_new_branch(PoolSnapshot(profiles=[
        _profile("max-profile", last_usage_percent_7d=30.0, no_5h_window=True),
        _profile("team-profile", last_usage_percent=40.0, last_usage_percent_7d=1.0),
    ]), NOW)

    assert decision.profile_id == "max-profile"


def test_fresh_7d_only_reading_clears_a_stale_5h_value():
    pool = PoolSnapshot(profiles=[_profile(
        "max-profile", last_usage_percent=88.0, resets_at=FUTURE,
        last_usage_percent_7d=90.0, resets_at_7d=FUTURE,
    )])
    updated = observe(pool, "max-profile", UsageSnapshot(
        percent=None, resets_at=None, confidence="measured", percent_7d=20.0,
        resets_at_7d=FUTURE,
    ), NOW).profiles[0]

    assert updated.last_usage_percent is None
    assert updated.resets_at is None
    assert updated.last_usage_percent_7d == 20.0


def test_persisted_7d_only_usage_restores_without_a_5h_value():
    fields = _restorable_usage_fields({
        "last_usage_percent": None,
        "resets_at": None,
        "last_usage_percent_7d": 96.0,
        "resets_at_7d": FUTURE.isoformat(),
        "no_5h_window": True,
    }, NOW)

    assert fields == {
        "last_usage_percent_7d": 96.0,
        "resets_at_7d": FUTURE,
        "no_5h_window": True,
    }


def test_legacy_7d_only_usage_infers_the_missing_window_flag():
    fields = _restorable_usage_fields({
        "last_usage_percent": None,
        "resets_at": None,
        "last_usage_percent_7d": 96.0,
        "resets_at_7d": FUTURE.isoformat(),
    }, NOW)

    assert fields["no_5h_window"] is True


def test_team_5h_reset_recovers_even_with_a_future_7d_sample():
    recovered = recover_expired_cooldowns(PoolSnapshot(profiles=[_profile(
        "team-profile",
        state=ProfileState.DRAINING,
        last_usage_percent=99.0,
        resets_at=NOW - timedelta(seconds=1),
        last_usage_percent_7d=99.0,
        resets_at_7d=FUTURE,
    )]), NOW).profiles[0]

    assert recovered.state == ProfileState.ELIGIBLE
    assert recovered.last_usage_percent is None
    assert recovered.resets_at is None
    assert recovered.last_usage_percent_7d == 99.0
    assert recovered.resets_at_7d == FUTURE


def test_max_7d_only_stays_draining_until_its_weekly_reset():
    runtime = _profile("max-profile", state=ProfileState.DRAINING,
                        no_5h_window=True, last_usage_percent_7d=99.0,
                        resets_at_7d=FUTURE)

    before_reset = recover_expired_cooldowns(PoolSnapshot(profiles=[runtime]), NOW).profiles[0]
    after_reset = recover_expired_cooldowns(
        PoolSnapshot(profiles=[before_reset]), FUTURE + timedelta(seconds=1)
    ).profiles[0]

    assert before_reset.state == ProfileState.DRAINING
    assert after_reset.state == ProfileState.ELIGIBLE
    assert after_reset.last_usage_percent_7d is None
    assert after_reset.resets_at_7d is None


@pytest.mark.parametrize("state", [
    ProfileState.ELIGIBLE,
    ProfileState.DRAINING,
    ProfileState.EXHAUSTED,
    ProfileState.COOLDOWN,
])
def test_expired_7d_sample_clears_without_affecting_a_live_5h_window(state):
    runtime = _profile(
        "team-profile",
        state=state,
        last_usage_percent=20.0,
        resets_at=FUTURE,
        last_usage_percent_7d=99.0,
        resets_at_7d=NOW - timedelta(seconds=1),
        cooldown_until=FUTURE if state == ProfileState.COOLDOWN else None,
    )

    recovered = recover_expired_cooldowns(PoolSnapshot(profiles=[runtime]), NOW).profiles[0]

    assert recovered.state == state
    assert recovered.last_usage_percent == 20.0
    assert recovered.resets_at == FUTURE
    assert recovered.last_usage_percent_7d is None
    assert recovered.resets_at_7d is None


def test_7d_reset_is_a_sort_deadline_only_for_no_5h_profiles():
    soon = NOW + timedelta(minutes=10)
    team = _profile("team-profile", last_usage_percent=10.0,
                    last_usage_percent_7d=90.0, resets_at_7d=soon)
    max_profile = _profile("max-profile", no_5h_window=True,
                           last_usage_percent_7d=90.0, resets_at_7d=soon)

    assert _reset_sort_key(team) == (1, None)
    assert _reset_sort_key(max_profile) == (0, soon)


def test_7d_reset_is_a_retry_deadline_only_for_no_5h_profiles():
    soon = NOW + timedelta(minutes=10)
    max_profile = _profile("max-profile", state=ProfileState.DRAINING,
                           no_5h_window=True, last_usage_percent_7d=99.0,
                           resets_at_7d=soon)
    team = _profile("team-profile", state=ProfileState.DRAINING,
                    last_usage_percent=99.0, last_usage_percent_7d=99.0,
                    resets_at_7d=soon)

    assert _pool_retry_after_seconds(PoolSnapshot(profiles=[max_profile]), NOW) == 600
    assert _pool_retry_after_seconds(PoolSnapshot(profiles=[team]), NOW) is None


def test_capacity_exhaustion_includes_weekly_reset_for_no_5h_profiles():
    profile = Profile(id="max-profile", name="max-profile", kind="oauth", enabled=True)
    runtime = _profile("max-profile", state=ProfileState.DRAINING,
                       no_5h_window=True, last_usage_percent_7d=99.0,
                       resets_at_7d=FUTURE)

    exhausted, reset = _capacity_exhaustion(PoolSnapshot(profiles=[runtime]),
                                             Pool(profiles=[profile]))

    assert exhausted is True
    assert reset == FUTURE


def test_profile_payload_serializes_7d_only_usage():
    profile = Profile(id="max-profile", name="max-profile", kind="oauth", switch_threshold=95.0)
    runtime = _profile("max-profile", last_usage_percent_7d=96.0, resets_at_7d=FUTURE)

    payload = daemon._profile_to_public_dict(profile, runtime=runtime)

    assert payload["usage_5h_percent"] is None
    assert payload["usage_7d_percent"] == 96.0


def test_dashboard_labels_a_missing_5h_window_and_keeps_the_7d_bar():
    app_js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")

    assert "profile.no_5h_window" in app_js
    assert "primaryWindowEmptyLabel" in app_js
    assert "b.usage_5h_percent ?? b.usage_7d_percent" in app_js
