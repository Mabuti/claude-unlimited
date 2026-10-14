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
    ([row(), row("claude-fable-5-1-20261001", source_effort="max")], "base row"),
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


def test_legacy_dict_accepts_distinct_exact_spellings():
    raw = {"claude-fable-5": {"effort": "high"}, "claude-fable-5-20261001": {"effort": "low"}}
    assert config._validated_model_parity(raw) == raw


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
                       "row_index": 2, "source_effort": "*", "model": "gpt-5.6-terra", "effort": "high",
                       "claude_effort": None, "configured_claude_effort": None, "note": "follows"}
    assert grid[5] == {"claude_model": "claude-opus-4-6", "requested_effort": "max",
                       "row_index": 0, "source_effort": "max", "model": "custom", "effort": "max",
                       "claude_effort": "high", "configured_claude_effort": "xhigh", "note": "override; follows; clamped to high"}
    assert grid[6]["claude_effort"] is None
    assert grid[6]["configured_claude_effort"] == "high"
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
        assert [r["effort"] for r in models.normalize_parity(raw)] == ["medium", "high", "medium", "low"]
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


@pytest.mark.parametrize("effort", EFFORTS)
@pytest.mark.parametrize("shape", ["dict", "list"])
@pytest.mark.parametrize("stem, date, overlay", [
    ("claude-mythos-5-1", "20261001", {"model": "gpt-6-luna", "effort": "low"}),
    ("claude-opus-5-5", "20260101", {"effort": "low"}),
])
def test_legacy_spellings_pin_ad6ef5f_results(effort, shape, stem, date, overlay):
    dated = f"{stem}-{date}"
    raw = {stem: {"model": "gpt-6-astra", "effort": "xhigh"}, dated: overlay}
    if shape == "list":
        raw = [dict(claude_model=k, **v) for k, v in raw.items()]
    # Pinned from ad6ef5f: the legacy dict overlays curated default rows
    # by base id, whereas a list preserves each spelling's separate row.
    if stem == "claude-opus-5-5" and shape == "dict":
        expected_base = expected_dated = ("gpt-6-astra", "low")
    else:
        expected_base = ("gpt-6-astra", "xhigh")
        expected_dated = ("gpt-6-luna" if "mythos" in stem else "gpt-5.6-terra", "low")
    for requested, expected in [(stem, expected_base), (dated, expected_dated),
                                (stem + "-20261201", expected_base)]:
        assert models.map_model(requested, parity=raw, requested_effort=effort) == models.OpenAIModelTarget(*expected)
    validated = config._validated_model_parity(raw)
    config.update_settings(model_parity=validated)
    assert config.load_pool().settings.model_parity == raw


@pytest.mark.parametrize("effort", EFFORTS)
def test_dated_and_undated_groups_keep_their_own_effort_overrides(effort):
    base, dated = "claude-fable-5-1", "claude-fable-5-1-20261001"
    rows = [row(base, model="base", claude_effort="medium"),
            row(dated, model="dated", claude_effort="low"),
            row(base, source_effort="max", model="base-max", claude_effort="high"),
            row(dated, source_effort="max", model="dated-max", claude_effort="max")]
    assert config._validated_model_parity(rows) == rows
    for model, target, injection in [(base, "base", "medium"), (dated, "dated", "low")]:
        if effort == "max":
            target += "-max"
            injection = "high" if model == base else "max"
        assert models.map_model(model, parity=rows, requested_effort=effort).model == target
        assert models.claude_effort_for(model, rows, requested_effort=effort) == injection
    assert len(models.parity_grid(rows)) == 12


@pytest.mark.parametrize("shape", ["dict", "list"])
def test_whitespace_duplicate_keys_pin_ad6ef5f_results(shape):
    raw = {"claude-opus-5": {"effort": "high"}, " claude-opus-5 ": {"effort": "low"}}
    # Executed against ad6ef5f: dict strips keys with last non-empty entry
    # winning; the equivalent list rejects duplicate Claude models.
    if shape == "list":
        with pytest.raises(ValueError, match="duplicate Claude model"):
            config._validated_model_parity([dict(claude_model=k, **v) for k, v in raw.items()])
        return
    cleaned = config._validated_model_parity(raw)
    assert cleaned == {"claude-opus-5": {"effort": "low"}}
    target = [r for r in models.normalize_parity(cleaned) if r["claude_model"] == "claude-opus-5"]
    assert target == [{"claude_model": "claude-opus-5", "model": "gpt-5.6-terra",
                       "effort": "low", "claude_effort": None}]


