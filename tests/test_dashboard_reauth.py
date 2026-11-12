"""Guardrails for on-demand OAuth re-authentication in the Dashboard."""

import json
import re

import claude_unlimited.daemon as daemon


_APP_JS = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
_LOCALE_DIR = daemon._STATIC_DIR.parent / "locales"


def _function_body(marker):
    start = _APP_JS.index(marker)
    next_function = re.search(r"\n(?:async )?function ", _APP_JS[start + len(marker):])
    if not next_function:
        return _APP_JS[start:]
    return _APP_JS[start:start + len(marker) + next_function.start()]


def test_menu_reauth_is_available_for_healthy_oauth_and_not_codex_profiles():
    can_reauth = _function_body("function profileCanReauth(")
    menu = _function_body("function openProfileKebabMenu(")

    # The predicate is the executable kind check: healthy OAuth profiles pass,
    # while codex and API-key profiles do not.
    assert "return p.kind === 'oauth';" in can_reauth
    assert "...(profileCanReauth(profile) ? [" in menu
    assert "label: t('profiles.menu_reauth')" in menu
    assert "action: 'reauth'" in menu
    assert "profileNeedsReauth(profile)" not in menu


def test_card_red_reauth_action_still_requires_needs_reauth():
    body = _function_body("function renderProfileCard(")

    assert "const needsReauth = profileNeedsReauth(p);" in body
    assert "const cardReauthAction = needsReauth ? reauthAction : '';" in body
    assert "${cardReauthAction}" in body


def test_launch_reauth_accepts_a_healthy_oauth_profile():
    body = _function_body("async function launchReauth(")

    assert "if (!profileCanReauth(profile || {})) return;" in body
    assert "profileNeedsReauth" not in body
    assert "/api/profiles/${profileId}/reauth/launch" in body
    assert "showReauthFallbackModal(profileId, state.reason)" in body


def test_update_reauth_card_uses_the_any_oauth_gate():
    body = _function_body("function updateReauthCard(")

    assert "profileCanReauth(profile)" in body
    assert "return false;" in body
    assert "return true;" in body


def test_menu_reauth_fallback_keeps_the_command_copyable():
    body = _function_body("function showReauthFallbackModal(")

    assert "const command = reauthCommand(profileId);" in body
    assert 'data-action="copy-reauth-fallback"' in body
    assert "navigator.clipboard.writeText(command)" in body


def test_menu_reauth_key_exists_in_every_locale():
    for code in ("en", "de", "es", "ro"):
        strings = json.loads((_LOCALE_DIR / f"{code}.json").read_text(encoding="utf-8"))
        assert strings["profiles.menu_reauth"]
