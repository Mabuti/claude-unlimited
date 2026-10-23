import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "claude_unlimited" / "locales"
APP_JS = ROOT / "claude_unlimited" / "static" / "app.js"

BUDGET_KEYS = {
    "settings.context1m.budget_one": ("{budget}",),
    "settings.context1m.budget_on": ("{budget}", "{models}"),
    "settings.context1m.budget_list_sep": (),
}


def test_budget_line_keys_and_placeholders_exist_in_every_locale():
    for code in ("en", "de", "es", "ro"):
        strings = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for key, placeholders in BUDGET_KEYS.items():
            assert key in strings, f"{code}.json is missing {key}"
            for placeholder in placeholders:
                assert placeholder in strings[key], f"{code}.json:{key} lacks {placeholder}"


def test_budget_line_does_not_reintroduce_english_formatting_literals():
    source = APP_JS.read_text(encoding="utf-8")

    assert "smallest of these" not in source
    assert "} on ${" not in source
    assert "K on " not in source
