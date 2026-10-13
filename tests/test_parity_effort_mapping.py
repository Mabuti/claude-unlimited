"""Direction A effort selection and backward compatibility; no provider I/O."""
import json

import pytest

from claude_unlimited import config, connectors, openai_models as models


EFFORTS = (None, *models.CLAUDE_REASONING_EFFORTS)


def row(claude_model="claude-fable-5-1", **fields):
    return {"claude_model": claude_model, **fields}


@pytest.mark.parametrize("rows, message", [
    ([row(source_effort="low")], "base row"),
    ([row(), row(source_effort="*")], "duplicate"),
    ([row(), row(source_effort="low"), row(source_effort="low")], "duplicate"),
    ([row(), row("claude-fable-5-1-20261001", source_effort="max")], "spellings"),
    ([row(), row(source_effort="none")], "source_effort"),
    ([row(source_effort=None)], "source_effort"),
    ([row(source_effort="")], "source_effort"),
    ([row(source_effort=[])], "source_effort"),
    ([row(source_effort={})], "source_effort"),
    ([row(source_effort=1)], "source_effort"),
])
def test_rejected_groups(rows, message):
    with pytest.raises(ValueError, match=message):
        config._validated_model_parity(rows)


def test_mixed_groups_allow_interleaved_overrides_before_base():
    rows = [row(source_effort="low", effort="follow"), row("claude-sonnet-5"),
            row(source_effort="*", effort="none"),
            row("claude-sonnet-5", source_effort="max", claude_effort="high")]
    assert config._validated_model_parity(rows) == rows


def test_legacy_dict_rejects_duplicate_base_spellings():
    with pytest.raises(ValueError, match="duplicate"):
        config._validated_model_parity({"claude-fable-5": {}, "claude-fable-5-20261001": {}})


