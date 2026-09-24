"""One test per behavior this FORK deliberately does differently from
upstream DevDock-AI.

This file exists because the v1.3.1 upstream merge (f369c9a) introduced FOUR
defects, every one of them "a conflict resolved by taking upstream's side
entire where the fork had deliberately diverged." The existing suite is
overwhelmingly upstream's and caught only two of the four; the other two were
caught by a human reading diffs. Invariants 11-14 below are those exact four
defects. Every test here is meant to fail loudly the next time an upstream
merge silently drops one of these — read the docstring on a failing test
before "fixing" it by touching this file; the fix almost always belongs in
the source the test is guarding.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import claude_unlimited.cli as cli
import claude_unlimited.config as config
import claude_unlimited.daemon as daemon
import claude_unlimited.export_import as ei
import claude_unlimited.gateway as gateway_module
import claude_unlimited.profiles as profile_repo
import claude_unlimited.router as router
from claude_unlimited.config import Pool, Profile, Settings, load_pool, save_pool
from claude_unlimited.gateway import GatewayResult

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1. daemon.py: the legacy-oauth-organization backfill must never be reachable
#    from the 60-second _oauth_refresh_loop — only the startup one-shot.
# ---------------------------------------------------------------------------

class _StopLoop(Exception):
    """Escapes _oauth_refresh_loop's `while True:` after N sleeps."""


def test_backfill_not_reachable_from_oauth_refresh_loop(monkeypatch):
    """WHY: the backfill used to be piggybacked on this same 60s timer. A
    Profile the profile endpoint will never identify (a `claude setup-token`
    credential gets a permanent 403) never gets its org_uuid set, so every
    tick re-ran the backfill against it forever — about 1440 requests/day to
    Anthropic per such Profile. That is exactly the "no background polling of
    a provider's API" AGENTS.md forbids. If this test goes red, someone
    reintroduced a call to _backfill_legacy_oauth_organizations (or its
    async wrapper) inside _oauth_refresh_loop's body.

    Tested BEHAVIORALLY: run one real iteration of the loop (time.sleep is
    patched to let exactly one iteration happen, then break out) and assert
    the backfill function was never called — not by reading the source text.
    """
    calls = {"n": 0}

    def fake_backfill():
        calls["n"] += 1

    def fake_sleep(seconds):
        calls.setdefault("sleeps", 0)
        calls["sleeps"] += 1
        if calls["sleeps"] >= 2:
            raise _StopLoop()

    monkeypatch.setattr(daemon, "_backfill_legacy_oauth_organizations", fake_backfill)
    monkeypatch.setattr(daemon.time, "sleep", fake_sleep)
    # runtime_snapshot() runs inside a try/except in the loop; give it a real,
    # harmless Gateway so this exercises the actual loop body, not a mock.
    monkeypatch.setattr(daemon, "_gateway", gateway_module.Gateway())

    with pytest.raises(_StopLoop):
        daemon._oauth_refresh_loop()

    assert calls["n"] == 0, (
        "the legacy-oauth backfill was called from _oauth_refresh_loop — it "
        "must run ONLY from _backfill_legacy_oauth_organizations_async at "
        "startup, or it silently polls Anthropic every 60s forever for any "
        "Profile the profile endpoint permanently refuses"
    )


# ---------------------------------------------------------------------------
# 2. config.py: MIN_PORT/MAX_PORT and port_in_range() agree at the boundaries.
# ---------------------------------------------------------------------------

def test_port_range_constants_and_boundaries():
    """WHY: MIN_PORT/MAX_PORT are the one definition of a usable listen port
    for --port, CLAUDE_UNLIMITED_PORT, settings.port and POST
    /api/settings/port alike (config.py's own comment). If either constant
    drifts, or port_in_range() stops agreeing with them at the edges, some of
    those paths accept a port the others reject."""
    assert config.MIN_PORT == 1024
    assert config.MAX_PORT == 65535
    assert config.port_in_range(1023) is False
    assert config.port_in_range(1024) is True
    assert config.port_in_range(65535) is True
    assert config.port_in_range(65536) is False


# ---------------------------------------------------------------------------
# 3. config.py resolve_port(): three separate range-checked fallback paths.
# ---------------------------------------------------------------------------

def test_resolve_port_explicit_out_of_range_raises(monkeypatch):
    """WHY: an explicit --port is the one input a human definitely typed on
    purpose, so it must fail loudly (ValueError -> SystemExit(2) at the CLI
    layer) rather than silently falling back to the default port."""
    with pytest.raises(ValueError):
        config.resolve_port(70000)
    with pytest.raises(ValueError):
        config.resolve_port(1023)


