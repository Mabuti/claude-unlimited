import json

from claude_unlimited.config import (
    Pool, Profile, Settings, save_pool, load_pool, DEFAULT_SWITCH_THRESHOLD, CONFIG_FILE, _default_port,
)


def test_settings_launchers_and_port_defaults():
    s = Settings()
    assert s.launchers == {}
    assert s.port == _default_port()


def test_profile_defaults():
    p = Profile(id="a", name="Personal Max")
    assert p.kind == "oauth"
    assert p.switch_threshold == DEFAULT_SWITCH_THRESHOLD
    assert p.enabled is True
    assert p.automatic is False


def test_unified_api_kind_has_no_gateway_split():
    # There is deliberately no "gateway" kind — an API-kind Profile pointed at
    # a custom base_url IS the gateway case.
    p = Profile(id="g", name="Team Gateway", kind="api", base_url="https://gateway.example/v1")
    assert p.kind == "api"


def test_pool_enabled_profiles_filters_disabled():
    pool = Pool(profiles=[
        Profile(id="a", name="A", enabled=True),
        Profile(id="b", name="B", enabled=False),
    ])
    assert [p.id for p in pool.enabled_profiles()] == ["a"]


def test_pool_get_by_id():
    pool = Pool(profiles=[Profile(id="a", name="A"), Profile(id="b", name="B")])
    assert pool.get("b").name == "B"
    assert pool.get("missing") is None


def test_save_and_load_pool_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")

    pool = Pool(profiles=[
        Profile(id="a", name="Personal Max", kind="oauth", priority=1, switch_threshold=98.0, automatic=True,
                account_uuid="uuid-a", plan="max"),
        Profile(id="b", name="Team Gateway", kind="api", base_url="https://gw.example", priority=4,
                monthly_budget_cap=50.0, token_threshold=250000, tag_color="#43C6FF"),
    ])
    save_pool(pool)

    loaded = load_pool()
    assert [p.id for p in loaded.profiles] == ["a", "b"]
    assert loaded.profiles[1].monthly_budget_cap == 50.0
    assert loaded.profiles[1].tag_color == "#43C6FF"
    # Full dataclass equality, not spot-checked fields: load_pool() rebuilds
    # Profile field-by-field, so a field added to Profile but not to that
    # reconstruction would silently reset to its default on every load.
    assert loaded.profiles[0] == pool.profiles[0]
    assert loaded.profiles[1] == pool.profiles[1]


def test_org_uuid_and_organization_type_round_trip_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")

    pool = Pool(profiles=[
        Profile(id="a", name="Org Seat", kind="oauth", account_uuid="uuid-a",
                org_uuid="org-a", organization_type="claude_team", plan="team"),
    ])
    save_pool(pool)

    loaded = load_pool()
    assert loaded.profiles[0].org_uuid == "org-a"
    assert loaded.profiles[0].organization_type == "claude_team"
    assert loaded.profiles[0] == pool.profiles[0]


def test_profile_without_org_uuid_or_organization_type_loads_with_none_for_both(tmp_path, monkeypatch):
    # Backward-compatibility guarantee: a config.json written before A3 has
    # neither key on a profile entry at all, and load_pool() must not KeyError
    # — both fields come back None, exactly like every other optional field.
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", cfg_file)
    cfg_file.write_text(json.dumps({
        "profiles": [{"id": "a", "name": "Personal Max", "kind": "oauth", "account_uuid": "uuid-a", "plan": "max"}],
        "settings": {},
    }))

    loaded = load_pool()
    assert loaded.profiles[0].org_uuid is None
    assert loaded.profiles[0].organization_type is None
    assert loaded.profiles[0].account_uuid == "uuid-a"  # unaffected sibling field still loads


def test_launchers_and_port_round_trip_through_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")

    pool = Pool(settings=Settings(launchers={"claude": "claude --dangerously-skip-permissions"}, port=9999))
    save_pool(pool)

    loaded = load_pool()
    assert loaded.settings.launchers == {"claude": "claude --dangerously-skip-permissions"}
    assert loaded.settings.port == 9999


def test_config_without_launchers_or_port_keys_loads_with_backward_compatible_defaults(tmp_path, monkeypatch):
    # Every config.json written before this ticket has no "launchers"/"port"
    # key under settings at all — load_pool() must not KeyError on it.
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", cfg_file)
    cfg_file.write_text(json.dumps({"profiles": [], "settings": {"update_mode": "manual"}}))

    loaded = load_pool()
    assert loaded.settings.launchers == {}
    assert loaded.settings.port == _default_port()
    assert loaded.settings.update_mode == "manual"


def test_save_pool_writes_atomically_no_leftover_tmp_file(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    cfg_file = tmp_path / "config.json"
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", cfg_file)

    save_pool(Pool(profiles=[Profile(id="a", name="A")]))

    assert cfg_file.exists()
    assert not (tmp_path / "config.json.tmp").exists()
    data = json.loads(cfg_file.read_text())
    assert data["profiles"][0]["id"] == "a"
