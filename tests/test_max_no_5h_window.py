from datetime import datetime, timedelta, timezone

from claude_unlimited import daemon
from claude_unlimited.config import Profile
from claude_unlimited.gateway import _restorable_usage_fields
from claude_unlimited.observation import UsageSnapshot
from claude_unlimited.router import (
    PoolSnapshot,
    ProfileRuntime,
    ProfileState,
    choose_for_new_branch,
    observe,
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
        _profile("max-profile", last_usage_percent_7d=30.0),
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
    }, NOW)

    assert fields == {
        "last_usage_percent_7d": 96.0,
        "resets_at_7d": FUTURE,
    }


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