def test_resolve_port_env_out_of_range_falls_back_to_default(monkeypatch):
    """WHY: the environment is inherited from a service unit or shell
    profile a user does not watch for error messages, so an out-of-range
    CLAUDE_UNLIMITED_PORT must fall back to the default rather than refuse to
    start something that was working yesterday."""
    monkeypatch.setenv("CLAUDE_UNLIMITED_PORT", "70000")
    assert config.resolve_port(None) == config._default_port()


def test_resolve_port_settings_out_of_range_falls_back_to_default(monkeypatch):
    """WHY: config.json is a plain file a user is invited to hand-edit
    (run_foreground's own bind-failure message says so). A hand-edited
    out-of-range settings.port must fall back to the default, the same
    treatment as a bad env var, rather than crash the daemon on startup."""
    monkeypatch.delenv("CLAUDE_UNLIMITED_PORT", raising=False)
    config.ensure_app_dir()
    config.CONFIG_FILE.write_text(json.dumps({"profiles": [], "settings": {"port": 99999}}))
    assert config.resolve_port(None) == config._default_port()


# ---------------------------------------------------------------------------
# 4. cli.py: _resolve_port_or_exit() turns a bad port into SystemExit(2), not
#    a stack trace.
# ---------------------------------------------------------------------------

def test_resolve_port_or_exit_is_a_clean_exit_2(capsys):
    """WHY: main() calls resolve_port() inside a dispatch expression, so an
    uncaught ValueError would surface to the user as a Python stack trace.
    argparse itself exits 2 with one line for a bad flag; a bad flag VALUE
    (--port 99999) must look the same, not like a crash."""
    with pytest.raises(SystemExit) as exc_info:
        cli._resolve_port_or_exit(99999)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.strip() != ""
    assert "99999" in captured.err or "port" in captured.err.lower()


# ---------------------------------------------------------------------------
# 5. router.py: the documented `reason` enumeration is complete.
# ---------------------------------------------------------------------------

def _documented_routing_reasons() -> set:
    """Every quoted word inside RoutingDecision's `reason` comment block."""
    source = inspect.getsource(router)
    match = re.search(
        r"class RoutingDecision:.*?Anything reading \.reason has to expect all of them\.",
        source, re.DOTALL,
    )
    assert match, "could not find the RoutingDecision reason-enumeration comment at all"
    return set(re.findall(r'"([a-zA-Z_]+)"', match.group(0)))


def _produced_routing_reasons() -> set:
    """Every reason string actually assigned to a RoutingDecision (or to a
    local `reason` variable later passed to one) in router.py and
    gateway.py — derived from source, not hand-copied, so this test keeps
    working as reasons are added or removed."""
    produced = set()
    for module in (router, gateway_module):
        source = inspect.getsource(module)
        produced |= set(re.findall(r'reason\s*=\s*"([a-zA-Z_]+)"', source))
    return produced


def test_routing_decision_reason_comment_is_complete():
    """WHY: RoutingDecision.reason is read by the Dashboard and by tests
    across the suite; the comment on the field is the only place all
    producers are enumerated in one spot. If a reason string produced by
    choose(), choose_for_new_branch() or gateway.py is missing from that
    comment, the next person reading .reason has no way to know it exists."""
    documented = _documented_routing_reasons()
    produced = _produced_routing_reasons()
    missing = produced - documented
    assert not missing, f"reason string(s) produced but not documented on RoutingDecision: {sorted(missing)}"


# ---------------------------------------------------------------------------
# 6. static/app.js: PORT_ERROR_LOCALE_KEYS maps both port errors, and both
#    locale keys it names exist in en.json.
# ---------------------------------------------------------------------------

def _load_locale(code: str) -> dict:
    return json.loads((REPO_ROOT / "claude_unlimited" / "locales" / f"{code}.json").read_text())


def _flatten_keys(d: dict, prefix: str = "") -> set:
    keys = set()
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten_keys(v, path)
        else:
            keys.add(path)
    return keys


def _port_error_locale_keys() -> dict:
    app_js = (REPO_ROOT / "claude_unlimited" / "static" / "app.js").read_text()
    match = re.search(r"const PORT_ERROR_LOCALE_KEYS\s*=\s*\{(.*?)\};", app_js, re.DOTALL)
    assert match, "PORT_ERROR_LOCALE_KEYS not found in static/app.js"
    body = match.group(1)
    return dict(re.findall(r"(\w+)\s*:\s*'([\w.]+)'", body))


