"""POST /api/settings/port's user-facing failures must be localizable.

CONTRIBUTING: "Every user-facing string lives in claude_unlimited/locales/
*.json". The endpoint's `message` field stays English on purpose — it also
serves non-dashboard API callers, and curl output is not a localized surface
— so the Dashboard translates the stable error CODE instead. These tests pin
that contract from both ends: every code the endpoint can return has a key in
every locale file, and app.js maps exactly those codes.
"""
import json
import re

import claude_unlimited.daemon as daemon
import claude_unlimited.i18n as i18n

# The error codes POST /api/settings/port can put in its JSON body.
PORT_ERROR_CODES = ("invalid_port", "port_in_use")

_APP_JS = (daemon._STATIC_DIR / "app.js").read_text(encoding="utf-8")


def _app_js_code_map() -> dict:
    """The `code -> locale key` map app.js applies in applyPortSetting()'s
    catch block, read out of the source rather than duplicated here, so this
    fails if the map is renamed or emptied."""
    block = re.search(
        r"const PORT_ERROR_LOCALE_KEYS = \{(.*?)\};", _APP_JS, re.DOTALL)
    assert block, "PORT_ERROR_LOCALE_KEYS is gone from app.js"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", block.group(1)))


def test_app_js_maps_every_port_error_code_the_endpoint_can_return():
    mapping = _app_js_code_map()
    assert set(mapping) == set(PORT_ERROR_CODES)


def test_every_mapped_locale_key_exists_in_every_locale_file():
    # _read_locale_file(), not load_locale(): the latter merges English in for
    # anything missing, which would pass on a file that never got the key.
    keys = set(_app_js_code_map().values())
    for code in i18n.list_locales():
        raw = set(i18n._read_locale_file(code).keys())
        missing = keys - raw
        assert not missing, f"{code}.json is missing port error keys: {missing}"


def test_translations_are_not_english_copies():
    en = i18n._read_locale_file("en")
    keys = sorted(_app_js_code_map().values())
    for code in i18n.list_locales():
        if code == "en":
            continue
        raw = i18n._read_locale_file(code)
        for key in keys:
            assert raw[key] != en[key], f"{code}.json:{key} is still the English string"


def test_unknown_error_code_still_falls_back_to_the_server_message():
    # Forward compatibility: a newer daemon inventing a code this page has
    # never heard of must still show the server's prose, not a blank box.
    catch_block = _APP_JS.split("async function applyPortSetting(")[1].split(
        "\n}\n")[0]
    assert "localized && localized !== localeKey ? localized : e.message" in catch_block


def test_endpoint_still_populates_message_for_non_dashboard_callers():
    src = (daemon.__file__ and open(daemon.__file__, encoding="utf-8").read())
    endpoint = src.split('if path == "/api/settings/port":')[1].split(
        "def _do_restart()")[0]
    for code in PORT_ERROR_CODES:
        chunk = endpoint.split(f'"error": "{code}"')
        assert len(chunk) > 1, f"{code} no longer returned by the endpoint"
        for after in chunk[1:]:
            assert '"message"' in after.split("return")[0], (
                f"{code} response lost its message field")


def test_en_locale_is_still_valid_json_after_the_additions():
    path = daemon._STATIC_DIR.parent / "locales" / "en.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in _app_js_code_map().values():
        assert data[key].strip()
