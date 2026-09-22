import base64
import json

import pytest

import claude_unlimited.activity as activity
import claude_unlimited.anthropic_oauth as anthropic_oauth
import claude_unlimited.export_import as ei
import claude_unlimited.openai_credential as openai_credential
import claude_unlimited.profiles as profile_repo
from claude_unlimited.config import load_pool


def _codex_id_token(user_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps(
        {"https://api.openai.com/auth": {"chatgpt_user_id": user_id}}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}.fakesig"


def _codex_encoded(account_id: str, *, user_id: str = None, access_token: str = "tok") -> str:
    id_token = _codex_id_token(user_id) if user_id else None
    return openai_credential.encode(openai_credential.StoredOpenAICredential(
        access_token=access_token, refresh_token=None, account_id=account_id, id_token=id_token))


class FakeSecretStore:
    def __init__(self):
        self.tokens = {}

    def set_token(self, profile_id, token):
        self.tokens[profile_id] = token

    def get_token(self, profile_id):
        return self.tokens[profile_id]

    def delete_token(self, profile_id):
        self.tokens.pop(profile_id, None)

    def has_token(self, profile_id):
        return profile_id in self.tokens


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = FakeSecretStore()
    monkeypatch.setattr(profile_repo, "secret_store", store)
    monkeypatch.setattr(ei, "secret_store", store)
    monkeypatch.setattr("claude_unlimited.config.APP_DIR", tmp_path)
    monkeypatch.setattr("claude_unlimited.config.CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(activity, "APP_DIR", tmp_path)
    monkeypatch.setattr(activity, "ACTIVITY_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(ei, "activity_module", activity)
    return store


def _account(**overrides):
    base = dict(
        account_uuid="uuid-shared", email="dev@example.com", display_name="Dev",
        org_uuid="org-x", org_name="Some Org", organization_type="claude_max",
        has_claude_max=True, has_claude_pro=False,
    )
    base.update(overrides)
    return anthropic_oauth.AccountProfile(**base)


def _fetch_by_token(mapping):
    """Same shape as test_account_identity_legacy_guard.py's helper: a
    fetch_account_profile fake keyed by access token, so a test can tell the
    "existing Profile's own credential" resolution apart from the "incoming
    bundle item's credential" resolution in one call."""
    def fake(access_token, timeout=15.0):
        try:
            return mapping[access_token]
        except KeyError:
            raise anthropic_oauth.ProfileLookupError(f"no fixture for token {access_token!r}")
    return fake


def test_export_profiles_requires_passphrase(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    with pytest.raises(ei.ExportImportError):
        ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False)


def test_export_settings_only_needs_no_passphrase_and_is_plaintext(env):
    bundle = ei.build_export_bundle(include_profiles=False, include_settings=True, include_activity=False)
    envelope = json.loads(bundle)
    assert envelope["encrypted"] is False
    assert "settings" in envelope["data"]


def test_export_import_roundtrip_with_correct_passphrase(env):
    profile_repo.create_profile(name="Personal Max", kind="oauth", credential="tok-real-long", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=True, include_activity=False,
                                     passphrase="correct horse battery staple")

    envelope = json.loads(bundle)
    assert envelope["encrypted"] is True
    assert "profiles" not in envelope  # not visible in the clear anywhere in the envelope

    parsed = ei.import_bundle(bundle, passphrase="correct horse battery staple")
    assert len(parsed.profiles) == 1
    assert parsed.profiles[0]["credential"] == "tok-real-long"
    assert parsed.profiles[0]["name"] == "Personal Max"


def test_import_wrong_passphrase_raises_specific_error(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="right-passphrase")
    with pytest.raises(ei.WrongPassphraseError):
        ei.import_bundle(bundle, passphrase="wrong-passphrase")


def test_import_encrypted_bundle_without_passphrase_raises_clear_error(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="secret")
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(bundle)


def test_import_bundle_never_writes_anything(env):
    profile_repo.create_profile(name="X", kind="oauth", credential="tok-long-enough", account_uuid="u1")
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="secret")
    ei.import_bundle(bundle, passphrase="secret")
    # Still exactly the one original profile — import_bundle is read-only.
    assert len(load_pool().profiles) == 1


def test_apply_import_adds_new_profile(env):
    bundle_profiles = [{
        "name": "Imported", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "new-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)
    assert result["profiles_added"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert env.get_token(pool.profiles[0].id) == "imported-tok-long"


def test_apply_import_keep_existing_skips_conflicting_profile(env):
    profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long", account_uuid="dup-uuid")
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="keep_existing")
    assert result["profiles_skipped"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].name == "Existing"


def test_apply_import_use_imported_updates_conflicting_credential(env, monkeypatch):
    existing = profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long",
                                            account_uuid="dup-uuid")
    # Both the existing Profile's own stored credential and the incoming
    # bundle item's credential resolve to the SAME organization — a
    # genuinely safe reuse, not the org_uuid=None-on-both-sides guess the
    # old code made. See test_apply_import_bundle_without_org_uuid_legacy_
    # matches_is_now_blocked_when_unresolvable for the unsafe counterpart.
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "original-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
        "imported-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
    }))
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    assert env.get_token(existing.id) == "imported-tok-long"
    pool = load_pool()
    assert len(pool.profiles) == 1  # still no duplicate row