def test_port_error_locale_keys_map_both_errors_and_resolve():
    """WHY: a --port collision surfaces to the Dashboard as either a 400
    (invalid_port) or a 409 (port_in_use). PORT_ERROR_LOCALE_KEYS is the only
    place that maps those two error codes to a translatable message; losing
    either entry, or a dangling key it names, means one of the two failure
    modes shows a raw error code to the user instead of a translated
    sentence."""
    mapping = _port_error_locale_keys()
    assert set(mapping.keys()) == {"invalid_port", "port_in_use"}
    en_keys = _flatten_keys(_load_locale("en"))
    for js_key, locale_key in mapping.items():
        assert locale_key in en_keys, f"PORT_ERROR_LOCALE_KEYS[{js_key!r}] -> {locale_key!r} missing from en.json"


# ---------------------------------------------------------------------------
# 7. locales: en/es/de/ro have EXACTLY equal key SETS.
# ---------------------------------------------------------------------------

def test_all_locales_have_identical_key_sets():
    """WHY: a count match with different keys is precisely the failure this
    test exists to catch — two locales can both have 705 keys while
    disagreeing about which 705, which is invisible to a len() check and
    renders as a silently-missing string at runtime. Compared as SETS,
    never counts."""
    codes = ("en", "es", "de", "ro")
    key_sets = {code: _flatten_keys(_load_locale(code)) for code in codes}
    baseline_code, baseline = "en", key_sets["en"]
    for code in codes:
        if code == baseline_code:
            continue
        missing = baseline - key_sets[code]
        extra = key_sets[code] - baseline
        assert not missing and not extra, (
            f"{code}.json key set differs from en.json: missing={sorted(missing)} extra={sorted(extra)}"
        )

    mapping = _port_error_locale_keys()
    for code in codes:
        for locale_key in mapping.values():
            assert locale_key in key_sets[code], f"{locale_key!r} missing from {code}.json"


# ---------------------------------------------------------------------------
# 8. export_import.py: _MACHINE_LOCAL_SETTINGS == {"port", "launchers"} and it
#    is actually enforced end to end, both directions. This is F08.
# ---------------------------------------------------------------------------

def test_machine_local_settings_withheld_from_export_and_dropped_on_import():
    """WHY (security fix F08): without this, an exported settings bundle
    carries this machine's launcher command — e.g.
    `claude --dangerously-skip-permissions` — to whoever imports it, and its
    listen port, silently relocating the importer's dashboard. Proved END TO
    END: build a bundle from a pool whose settings carry a non-default port
    and a launcher command, assert neither key appears in the exported
    bundle; then import a bundle that DOES contain them (as a hand-written or
    pre-fix bundle would) and assert they are silently dropped rather than
    applied."""
    assert ei._MACHINE_LOCAL_SETTINGS == frozenset({"port", "launchers"})

    dangerous_launcher = "claude --dangerously-skip-permissions"
    save_pool(Pool(profiles=[], settings=Settings(
        port=59999, launchers={"claude": dangerous_launcher},
    )))

    # --- export direction ---
    bundle = ei.build_export_bundle(include_profiles=False, include_settings=True, include_activity=False)
    envelope = json.loads(bundle)
    exported_settings = envelope["data"]["settings"]
    assert "port" not in exported_settings, "port leaked into an exported settings bundle"
    assert "launchers" not in exported_settings, "launchers leaked into an exported settings bundle (F08)"

    # --- import direction: a bundle that DOES carry them (e.g. hand-written,
    # or written by a build that predates the fix) must still have them
    # dropped rather than applied. Reset the local pool to a SAFE state
    # first, so a later assertion that it's still safe actually proves the
    # import dropped the incoming values rather than just observing the
    # setup pool's own leftover (dangerous) state from the export step above.
    safe_port = config._default_port()
    save_pool(Pool(profiles=[], settings=Settings(port=safe_port, launchers={})))
    parsed = ei.ParsedBundle(
        profiles=[],
        settings={"port": 4444, "launchers": {"claude": dangerous_launcher}, "language": "en"},
        activity=None,
    )
    ei.apply_import(parsed, import_profiles=False, import_settings=True)
    pool_after = load_pool()
    assert pool_after.settings.port == safe_port, "an imported bundle's port was applied (F08)"
    assert pool_after.settings.launchers.get("claude") != dangerous_launcher, (
        "an imported bundle's launcher command was applied (F08) — this is the "
        "exact vector that hands `--dangerously-skip-permissions` to an importer"
    )


# ---------------------------------------------------------------------------
# 9. profiles.py: Anthropic identity is the (account, organization) PAIR.
# ---------------------------------------------------------------------------

