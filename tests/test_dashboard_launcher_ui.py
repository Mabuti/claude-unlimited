"""Guardrails for the Settings-view launcher-commands and port UI (W3).

These assert on the static assets themselves — the same idiom as
test_dashboard_view_routes.py — and deliberately do NOT start the daemon or
call any API endpoint. The `/api/settings` and `/api/settings/port`
endpoints this UI talks to are built by a parallel ticket; these tests must
pass whether or not that code exists yet.
"""
import json
import re

import claude_unlimited.daemon as daemon

_INDEX_HTML = (daemon._STATIC_DIR / "index.html").read_text(encoding="utf-8")
_APP_JS = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")
_EN_KEYS = set(
    json.loads((daemon._STATIC_DIR.parent / "locales" / "en.json").read_text(encoding="utf-8")).keys()
) - {"_meta"}


def _shell_env_block(html):
    # The code-row that renders the `export ANTHROPIC_BASE_URL=...` snippet,
    # identified the same way test_dashboard_view_routes.py isolates a
    # branch: split on a unique anchor and take a bounded window after it.
    marker = 'id="shellLine"'
    assert marker in html, "shellLine element not found in index.html"
    start = html.index(marker)
    return html[max(0, start - 400):start + 400]


def test_no_4317_literal_in_shell_env_snippet():
    block = _shell_env_block(_INDEX_HTML)
    assert "4317" not in block, (
        "the shell-env snippet must render the port from window.location.host, "
        "not a hardcoded literal"
    )


def test_shell_line_is_populated_from_window_location_host():
    assert "window.location.host" in _APP_JS
    # The actual export line construction must reference the live host, not
    # a bare string literal.
    assert 'shellLine.textContent = `export ANTHROPIC_BASE_URL="http://${window.location.host' in _APP_JS


def _data_i18n_keys(html):
    keys = set()
    for attr in ("data-i18n", "data-i18n-placeholder", "data-i18n-title"):
        keys |= set(re.findall(rf'{attr}="([^"]+)"', html))
    return keys


def _literal_t_call_keys(js):
    # Only literal t('...') / t("...") calls — a key that arrives as data
    # (e.g. a server-provided label_key looked up via t(kind.label_key)) is
    # intentionally not caught here, since it is data-driven rather than
    # baked into the client source.
    return set(re.findall(r"""\bt\((['"])([\w.]+)\1\)""", js))


def test_every_new_data_i18n_key_exists_in_en_json():
    html_keys = _data_i18n_keys(_INDEX_HTML)
    js_keys = {key for _, key in _literal_t_call_keys(_APP_JS)}
    all_keys = html_keys | js_keys
    launcher_or_port_keys = {k for k in all_keys if k.startswith("settings.launchers.") or k.startswith("settings.port.")}
    assert launcher_or_port_keys, "expected new settings.launchers.* / settings.port.* keys to be referenced"
    missing = launcher_or_port_keys - _EN_KEYS
    assert not missing, f"keys referenced in the UI but missing from en.json: {missing}"


def test_registry_label_keys_exist_in_en_json():
    # These are the label_key values the plan's LAUNCHER_KINDS registry uses
    # (config.py, built by a parallel ticket); the row is rendered via
    # t(kind.label_key), so it is data-driven and invisible to a static
    # regex scan of app.js. Assert them directly instead.
    for key in ("settings.launchers.claude", "settings.launchers.codex"):
        assert key in _EN_KEYS, f"{key} must exist so a registry-driven row can render its label"


def test_launcher_rows_container_is_empty_in_static_markup():
    # The rows must come from JS iterating launcher_kinds, never a
    # hand-written block per CLI baked into index.html.
    match = re.search(r'<div class="section-body" id="launcherRows">(.*?)</div>', _INDEX_HTML, re.S)
    assert match, "launcherRows container not found"
    assert match.group(1).strip() == "", "launcherRows must be empty in static markup — rows are JS-rendered"


def test_app_js_renders_launcher_rows_by_iterating_the_registry():
    assert "renderLauncherRows" in _APP_JS
    # Must iterate a launcher-kinds collection (server-provided), not a
    # fixed, hand-written list of CLI names.
    assert re.search(r"_lastLauncherKinds\.map\(", _APP_JS), (
        "launcher rows must be built by looping over launcher_kinds"
    )


def test_app_js_has_no_hand_written_per_kind_launcher_row():
    # No hardcoded per-CLI DOM ids for the launcher inputs — a future CLI
    # kind must need zero changes to index.html or app.js.
    for bad in ("claudeLauncherInput", "codexLauncherInput", "claudeCommandInput", "codexCommandInput"):
        assert bad not in _APP_JS
        assert bad not in _INDEX_HTML


def test_launcher_save_patches_full_map_to_settings():
    assert "launchers: next" in _APP_JS or re.search(r"launchers['\"]?\s*:\s*next", _APP_JS)
    assert "/api/settings'" in _APP_JS


def test_port_apply_posts_to_the_port_endpoint():
    assert "/api/settings/port" in _APP_JS
    assert re.search(r"method:\s*'POST'.{0,80}/api/settings/port|/api/settings/port['\"].{0,120}method:\s*'POST'", _APP_JS, re.S) or (
        "/api/settings/port" in _APP_JS and "'POST'" in _APP_JS
    )


def test_port_apply_navigates_after_a_delay_instead_of_polling_health():
    # A cross-origin health probe cannot work under this dashboard's CSP
    # (connect-src 'self' blocks fetch to the new port's origin regardless
    # of whether the new daemon is up — see app.js's PORT_MOVE_DELAY_MS
    # comment) so it must not be reintroduced, and the poll it replaced must
    # not linger as dead code.
    assert "pollHealthUntilUp" not in _APP_JS
    assert "window.location.href" in _APP_JS
    assert "PORT_MOVE_DELAY_MS" in _APP_JS
    assert re.search(r"setTimeout\(\s*\(\)\s*=>\s*\{\s*window\.location\.href", _APP_JS), (
        "the redirect must be a plain delayed top-level navigation, not a poll"
    )


def test_port_ui_warns_before_applying_that_it_restarts_the_daemon():
    assert "settings.port.restart_warning" in _APP_JS
    assert "settings.port.restart_warning" in _EN_KEYS