def test_apply_import_use_imported_updates_the_whole_profile_not_just_credential(env, monkeypatch):
    # "Use imported version" must apply every bundle field (name, priority,
    # threshold, enabled, automatic, tag_color, base_url, default_model,
    # budget cap), not just the stored credential.
    existing = profile_repo.create_profile(name="Existing", kind="oauth", credential="original-tok-long",
                                            account_uuid="dup-uuid", priority=1, switch_threshold=98.0)
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "original-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
        "imported-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
    }))
    bundle_profiles = [{
        "name": "Renamed on the other machine", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 3, "switch_threshold": 90.0, "enabled": False, "automatic": False,
        "default_model": "claude-opus-5", "monthly_budget_cap": 50.0, "tag_color": "#FFB020",
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1
    updated = pool.profiles[0]
    assert updated.id == existing.id  # same row, not a duplicate
    assert updated.name == "Renamed on the other machine"
    assert updated.priority == 3
    assert updated.switch_threshold == 90.0
    assert updated.enabled is False
    assert updated.automatic is False
    assert updated.default_model == "claude-opus-5"
    assert updated.monthly_budget_cap == 50.0
    assert updated.tag_color == "#FFB020"


def test_export_then_apply_import_roundtrips_token_threshold(env):
    # A field added to ExportedProfile/build_export_bundle/apply_import in only
    # two of those three spots is silently dropped.
    profile_repo.create_profile(name="X", kind="api", credential="tok-long-enough-key", token_threshold=250000)
    bundle = ei.build_export_bundle(include_profiles=True, include_settings=False, include_activity=False,
                                     passphrase="correct horse battery staple")
    parsed = ei.import_bundle(bundle, passphrase="correct horse battery staple")
    assert parsed.profiles[0]["token_threshold"] == 250000

    # No account_uuid on an api-kind Profile — nothing to match against, so
    # this exercises apply_import's ADD path (a fresh new_profile row).
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)
    assert result["profiles_added"] == 1
    imported = [p for p in load_pool().profiles if p.name == "X"][-1]
    assert imported.token_threshold == 250000


def test_apply_import_settings(env):
    parsed = ei.ParsedBundle(profiles=[], settings={"update_mode": "manual"}, activity=None)
    result = ei.apply_import(parsed, import_profiles=False, import_settings=True)
    assert result["settings_applied"] is True
    assert load_pool().settings.update_mode == "manual"


def test_unsupported_bundle_version_rejected(env):
    bad = json.dumps({"bundle_version": 999, "encrypted": False, "data": {}}).encode()
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(bad)


def test_not_json_raises_clear_error(env):
    with pytest.raises(ei.ExportImportError):
        ei.import_bundle(b"not json at all")


def test_the_account_tier_survives_an_export_import_round_trip(env):
    """`plan` ("max"/"pro") is discovered only by the add-account and
    import-login flows, and nothing recomputes it afterwards — so a Profile
    restored from a bundle used to show no tier in the Dashboard forever."""
    profile_repo.create_profile(name="Max account", kind="oauth", credential="sk-ant-12345678",
                            account_uuid="acct-1", plan="max")
    bundle = ei.build_export_bundle(
        include_profiles=True, include_settings=False, include_activity=False,
        passphrase="hunter22")

    profile_repo.reset_all_profiles()
    parsed = ei.import_bundle(bundle, passphrase="hunter22")
    ei.apply_import(parsed, import_profiles=True, import_settings=False)

    assert [p.plan for p in profile_repo.list_profiles()] == ["max"]


def test_apply_import_bundle_with_org_uuid_pair_matches(env):
    # A bundle written after this fix carries org_uuid — an exact pair match
    # (account_uuid AND org_uuid) reuses the existing Profile.
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="original-tok-long",
        account_uuid="uuid-1", org_uuid="org-1", organization_type="claude_max")
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "uuid-1", "org_uuid": "org-1", "organization_type": "claude_max",
        "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    pool = load_pool()
    assert len(pool.profiles) == 1  # no duplicate
    assert pool.profiles[0].id == existing.id


