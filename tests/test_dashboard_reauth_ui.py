"""Static guardrails for the profile-card re-auth action."""

import json
import re

import claude_unlimited.daemon as daemon


_APP_JS = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
_INDEX_HTML = (daemon._STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _function_body(source, name):
    start = source.index(f"function {name}(")
    next_function = source.find("\nfunction ", start + 1)
    return source[start:] if next_function == -1 else source[start:next_function]


def test_auth_invalid_cards_have_bad_border_and_oauth_reauth_action():
    body = _function_body(_APP_JS, "renderProfileCard")
    assert "profileNeedsReauth(p)" in body
    assert "needs-reauth" in body
    assert "profileCanReauth(p) ? renderReauthAction(p)" in body
    assert "data-action=\"reauth\"" in _APP_JS


def test_reauth_action_posts_only_to_fixed_profile_endpoint_and_has_fallback():
    assert "/api/profiles/${profileId}/reauth/launch" in _APP_JS
    assert "method: 'POST'" in _APP_JS
    assert "mode: 'fallback'" in _APP_JS
    assert "data-action=\"copy-reauth\"" in _APP_JS
    assert "claude-unlimited reauth --profile ${profileId}" in _APP_JS
    assert "Terminal opened" not in _APP_JS  # rendered through the locale key


def test_reauth_action_keeps_button_command_and_copy_control_in_every_mode():
    body = _function_body(_APP_JS, "renderReauthAction")
    assert "state.mode === 'opened'" in body
    assert "state.mode === 'fallback'" in body
    assert body.count('data-action="reauth"') == 1
    assert body.count('data-action="copy-reauth"') == 1
    assert "${esc(command)}" in body
    assert "profile.reauth_command_caption" in body
    assert "p-reauth-command-caption" in body


def test_reauth_fallback_reason_is_escaped_and_rendered_under_command():
    body = _function_body(_APP_JS, "renderReauthAction")
    assert "state.reason" in body
    assert "esc(state.reason)" in body
    assert "p-reauth-reason" in body
    assert ".p-reauth-reason" in _INDEX_HTML


def test_profile_refresh_removes_stale_reauth_ui_state():
    body = _function_body(_APP_JS, "loadProfiles")
    assert "liveReauthIds" in body
    assert "_reauthUi.delete(id)" in body
    assert "setLiveHtml(el" in body


def test_reauth_strings_exist_in_every_locale():
    required = {
        "profile.reauth", "profile.reauth_opened", "profile.reauth_fallback",
        "profile.reauth_command_caption",
    }
    locale_dir = daemon._STATIC_DIR.parent / "locales"
    for path in sorted(locale_dir.glob("*.json")):
        strings = json.loads(path.read_text(encoding="utf-8"))
        assert required <= strings.keys(), f"{path.name} is missing re-auth strings"


def test_reauth_card_css_uses_bad_token_and_fallback_command_markup():
    assert ".p-card.needs-reauth" in _INDEX_HTML
    assert "border-color: var(--bad)" in _INDEX_HTML
    assert ".p-reauth-command" in _INDEX_HTML
    assert re.search(r'data-action="copy-reauth"', _APP_JS)
