"""Guardrails for per-window reset times on profile cards (fix/card-7d-reset).

Before this fix, the card and Detail modal only ever showed the 5h window's
reset time (a single footer/field-sub line built from
`p.usage_5h_resets_at`) even though the daemon has always sent the 7d
window's reset time too (`p.usage_7d_resets_at`, see daemon.py
_profile_to_public_dict). These assert on the static app.js source itself —
same idiom as test_dashboard_launcher_ui.py — and deliberately do not start
the daemon.
"""
import re

import claude_unlimited.daemon as daemon
import claude_unlimited.i18n as i18n

_APP_JS = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _function_body(js, name):
    """Slice out one top-level `function <name>(...) { ... }` body.

    Same approach as the other dashboard tests: find the function's
    signature and take everything up to the next top-level `\nfunction `,
    rather than writing a JS parser.
    """
    marker = f"function {name}("
    start = js.index(marker)
    next_fn = js.index("\nfunction ", start + len(marker))
    return js[start:next_fn]


def test_render_usage_bar_declares_resets_at_param_and_emits_bar_reset():
    body = _function_body(_APP_JS, "renderUsageBar")
    assert re.search(r"function renderUsageBar\([^)]*resetsAt[^)]*\)", body), (
        "renderUsageBar must declare a resetsAt parameter"
    )
    assert "bar-reset" in body, "renderUsageBar must be able to emit a .bar-reset span"


def test_render_profile_card_passes_7d_resets_at_into_a_bar():
    body = _function_body(_APP_JS, "renderProfileCard")
    assert "usage_7d_resets_at" in body, (
        "renderProfileCard must thread p.usage_7d_resets_at into the 7d bar, "
        "not drop it on the floor"
    )


def test_render_profile_card_no_longer_uses_the_old_footer_only_reset():
    body = _function_body(_APP_JS, "renderProfileCard")
    assert "formatFutureRelative(p.usage_5h_resets_at)" not in body, (
        "the old single footer-line reset (5h only) must be gone now that "
        "each bar carries its own reset time"
    )


def test_detail_modal_pd_bars_threads_both_reset_fields():
    # The Detail modal's pd_bars block lives inside the larger populate
    # function rather than its own top-level function, so anchor on the
    # element id instead of a function name.
    idx = _APP_JS.index("document.getElementById('pd_bars').innerHTML")
    window = _APP_JS[max(0, idx - 800):idx + 800]
    assert "usage_5h_resets_at" in window
    assert "usage_7d_resets_at" in window


def test_format_reset_label_and_tooltip_helpers_exist():
    assert "function formatResetLabel(isoString)" in _APP_JS
    assert "function formatResetTooltip(isoString)" in _APP_JS


def test_resets_on_key_exists_in_every_locale():
    # The raw file, not load_locale(): that merges English in underneath, so
    # a locale missing the key would still pass through it.
    for code in i18n.list_locales():
        strings = i18n._read_locale_file(code)
        assert "profile.resets_on" in strings, f"{code} is missing profile.resets_on"


# ---- honest "healthy" display (T6) ----
#
# The daemon deliberately drops quota state (not usage numbers) on restart
# (runtime_state.py), so right after a restart a Profile can read
# status_word "healthy" while its restored usage_5h/7d percent already sits
# at or past where it would have rotated away (see the CLI's mirror-image
# fix in cli.py _eligible_usage_suffix / tests in
# test_cli_code_profile_picker.py). These assert the Dashboard card and
# Detail modal stop asserting "healthy" under the same contradiction,
# reusing the existing ALMOST_EXHAUSTED_BAND constant rather than a new
# literal — same static-source-scan idiom as the rest of this file.

def test_healthy_usage_override_function_reuses_the_existing_band_constant():
    body = _function_body(_APP_JS, "healthyUsageOverride")
    assert "p.status_word !== 'healthy'" in body
    assert "p.switch_threshold" in body
    assert "ALMOST_EXHAUSTED_BAND" in body, (
        "must reuse the existing ALMOST_EXHAUSTED_BAND constant, not a new "
        "hardcoded band width"
    )
    assert "usage_5h_percent" in body and "usage_7d_percent" in body
    # An entry without a usable switch_threshold must fall back to the
    # daemon's served default rather than returning null — returning null
    # fails OPEN, leaving the word "healthy" on a Profile whose numbers say
    # otherwise, and puts the Dashboard at odds with the CLI picker.
    assert "default_switch_threshold" in body, (
        "must fall back to the daemon's served default_switch_threshold when "
        "the profile entry carries no numeric switch_threshold"
    )


def test_render_profile_card_status_pill_uses_the_healthy_override():
    body = _function_body(_APP_JS, "renderProfileCard")
    assert "healthyUsageOverride(p)" in body
    assert "usageOverride ? usageOverride.text : statusLabel(p.status_word)" in body, (
        "the status pill must render the override's text instead of the "
        "bare 'healthy' word when the override is present"
    )


def test_profile_detail_modal_status_tag_shows_the_healthy_override_too():
    idx = _APP_JS.index("document.getElementById('pd_status_tag')")
    window = _APP_JS[idx:idx + 400]
    assert "healthyUsageOverride(p)" in window
    assert "pdUsageOverride" in window
    assert "p.status_word !== 'healthy' || pdUsageOverride" in window, (
        "the Detail modal must also show the tag when a healthy profile's "
        "usage contradicts the word, not only on a non-healthy state"
    )