@pytest.mark.parametrize("requested", ["claude-fable-5-1", "claude-fable-5-1-20261001", "claude-fable-6"])
@pytest.mark.parametrize("effort", EFFORTS)
def test_exact_base_family_choose_override_or_base(requested, effort):
    rows = [row(source_effort="max", model="override", effort="low"),
            row(model="base", effort="high"),
            row("claude-fable-4", model="later-family", effort="none")]
    expected = ("override", "low") if effort == "max" else ("base", "high")
    assert models.map_model(requested, parity=rows, requested_effort=effort) == models.OpenAIModelTarget(*expected)
    # An exact or dated match beats the earlier family group.
    assert models.map_model("claude-fable-4-20261001", parity=rows, requested_effort=effort).model == "later-family"
    assert models.map_model("claude-fable-4", parity=rows, requested_effort=effort).model == "later-family"


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("model", ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5", "claude-haiku-5-5"])
def test_follow_uses_explicit_effort_or_curated_default(model, effort):
    target = models.map_model(model, parity=[row(model, effort="follow")], requested_effort=effort)
    assert target.reasoning_effort == (effort or models._curated_fallback(model, None).reasoning_effort)


@pytest.mark.parametrize("body", [{}, {"output_config": None}, {"output_config": {}},
                                  {"output_config": {"effort": None}}, {"output_config": {"effort": ""}}])
def test_absent_effort_forms(body):
    effort = models.requested_effort(body)
    assert effort is None
    assert models.map_model("claude-fable-5-1", parity=[row(effort="follow"), row(source_effort="max", effort="none")],
                            requested_effort=effort).reasoning_effort == "medium"


@pytest.mark.parametrize("overrides, expected", [
    ({"override_model": "profile"}, ("profile", "max")),
    ({"override_reasoning_effort": "none"}, ("override", "none")),
    ({"override_model": "profile", "override_reasoning_effort": "low"}, ("profile", "low")),
])
def test_profile_overrides_remain_independent_and_win(overrides, expected):
    rows = [row(effort="high"), row(source_effort="max", model="override", effort="follow")]
    assert models.map_model("claude-fable-5-1", parity=rows, requested_effort="max", **overrides) == models.OpenAIModelTarget(*expected)


@pytest.mark.parametrize("effort", EFFORTS)
def test_claude_injection_uses_same_override_matching(effort):
    rows = [row(claude_effort="medium"), row(source_effort="max", claude_effort="low")]
    assert models.claude_effort_for("claude-fable-5-1-20261001", rows, requested_effort=effort) == (
        "low" if effort == "max" else "medium")
    assert models.claude_effort_for("claude-fable-6", rows, requested_effort=effort) == (
        "low" if effort == "max" else "medium")


def test_grid_order_indices_follow_clamping_and_suppression():
    rows = [row("claude-opus-4-6", source_effort="max", model="custom", effort="follow", claude_effort="xhigh"),
            row("claude-haiku-5-5", effort="low", claude_effort="high"),
            row("claude-opus-4-6", effort="follow")]
    grid = models.parity_grid(rows)
    assert len(grid) == 12
    assert [r["requested_effort"] for r in grid[:6]] == list(EFFORTS)
    assert grid[0] == {"claude_model": "claude-opus-4-6", "requested_effort": None,
                       "row_index": 2, "model": "gpt-5.6-terra", "effort": "high",
                       "claude_effort": None, "note": "follows"}
    assert grid[5] == {"claude_model": "claude-opus-4-6", "requested_effort": "max",
                       "row_index": 0, "model": "custom", "effort": "max",
                       "claude_effort": "high", "note": "override; follows; clamped to high"}
    assert grid[6]["claude_effort"] is None
    assert grid[6]["note"] == "not supported by this model"
    assert grid[6]["row_index"] == 1
    for cell in grid:
        target = models.map_model(cell["claude_model"], parity=rows, requested_effort=cell["requested_effort"])
        assert (cell["model"], cell["effort"]) == (target.model, target.reasoning_effort)
        assert cell["claude_effort"] == models.claude_effort_for(
            cell["claude_model"], rows, requested_effort=cell["requested_effort"])


def test_models_listing_dedupes_and_uses_base_label_even_when_override_first():
    rows = [row(source_effort="low", model="override", effort="none"),
            row("claude-sonnet-5", effort="high"), row(model="base", effort="follow")]
    listing = connectors.models_listing("codex", rows)
    assert [m for m, _ in listing] == ["claude-fable-5-1", "claude-sonnet-5"]
    assert listing[0][1].endswith("| base · follows Claude Code")
    assert listing[1][1].endswith(" · high")


def test_defaults_follow_but_missing_saved_effort_is_concrete():
    assert all(r["effort"] == "follow" for r in models.default_parity_rows())
    for raw in (None, {}, []):
        assert all(r["effort"] == "follow" for r in models.normalize_parity(raw))
    for raw in ([row()], {"claude-fable-5-1": {"model": "legacy"}}):
        assert models.map_model("claude-fable-5-1", parity=raw, requested_effort="max").reasoning_effort == "medium"
    assert models.map_model("claude-sonnet-5", parity={"claude-fable-5-1": {"model": "legacy"}},
                            requested_effort="max").reasoning_effort == "medium"


LEGACY_ROWS = [
    row("claude-mythos-5-1", model="gpt-6-astra", effort="xhigh"),
    row("claude-fable-5-1", model="gpt-6-astra", effort="high"),
    row("claude-opus-5-5", model="gpt-5.6-sol", effort="high"),
    row("claude-sonnet-5-5", model="gpt-6-sol", effort="medium"),
    row("claude-haiku-5-5", model="gpt-6-luna", effort="low"),
]


@pytest.mark.parametrize("legacy", LEGACY_ROWS)
@pytest.mark.parametrize("effort", EFFORTS)
def test_live_five_rows_remain_identical_for_all_thirty_pairs(legacy, effort):
    # Exercise the same persisted list shape without touching the live config.
    config.CONFIG_FILE.write_text(json.dumps({"settings": {"model_parity": LEGACY_ROWS}}))
    rows = config.load_pool().settings.model_parity
    assert rows == LEGACY_ROWS
    assert models.map_model(legacy["claude_model"], parity=rows, requested_effort=effort) == models.OpenAIModelTarget(
        legacy["model"], legacy["effort"])
    assert models.claude_effort_for(legacy["claude_model"], rows, requested_effort=effort) is None


def test_profile_window_summary_includes_override_targets():
    profile = config.Profile(id="test", name="Test", kind="codex")
    rows = [row(model="gpt-6-astra"), row(source_effort="low", model="gpt-5.6-luna")]
    assert set(models.codex_profile_windows(profile, rows)["models"]) >= {"gpt-6-astra", "gpt-5.6-luna"}