# Executed against ad6ef5f without a loaded catalogue: all empty shapes yield
# these exact rows; every incoming effort keeps each concrete target unchanged.
LEGACY_EMPTY_ROWS = [
    row('claude-fable-5', model='gpt-5.6-sol', effort='medium', claude_effort=None),
    row('claude-opus-5-5', model='gpt-5.6-terra', effort='high', claude_effort=None),
    row('claude-sonnet-5', model='gpt-5.6-terra', effort='medium', claude_effort=None),
    row('claude-haiku-4-5-20251001', model='gpt-5.6-luna', effort='low', claude_effort=None),
]


@pytest.mark.parametrize('shape', ['absent', {}, []])
@pytest.mark.parametrize('effort', EFFORTS)
def test_legacy_empty_configs_pin_ad6ef5f_without_writes(shape, effort, monkeypatch):
    monkeypatch.setattr(models.model_catalogue, 'current', lambda: None)
    settings = {} if shape == 'absent' else {'model_parity': shape}
    original = json.dumps({'settings': settings})
    config.CONFIG_FILE.write_text(original)
    before = config.CONFIG_FILE.stat().st_mtime_ns
    pool = config.load_pool()
    assert models.normalize_parity(pool.settings.model_parity) == LEGACY_EMPTY_ROWS
    for legacy in LEGACY_EMPTY_ROWS:
        for model_id in (legacy['claude_model'], legacy['claude_model'] + '-20261001'):
            assert models.map_model(model_id, parity=pool.settings.model_parity,
                                    requested_effort=effort) == models.OpenAIModelTarget(
                                        legacy['model'], legacy['effort'])
    assert config.CONFIG_FILE.read_text() == original
    assert config.CONFIG_FILE.stat().st_mtime_ns == before
    config.update_settings(language='de')
    assert models.normalize_parity(config.load_pool().settings.model_parity) == LEGACY_EMPTY_ROWS


def test_first_config_creation_writes_explicit_follow_rows():
    assert not config.CONFIG_FILE.exists()
    pool = config.load_pool()
    config.save_pool(pool)
    stored = json.loads(config.CONFIG_FILE.read_text())['settings']['model_parity']
    assert stored == models.default_parity_rows()
    assert len(stored) == 4
    config.save_pool(pool)
    assert config.load_pool().settings.model_parity == stored
    assert all(r['effort'] == 'follow' for r in stored)
    assert models.map_model('claude-fable-5-1', parity=config.load_pool().settings.model_parity,
                            requested_effort='max').reasoning_effort == 'max'


def test_explicit_reset_defaults_persist_and_follow_all_levels():
    config.CONFIG_FILE.write_text(json.dumps({'settings': {'model_parity': LEGACY_ROWS}}))
    config.update_settings(model_parity=models.default_parity_rows())
    stored = config.load_pool().settings.model_parity
    assert models.normalize_parity(stored) == models.default_parity_rows()
    for effort in models.CLAUDE_REASONING_EFFORTS:
        for r in stored:
            assert models.map_model(r['claude_model'], parity=stored,
                                    requested_effort=effort).reasoning_effort == effort


def test_server_accepts_64_rows_and_rejects_65():
    rows = [row(f'claude-test-{i}', effort='follow') for i in range(65)]
    assert len(config._validated_model_parity(rows[:64])) == 64
    with pytest.raises(ValueError, match='too many'):
        config._validated_model_parity(rows)


def test_offline_picker_model_and_effort_are_neutral():
    from claude_unlimited import cli
    for labels in (cli._CODEX_MODEL_LABELS, cli._MIXED_MODEL_LABELS):
        for tier, (name, description) in labels.items():
            assert "GPT" not in name
            assert "mapping unavailable" in description
            assert 'follows Claude Code' not in description


@pytest.mark.parametrize("shape", ["absent", {}, [], None])
@pytest.mark.parametrize("effort", EFFORTS)
def test_import_legacy_settings_into_fresh_install_pins_ad6ef5f(shape, effort, monkeypatch):
    from claude_unlimited import export_import as ei
    monkeypatch.setattr(models.model_catalogue, "current", lambda: None)
    monkeypatch.setattr(ei.activity_module, "record", lambda *a, **kw: None)
    assert not config.CONFIG_FILE.exists()
    settings = {} if shape == "absent" else {"model_parity": shape}
    bundle = json.dumps({"bundle_version": ei.BUNDLE_VERSION, "encrypted": False,
                         "data": {"settings": settings}}).encode()
    parsed = ei.import_bundle(bundle)
    result = ei.apply_import(parsed, import_profiles=False, import_settings=True)
    assert result["settings_applied"] is True
    stored = json.loads(config.CONFIG_FILE.read_text())["settings"]["model_parity"]
    assert models.normalize_parity(stored) == LEGACY_EMPTY_ROWS
    assert isinstance(stored, list) and len(stored) == 4
    for legacy in LEGACY_EMPTY_ROWS:
        assert models.map_model(legacy["claude_model"], parity=stored,
                                requested_effort=effort) == models.OpenAIModelTarget(
                                    legacy["model"], legacy["effort"])