def test_anthropic_identity_is_account_org_pair_not_account_alone():
    """WHY: one Anthropic account_uuid can surface under more than one
    organization.uuid — a personal Max plan and a Team seat on the same
    email return the SAME account_uuid but a DIFFERENT org_uuid (measured
    2026-09-22). Deduping on account_uuid alone collapses two separately
    billed subscriptions into one Profile, silently overwriting one with the
    other. Two Profiles sharing account_uuid under different org_uuid must
    be treated as two distinct identities."""
    shared_account = "uuid-shared-account"
    profile_org_a = Profile(id="p-a", name="Personal Max", kind="oauth",
                             account_uuid=shared_account, org_uuid="org-a")
    profile_org_b = Profile(id="p-b", name="Team Seat", kind="oauth",
                             account_uuid=shared_account, org_uuid="org-b")
    candidates = [profile_org_a, profile_org_b]

    match_a, backfill_a = profile_repo.find_by_account_and_org(shared_account, "org-a", profiles=candidates)
    match_b, backfill_b = profile_repo.find_by_account_and_org(shared_account, "org-b", profiles=candidates)

    assert match_a is not None and match_a.id == "p-a", "org-a lookup must resolve to the org-a Profile, not collide"
    assert match_b is not None and match_b.id == "p-b", "org-b lookup must resolve to the org-b Profile, not collide"
    assert match_a.id != match_b.id, "two different (account, org) pairs collapsed onto the same Profile"
    assert backfill_a is False and backfill_b is False, "an exact pair match must never ask for org backfill"


# ---------------------------------------------------------------------------
# 10. docs/adr: no two ADR files share a leading number.
# ---------------------------------------------------------------------------

def test_no_duplicate_adr_numbers():
    """WHY: this exact merge produced a number collision between two ADRs
    written independently on each side of the fork/upstream split. Two ADRs
    sharing a number means one silently shadows the other in any listing or
    reference by number."""
    adr_files = sorted((REPO_ROOT / "docs" / "adr").glob("*.md"))
    assert adr_files, "no ADR files found — path may have moved"
    numbers = []
    for f in adr_files:
        match = re.match(r"^(\d{4})-", f.name)
        assert match, f"ADR filename does not start with a 4-digit number: {f.name}"
        numbers.append(match.group(1))
    duplicates = {n for n in numbers if numbers.count(n) > 1}
    assert not duplicates, f"duplicate ADR number(s): {sorted(duplicates)}"


# ---------------------------------------------------------------------------
# 11. cli.py _MODEL_TIER_IDS: OPUS/SONNET carry [1m], FABLE/HAIKU do not.
# ---------------------------------------------------------------------------

def test_model_tier_ids_1m_suffix_only_on_opus_and_sonnet():
    """WHY: a bare model id makes Claude Code send no `context-1m` beta
    header, so the session caps at 200k context instead of 1M. FABLE and
    HAIKU are deliberately bare because the installed binary (2.1.281) has
    no `[1m]` variant for either tier — `claude-fable-5[1m]` exists only
    inside a legacy migration path (`tengu_legacy_opus_migration`) and is a
    different id family (`claude-fable-5`, not `claude-fable-5-1`) besides.
    Adding `[1m]` to FABLE or HAIKU, or dropping it from OPUS/SONNET, either
    silently caps a tier at 200k or advertises a context size Claude Code
    will refuse to honor."""
    ids = cli._MODEL_TIER_IDS
    assert ids["OPUS"].endswith("[1m]"), ids["OPUS"]
    assert ids["SONNET"].endswith("[1m]"), ids["SONNET"]
    assert not ids["FABLE"].endswith("[1m]"), ids["FABLE"]
    assert not ids["HAIKU"].endswith("[1m]"), ids["HAIKU"]


# ---------------------------------------------------------------------------
# 12. cli.py offline-fallback labels: "1M context · " prefix on OPUS/SONNET
#     only, in both _CODEX_MODEL_LABELS and _MIXED_MODEL_LABELS.
# ---------------------------------------------------------------------------

def test_offline_fallback_labels_1m_prefix_only_on_opus_and_sonnet():
    """WHY: the fork had 7 such "1M context · " prefixes across the two
    offline-fallback label dicts (used only when the daemon's live parity
    fetch fails) and the upstream merge dropped all 7 at once, because the
    whole literal dict was taken from upstream's side of the conflict. The
    prefix must survive on exactly the two 1M-context tiers, in both label
    dicts."""
    for label_dict in (cli._CODEX_MODEL_LABELS, cli._MIXED_MODEL_LABELS):
        for tier in ("OPUS", "SONNET"):
            _name, description = label_dict[tier]
            assert description.startswith("1M context · "), (label_dict, tier, description)
        for tier in ("FABLE", "HAIKU"):
            _name, description = label_dict[tier]
            assert not description.startswith("1M context · "), (label_dict, tier, description)


