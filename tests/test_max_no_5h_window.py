from datetime import datetime, timedelta, timezone
import re

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


def _function_body(source, marker):
    start = source.index(marker)
    next_function = re.search(r"\n(?:async )?function ", source[start + len(marker):])
    if not next_function:
        return source[start:]
    return source[start:start + len(marker) + next_function.start()]


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


def test_legacy_7d_only_usage_does_not_infer_the_missing_window_flag():
    fields = _restorable_usage_fields({
        "last_usage_percent": None,
        "resets_at": None,
        "last_usage_percent_7d": 96.0,
        "resets_at_7d": FUTURE.isoformat(),
    }, NOW)

    assert "no_5h_window" not in fields


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


def test_no_5h_exhausted_does_not_recover_from_old_7d_reset_while_5h_reset_is_future():
    runtime = _profile(
        "max-profile",
        state=ProfileState.EXHAUSTED,
        no_5h_window=True,
        resets_at=NOW + timedelta(hours=2),
        last_usage_percent_7d=99.0,
        resets_at_7d=NOW - timedelta(hours=1),
    )

    recovered = recover_expired_cooldowns(PoolSnapshot(profiles=[runtime]), NOW).profiles[0]

    assert recovered.state == ProfileState.EXHAUSTED
    assert recovered.resets_at == NOW + timedelta(hours=2)
    assert recovered.last_usage_percent_7d is None
    assert recovered.resets_at_7d is None


def test_no_5h_exhausted_recovers_when_5h_reset_has_also_passed():
    runtime = _profile(
        "max-profile",
        state=ProfileState.EXHAUSTED,
        no_5h_window=True,
        resets_at=NOW - timedelta(minutes=1),
        last_usage_percent_7d=99.0,
        resets_at_7d=NOW - timedelta(hours=1),
    )

    recovered = recover_expired_cooldowns(PoolSnapshot(profiles=[runtime]), NOW).profiles[0]

    assert recovered.state == ProfileState.ELIGIBLE


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


def test_profile_payload_serializes_no_5h_window_authority():
    max_profile = Profile(id="max-profile", name="max-profile", kind="oauth", switch_threshold=95.0)
    max_runtime = _profile("max-profile", no_5h_window=True,
                           last_usage_percent_7d=96.0, resets_at_7d=FUTURE)
    team_profile = Profile(id="team-profile", name="team-profile", kind="oauth", switch_threshold=95.0)
    team_runtime = _profile("team-profile", no_5h_window=False,
                            last_usage_percent=None, resets_at=None,
                            last_usage_percent_7d=96.0, resets_at_7d=FUTURE)

    max_payload = daemon._profile_to_public_dict(max_profile, runtime=max_runtime)
    team_payload = daemon._profile_to_public_dict(team_profile, runtime=team_runtime)

    assert max_payload["usage_5h_percent"] is None
    assert max_payload["usage_7d_percent"] == 96.0
    assert max_payload["no_5h_window"] is True
    assert team_payload["usage_5h_percent"] is None
    assert team_payload["usage_7d_percent"] == 96.0
    assert team_payload["no_5h_window"] is False


def test_dashboard_label_keeps_team_after_reset_as_not_observed():
    app_js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    label_body = _function_body(app_js, "function primaryWindowEmptyLabel(")

    assert "p.kind === 'oauth' && p.no_5h_window === true && !has5h && has7d" in label_body
    assert "profile.no_5h_window" in label_body


def test_dashboard_uses_flagged_7d_only_usage_for_profiles_table_sort():
    app_js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
    sorter_body = _function_body(app_js, "function filteredSortedProfiles(")

    assert "b.usage_5h_percent ?? (b.no_5h_window === true ? b.usage_7d_percent : null) ?? -1" in sorter_body
    assert "a.usage_5h_percent ?? (a.no_5h_window === true ? a.usage_7d_percent : null) ?? -1" in sorter_body


def test_dashboard_display_callers_use_the_authoritative_window_flag():
    app_js = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")

    for marker in (
        "function renderProfileCard(",
        "function renderProfileTableRow(",
        "function openProfileDetailModal(",
    ):
        assert "primaryWindowEmptyLabel(p, has5h, has7d)" in _function_body(app_js, marker)

    cost_body = _function_body(app_js, "function renderCostRow(")
    assert "p.kind === 'oauth' && p.no_5h_window === true && has7d" in cost_body