@pytest.mark.parametrize("shape", ["absent", {}, [], None, LEGACY_ROWS[:2]])
def test_import_existing_settings_preserves_or_replaces_parity(shape, monkeypatch):
    from claude_unlimited import export_import as ei
    monkeypatch.setattr(models.model_catalogue, "current", lambda: None)
    monkeypatch.setattr(ei.activity_module, "record", lambda *a, **kw: None)
    config.CONFIG_FILE.write_text(json.dumps({"settings": {
        "language": "en", "model_parity": LEGACY_ROWS}}))
    before = {(r["claude_model"], e): models.map_model(
        r["claude_model"], parity=LEGACY_ROWS, requested_effort=e)
        for r in LEGACY_ROWS for e in EFFORTS}
    settings = {"language": "de"}
    if shape != "absent":
        settings["model_parity"] = shape
    parsed = ei.ParsedBundle(profiles=[], settings=settings, activity=None)
    ei.apply_import(parsed, import_profiles=False, import_settings=True)
    stored = json.loads(config.CONFIG_FILE.read_text())["settings"]
    assert stored["language"] == "de"
    expected = LEGACY_ROWS if shape == "absent" else (shape or LEGACY_EMPTY_ROWS)
    if shape == "absent" or shape:
        assert stored["model_parity"] == expected
    else:
        assert isinstance(stored["model_parity"], list)
        assert [r["effort"] for r in stored["model_parity"]] == ["medium", "high", "medium", "low"]
        assert models.normalize_parity(stored["model_parity"]) == expected
    if shape == "absent":
        assert {(model, e): models.map_model(model, parity=stored["model_parity"],
                                            requested_effort=e)
                for model, e in before} == before


def test_fresh_import_checks_config_existence_before_profile_writes(monkeypatch):
    from claude_unlimited import export_import as ei
    monkeypatch.setattr(models.model_catalogue, "current", lambda: None)
    monkeypatch.setattr(ei.activity_module, "record", lambda *a, **kw: None)
    monkeypatch.setattr(ei.secret_store, "set_token", lambda *a: None)
    assert not config.CONFIG_FILE.exists()
    parsed = ei.ParsedBundle(
        profiles=[{"name": "Fixture", "kind": "api", "credential": "fixture"}],
        settings={"language": "de"}, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=True)
    assert result["profiles_added"] == 1
    stored = json.loads(config.CONFIG_FILE.read_text())["settings"]
    assert stored["language"] == "de"
    assert isinstance(stored["model_parity"], list)
    assert [r["effort"] for r in stored["model_parity"]] == ["medium", "high", "medium", "low"]
    assert models.normalize_parity(stored["model_parity"]) == LEGACY_EMPTY_ROWS


@pytest.mark.parametrize("shape", [{}, []])
def test_saving_legacy_empty_pool_to_missing_file_does_not_opt_into_follow(shape):
    config.save_pool(config.Pool(settings=config.Settings(model_parity=shape)))
    assert config.load_pool().settings.model_parity == {}
    assert models.normalize_parity(config.load_pool().settings.model_parity) == LEGACY_EMPTY_ROWS


def test_grid_missing_base_uses_curated_fallback():
    rows = [row("claude-sonnet-5", source_effort="high", model="custom", effort="max")]
    config.CONFIG_FILE.write_text(json.dumps({"settings": {"model_parity": rows}}))
    grid = models.parity_grid(config.load_pool().settings.model_parity)
    for cell in grid:
        if cell["requested_effort"] == "high":
            assert (cell["model"], cell["effort"], cell["row_index"]) == ("custom", "max", 0)
            assert cell["note"] == "override"
        else:
            assert (cell["model"], cell["effort"]) == ("gpt-5.6-terra", "medium")
            assert cell["note"] == "no base row (fallback)"
            assert cell["row_index"] is None
            assert cell["source_effort"] is None