def test_apply_import_bundle_with_org_uuid_different_from_existing_org_adds_second_profile(env):
    # Same account_uuid, different org_uuid — the bundle carries a second
    # subscription for an account already partly in the pool, and it must
    # be added rather than overwriting the first.
    profile_repo.create_profile(
        name="Org Seat", kind="oauth", credential="team-tok-long",
        account_uuid="uuid-shared", org_uuid="org-team", organization_type="claude_team")
    bundle_profiles = [{
        "name": "Personal Max", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "uuid-shared", "org_uuid": "org-personal-max", "organization_type": "claude_max",
        "credential": "personal-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)
    assert result["profiles_added"] == 1
    profiles = load_pool().profiles
    assert len(profiles) == 2
    assert {p.org_uuid for p in profiles} == {"org-team", "org-personal-max"}


def test_apply_import_bundle_without_org_uuid_and_unresolvable_is_blocked_not_overwritten(env, monkeypatch):
    # Finding 2 regression. Before this fix, a bundle item with no org_uuid
    # matched an existing org_uuid=None Profile on account_uuid ALONE and
    # apply_import() called secret_store.set_token() on it directly —
    # find_by_account_and_org()'s legacy fallback was never routed through
    # resolve_legacy_oauth_match() at all. That is the 2026-09-22 incident
    # from the import side: neither side's organization is proven, so
    # "match on account_uuid alone" is exactly the unsafe guess this whole
    # fix exists to remove. Both credentials are unresolvable here (network
    # never mocked to succeed) — the safe default is to leave the existing
    # Profile alone and report it, never to overwrite it on a guess.
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="original-tok-long", account_uuid="dup-uuid")
    assert existing.org_uuid is None
    original_stored = env.get_token(existing.id)

    def boom(access_token, timeout=15.0):
        raise anthropic_oauth.ProfileLookupError("token no longer valid")

    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", boom)

    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_overwrite_blocked"] == 1
    assert result["profiles_updated"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1  # no duplicate created either
    assert pool.profiles[0].id == existing.id
    assert pool.profiles[0].name == "Existing"       # untouched
    assert env.get_token(existing.id) == original_stored  # byte-for-byte unchanged


def test_apply_import_bundle_without_org_uuid_but_resolves_to_same_org_still_updates(env, monkeypatch):
    # The safe counterpart: no org_uuid key in the bundle, but the incoming
    # item's OWN credential resolves to the same organization the existing
    # Profile's own credential resolves to — a genuine match, proven from
    # both sides' credentials rather than assumed from account_uuid alone.
    existing = profile_repo.create_profile(
        name="Existing", kind="oauth", credential="original-tok-long", account_uuid="dup-uuid")
    monkeypatch.setattr(anthropic_oauth, "fetch_account_profile", _fetch_by_token({
        "original-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
        "imported-tok-long": _account(account_uuid="dup-uuid", org_uuid="org-x"),
    }))
    bundle_profiles = [{
        "name": "Imported version", "kind": "oauth", "base_url": None, "auth_mode": "api_key",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "dup-uuid", "credential": "imported-tok-long",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")
    assert result["profiles_updated"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1  # no duplicate
    assert pool.profiles[0].id == existing.id
    assert env.get_token(existing.id) == "imported-tok-long"


def test_org_uuid_and_organization_type_survive_an_export_import_round_trip(env):
    profile_repo.create_profile(
        name="Org Seat", kind="oauth", credential="sk-ant-12345678",
        account_uuid="acct-1", org_uuid="org-team", organization_type="claude_team")
    bundle = ei.build_export_bundle(
        include_profiles=True, include_settings=False, include_activity=False,
        passphrase="hunter22")

    profile_repo.reset_all_profiles()
    parsed = ei.import_bundle(bundle, passphrase="hunter22")
    assert parsed.profiles[0]["org_uuid"] == "org-team"
    assert parsed.profiles[0]["organization_type"] == "claude_team"

    ei.apply_import(parsed, import_profiles=True, import_settings=False)

    restored = profile_repo.list_profiles()
    assert len(restored) == 1
    assert restored[0].org_uuid == "org-team"
    assert restored[0].organization_type == "claude_team"


def test_codex_user_id_survives_an_export_import_round_trip(env):
    profile_repo.create_profile(
        name="Codex", kind="codex", credential=_codex_encoded("acct-1", user_id="user-a"),
        auth_mode="chatgpt_subscription", account_uuid="acct-1",
        credential_already_encoded=True, codex_user_id="user-a")
    bundle = ei.build_export_bundle(
        include_profiles=True, include_settings=False, include_activity=False,
        passphrase="hunter22")

    profile_repo.reset_all_profiles()
    parsed = ei.import_bundle(bundle, passphrase="hunter22")
    assert parsed.profiles[0]["codex_user_id"] == "user-a"

    ei.apply_import(parsed, import_profiles=True, import_settings=False)

    restored = profile_repo.list_profiles()
    assert len(restored) == 1
    assert restored[0].codex_user_id == "user-a"


def test_apply_import_codex_item_same_user_updates_in_place(env):
    existing = profile_repo.create_profile(
        name="Existing", kind="codex", credential=_codex_encoded("acct-1", user_id="user-a", access_token="tok-old"),
        auth_mode="chatgpt_subscription", account_uuid="acct-1",
        credential_already_encoded=True, codex_user_id="user-a")

    new_cred = _codex_encoded("acct-1", user_id="user-a", access_token="tok-new")
    bundle_profiles = [{
        "name": "Imported version", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-1", "credential": new_cred, "codex_user_id": "user-a",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_updated"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].id == existing.id
    assert env.get_token(existing.id) == new_cred


def test_apply_import_codex_item_distinct_resolved_user_is_added_not_blocked(env):
    """F1 regression (verifier finding, 2026-09-22): a codex item sharing an
    existing Profile's account_id but belonging to a DIFFERENT, fully
    RESOLVED ChatGPT user is a genuinely distinct person — the incident's
    own shape (seats A and B on one chatgpt_account_id) — and must be
    ADDED as a second Profile, never blocked and never merged into A.
    Before the fix, find_codex_profile() reported this the same way as a
    truly ambiguous (unresolvable) candidate, so apply_import() refused to
    add seat B at all: a second seat silently dropped on migration."""
    existing = profile_repo.create_profile(
        name="User A", kind="codex", credential=_codex_encoded("acct-shared", user_id="user-a", access_token="tok-a"),
        auth_mode="chatgpt_subscription", account_uuid="acct-shared",
        credential_already_encoded=True, codex_user_id="user-a")
    original_stored = env.get_token(existing.id)

    other_cred = _codex_encoded("acct-shared", user_id="user-b", access_token="tok-b")
    bundle_profiles = [{
        "name": "User B", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": other_cred, "codex_user_id": "user-b",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_added"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    assert result["profiles_updated"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 2
    seat_a = next(p for p in pool.profiles if p.id == existing.id)
    seat_b = next(p for p in pool.profiles if p.id != existing.id)
    assert seat_a.name == "User A"                         # untouched
    assert env.get_token(existing.id) == original_stored   # byte-for-byte unchanged
    assert seat_b.name == "User B"
    assert seat_b.codex_user_id == "user-b"
    assert env.get_token(seat_b.id) == other_cred


def test_apply_import_two_codex_seats_into_empty_pool_both_added(env):
    """The clean-migration case F1 exists to protect: two distinct ChatGPT
    users sharing one chatgpt_account_id, imported into a pool that has
    neither yet, must both land as separate Profiles."""
    bundle_profiles = [
        {
            "name": "User A", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
            "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
            "default_model": None, "monthly_budget_cap": None, "tag_color": None,
            "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-a"),
            "codex_user_id": "user-a",
        },
        {
            "name": "User B", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
            "priority": 2, "switch_threshold": 98.0, "enabled": True, "automatic": True,
            "default_model": None, "monthly_budget_cap": None, "tag_color": None,
            "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-b"),
            "codex_user_id": "user-b",
        },
    ]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False)

    assert result["profiles_added"] == 2
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 2
    assert {p.codex_user_id for p in pool.profiles} == {"user-a", "user-b"}
    assert pool.profiles[0].id != pool.profiles[1].id


def test_apply_import_codex_item_against_unresolvable_legacy_candidate_still_blocked(env):
    """The genuinely ambiguous case is still blocked: an existing candidate
    whose OWN identity can't be resolved at all (no id_token in its stored
    credential) can't be ruled out as this item's person, so this must
    still refuse rather than guess either way."""
    unresolvable = profile_repo.create_profile(
        name="Legacy", kind="codex", credential=_codex_encoded("acct-shared", access_token="tok-raw"),  # no id_token
        auth_mode="chatgpt_subscription", account_uuid="acct-shared", credential_already_encoded=True)
    original_stored = env.get_token(unresolvable.id)

    bundle_profiles = [{
        "name": "Incoming", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-a"),
        "codex_user_id": "user-a",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_overwrite_blocked"] == 1
    assert result["profiles_added"] == 0
    assert result["profiles_updated"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert env.get_token(unresolvable.id) == original_stored  # untouched


def test_apply_import_codex_item_blocked_not_raised_when_candidates_token_missing_from_store(env):
    """M18 regression from the import side: a same-account_id candidate
    whose token has gone missing from the store entirely (get_token raises)
    must be treated as unresolved and blocked — never let that exception
    propagate out of apply_import() and crash the whole import."""
    legacy = profile_repo.create_profile(
        name="Legacy", kind="codex", credential=_codex_encoded("acct-1", user_id="user-a"),
        auth_mode="chatgpt_subscription", account_uuid="acct-1", credential_already_encoded=True)
    env.delete_token(legacy.id)  # token now entirely missing from the store

    bundle_profiles = [{
        "name": "Incoming", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-1", "credential": _codex_encoded("acct-1", user_id="user-a"),
        "codex_user_id": "user-a",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_overwrite_blocked"] == 1
    assert result["profiles_added"] == 0
    assert result["profiles_updated"] == 0
    assert len(load_pool().profiles) == 1


def test_apply_import_codex_item_falls_back_to_bundle_field_when_credential_has_no_user_id(env):
    """M9 regression: when an item's own shipped credential yields NO user
    id at all (a raw, non-JSON credential shape — no id_token to read),
    apply_import() must fall back to the bundle's own codex_user_id field
    rather than treating the identity as unknown and blocking. Here the
    field correctly names the existing Profile's real user, so this must
    update in place."""
    existing = profile_repo.create_profile(
        name="Existing", kind="codex", credential=_codex_encoded("acct-1", user_id="user-a", access_token="tok-old"),
        auth_mode="chatgpt_subscription", account_uuid="acct-1",
        credential_already_encoded=True, codex_user_id="user-a")

    no_id_token_cred = _codex_encoded("acct-1", access_token="tok-new")  # no user_id -> no id_token
    item = {
        "name": "Imported version", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-1", "credential": no_id_token_cred, "codex_user_id": "user-a",
    }
    parsed = ei.ParsedBundle(profiles=[item], settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_updated"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    assert result["profiles_added"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].id == existing.id
    assert env.get_token(existing.id) == no_id_token_cred


def test_apply_import_codex_item_keep_existing_skips_unresolvable_candidate(env):
    """conflict_strategy="keep_existing" never even attempts resolution for
    a genuinely ambiguous (unresolvable) candidate — same short-circuit as
    the oauth branch's keep_existing skip: existing Profile kept, nothing
    written, counted as skipped rather than blocked."""
    profile_repo.create_profile(
        name="Legacy", kind="codex", credential=_codex_encoded("acct-shared", access_token="tok-raw"),  # no id_token
        auth_mode="chatgpt_subscription", account_uuid="acct-shared", credential_already_encoded=True)

    bundle_profiles = [{
        "name": "Incoming", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-a"),
        "codex_user_id": "user-a",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="keep_existing")

    assert result["profiles_skipped"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    assert result["profiles_added"] == 0
    assert len(load_pool().profiles) == 1


def test_apply_import_codex_item_keep_existing_still_adds_a_distinct_resolved_user(env):
    """The counterpart to the skip above: under keep_existing, a distinct
    RESOLVED user is not a conflict at all (see
    test_apply_import_codex_item_distinct_resolved_user_is_added_not_blocked),
    so it is added exactly like any other non-conflicting new item would
    be under keep_existing (see test_apply_import_adds_new_profile)."""
    profile_repo.create_profile(
        name="User A", kind="codex", credential=_codex_encoded("acct-shared", user_id="user-a"),
        auth_mode="chatgpt_subscription", account_uuid="acct-shared",
        credential_already_encoded=True, codex_user_id="user-a")

    bundle_profiles = [{
        "name": "User B", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-b"),
        "codex_user_id": "user-b",
    }]
    parsed = ei.ParsedBundle(profiles=bundle_profiles, settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="keep_existing")

    assert result["profiles_added"] == 1
    assert result["profiles_skipped"] == 0
    assert result["profiles_overwrite_blocked"] == 0
    assert len(load_pool().profiles) == 2


def test_apply_import_codex_item_old_shape_with_no_codex_user_id_key_resolves_from_credential(env):
    """A bundle exported before codex_user_id existed has no such key on the
    item at all — not even null. Its shipped credential's id_token still
    proves who it is, so this must update in place (not be blocked) and
    backfill codex_user_id onto the existing legacy Profile, exactly like
    upsert_codex_profile()'s own same-user-relogin backfill path."""
    legacy = profile_repo.create_profile(
        name="Legacy", kind="codex", credential=_codex_encoded("acct-1", user_id="user-a", access_token="tok-legacy"),
        auth_mode="chatgpt_subscription", account_uuid="acct-1", credential_already_encoded=True)
    assert legacy.codex_user_id is None

    old_shape_item = {
        "name": "Imported version", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-1", "credential": _codex_encoded("acct-1", user_id="user-a", access_token="tok-new"),
        # deliberately no "codex_user_id" key at all
    }
    assert "codex_user_id" not in old_shape_item
    parsed = ei.ParsedBundle(profiles=[old_shape_item], settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_updated"] == 1
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].id == legacy.id
    assert pool.profiles[0].codex_user_id == "user-a"  # backfilled
    assert env.get_token(legacy.id) == old_shape_item["credential"]


def test_apply_import_codex_item_bundle_field_disagrees_with_credential_credential_wins(env):
    """The bundle's claimed codex_user_id must never be trusted over what
    its own shipped credential actually resolves to. Here the field
    (falsely) claims user-a, matching the existing Profile, but the
    shipped credential is actually user-b's — a spoofed field must never
    get treated as "same person, overwrite A". Since the credential wins,
    this item is user-b: a genuinely distinct, fully-resolved person (F1),
    so it is ADDED as its own Profile rather than merged into or blocking
    A. The field's spoofed claim is proven powerless by A staying
    completely untouched and the new Profile carrying user-b's real
    identity, not the field's claimed user-a."""
    existing = profile_repo.create_profile(
        name="User A", kind="codex", credential=_codex_encoded("acct-shared", user_id="user-a", access_token="tok-a"),
        auth_mode="chatgpt_subscription", account_uuid="acct-shared",
        credential_already_encoded=True, codex_user_id="user-a")
    original_stored = env.get_token(existing.id)

    spoofed_item = {
        "name": "Spoofed as User A", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": _codex_encoded("acct-shared", user_id="user-b", access_token="tok-b"),
        "codex_user_id": "user-a",  # falsely claims user-a; the shipped credential says user-b
    }
    parsed = ei.ParsedBundle(profiles=[spoofed_item], settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_added"] == 1
    assert result["profiles_updated"] == 0
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 2
    seat_a = next(p for p in pool.profiles if p.id == existing.id)
    new_seat = next(p for p in pool.profiles if p.id != existing.id)
    assert seat_a.name == "User A"                        # untouched
    assert env.get_token(existing.id) == original_stored  # byte-for-byte unchanged
    assert new_seat.codex_user_id == "user-b"              # the credential's real identity, not the spoofed field
    assert env.get_token(new_seat.id) == spoofed_item["credential"]


def test_apply_import_codex_item_credential_confirms_match_despite_wrong_field(env):
    """The reverse direction of the same rule: the bundle's codex_user_id
    field wrongly names a stranger who doesn't exist here, but the item's
    own shipped credential actually resolves to the SAME user as an
    existing Profile. If the field were trusted, this would look like no
    match (or a distinct new person); since the credential wins, it
    correctly UPDATES the existing Profile in place instead."""
    existing = profile_repo.create_profile(
        name="User A", kind="codex", credential=_codex_encoded("acct-shared", user_id="user-a", access_token="tok-old"),
        auth_mode="chatgpt_subscription", account_uuid="acct-shared",
        credential_already_encoded=True, codex_user_id="user-a")

    new_cred = _codex_encoded("acct-shared", user_id="user-a", access_token="tok-new")
    item = {
        "name": "Imported version", "kind": "codex", "base_url": None, "auth_mode": "chatgpt_subscription",
        "priority": 1, "switch_threshold": 98.0, "enabled": True, "automatic": True,
        "default_model": None, "monthly_budget_cap": None, "tag_color": None,
        "account_uuid": "acct-shared", "credential": new_cred,
        "codex_user_id": "user-does-not-exist",  # wrong on purpose; the credential is the truth
    }
    parsed = ei.ParsedBundle(profiles=[item], settings=None, activity=None)
    result = ei.apply_import(parsed, import_profiles=True, import_settings=False, conflict_strategy="use_imported")

    assert result["profiles_updated"] == 1
    assert result["profiles_added"] == 0
    assert result["profiles_overwrite_blocked"] == 0
    pool = load_pool()
    assert len(pool.profiles) == 1
    assert pool.profiles[0].id == existing.id
    assert pool.profiles[0].codex_user_id == "user-a"   # the credential's real identity, not the bogus field
    assert env.get_token(existing.id) == new_cred


def test_importing_settings_does_not_reset_fields_the_bundle_never_carried(env):
    """A bundle exported by a version predating a field simply has no key for
    it. Rebuilding Settings from scratch turned that into "reset it to the
    default", silently changing preferences of whoever imported."""
    from claude_unlimited.config import update_settings

    update_settings(language="ro", notifications_enabled=False)
    parsed = ei.ParsedBundle(
        profiles=[], settings={"update_mode": "manual"}, activity=None)

    ei.apply_import(parsed, import_profiles=False, import_settings=True)

    settings = load_pool().settings
    assert settings.update_mode == "manual"     # what the bundle asked for
    assert settings.language == "ro"            # untouched, not reset to "en"
    assert settings.notifications_enabled is False