# ---------------------------------------------------------------------------
# 13. daemon.py _proxy_error_payload: 429 -> rate_limit_error, 503 stays
#     overloaded_error, request_exceeds_every_window stays invalid_request_error.
# ---------------------------------------------------------------------------

def test_proxy_error_payload_429_is_rate_limit_not_overloaded():
    """WHY: an empty pool is THIS daemon out of accounts, not Anthropic being
    overloaded. A client that inspects `type` and sees overloaded_error backs
    off against a condition only a local account change (re-auth, add a
    Profile) can ever clear — it will retry forever. 503 (a genuinely
    unusable pool, e.g. no_usable_profile) and the capacity guard's
    request_exceeds_every_window must keep their own distinct mappings."""
    quota_blocked = GatewayResult(status=429, headers={}, body_chunks=None,
                                   profile_id=None, error="no_eligible_profile")
    _status, payload = daemon._proxy_error_payload(quota_blocked)
    assert payload["error"]["type"] == "rate_limit_error"

    genuinely_unusable = GatewayResult(status=503, headers={}, body_chunks=None,
                                        profile_id=None, error="no_usable_profile")
    _status2, payload2 = daemon._proxy_error_payload(genuinely_unusable)
    assert payload2["error"]["type"] == "overloaded_error"

    too_long = GatewayResult(status=400, headers={}, body_chunks=None,
                              profile_id=None, error="request_exceeds_every_window",
                              error_detail="prompt too long")
    _status3, payload3 = daemon._proxy_error_payload(too_long)
    assert payload3["error"]["type"] == "invalid_request_error"


# ---------------------------------------------------------------------------
# 14. daemon.py _FORCED_PROFILE_ERROR_MESSAGES["no_usable_profile"] exists and
#     says the condition will not fix itself by waiting.
# ---------------------------------------------------------------------------

def test_no_usable_profile_message_says_it_will_not_fix_itself():
    """WHY: losing this message told a user whose accounts all need re-auth
    (or are all disabled) to sit and wait for a rotation that would never
    happen — every account being unusable is not a wait-and-retry condition,
    unlike an ordinary rotation/quota wait. The honest, specific message is
    what tells the user to go fix the account instead of watching a spinner."""
    assert "no_usable_profile" in daemon._FORCED_PROFILE_ERROR_MESSAGES
    message = daemon._FORCED_PROFILE_ERROR_MESSAGES["no_usable_profile"]
    assert "will not fix itself by waiting" in message

def test_api_base_url_accepts_local_http_but_never_public_http():
    """Upstream v1.3.1 (80db304 "feat(api): local model servers") replaced this
    fork's https-only rule for Profile base_url with
    net_scope.validate(allow_local_http=kind != "codex"), so a local model
    server can be reached over plain http.

    That relaxation was reviewed and ACCEPTED deliberately on 2026-09-24 — it
    was not inherited by accident. The fork's previous rule refused even
    loopback http, reasoning that base_url validates the UPSTREAM target and
    so is a different trust boundary from the daemon's own local listener.

    This test pins where the line now sits, so that a future merge which
    widens or narrows it fails here and the change becomes a decision again
    instead of a surprise. A key may cross localhost or a private LAN in the
    clear; it must never cross the public internet that way, and never for a
    Codex profile, whose bridge speaks https only.
    """
    from claude_unlimited import profiles

    for ok in ("https://api.anthropic.com",
               "https://example.com/v1",
               "http://localhost:11434",
               "http://127.0.0.1:8080",
               "http://192.168.1.10:11434",
               "http://10.0.0.5:11434"):
        profiles._validate_base_url(ok, "api")  # must not raise

    for blocked in ("http://api.example.com",
                    "http://8.8.8.8",
                    "http://example.com/v1"):
        try:
            profiles._validate_base_url(blocked, "api")
        except profiles.ValidationError:
            pass
        else:
            raise AssertionError(
                f"public plain-http base_url {blocked!r} was accepted — a Profile key "
                "would cross the internet in the clear")

    # A Codex profile is https-only whatever the host.
    try:
        profiles._validate_base_url("http://localhost:11434", "codex")
    except profiles.ValidationError:
        pass
    else:
        raise AssertionError("codex profile accepted plain http; the Codex bridge is https-only")

