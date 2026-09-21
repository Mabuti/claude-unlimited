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
