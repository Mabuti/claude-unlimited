"""Guards the policy that the Dashboard Help view stays current with the CLI.

A prose rule ("update Help when you add a command") has already been missed
once. These tests make that mechanical: every subcommand argparse actually
registers must have a row in the Help view and vice versa, every Settings
section must be mentioned there (or be a named, justified exception), and
the Help view's inline fallback text must not drift from en.json.

Static analysis only, the same idiom as test_dashboard_view_routes.py and
test_dashboard_launcher_ui.py: read index.html/en.json as text/JSON and
introspect cli.py's argparse tree without invoking any command. No daemon
starts, no network, nothing is written outside pytest's own fixtures.
"""
from __future__ import annotations

import argparse
import json
import re
from html.parser import HTMLParser

import claude_unlimited.cli as cli
import claude_unlimited.daemon as daemon

_INDEX_HTML = (daemon._STATIC_DIR / "index.html").read_text(encoding="utf-8")
_EN = json.loads((daemon._STATIC_DIR.parent / "locales" / "en.json").read_text(encoding="utf-8"))

# Settings sections that legitimately have no mention in the Help view's
# text. Each needs a one-line reason. This list is itself a finding to
# review on every addition -- the goal is for it to stay short, not to grow
# quietly. Do not add an entry here just to make a failing test pass; add it
# only when the section genuinely needs no CLI-adjacent explanation.
_SETTINGS_SECTIONS_EXEMPT_FROM_HELP = {
    "settings.updates.title": "self-describing update checker/toggle, nothing to walk through",
    "settings.language.title": "a locale picker; not part of the CLI-driven workflow Help documents",
    "settings.parity.title": "a read-only diagnostic table, not a workflow step",
    "settings.process.title": "process/log controls with their own on-screen labels",
    "settings.port.title": "the port picker; the running port is surfaced elsewhere (e.g. the desktop-app URL) without naming this section",
    "settings.notifications.title": "notification toggles, self-explanatory in place",
    "settings.export_import.title": "has its own modal with its own instructions; not part of the CLI walkthrough",
    "settings.storage.title": "shows on-disk usage; nothing to instruct",
    "settings.danger.title": "a single destructive button with its own inline warning text",
}


def _registered_subcommands():
    """Return [(name, [aliases]), ...] for every sub.add_parser() call in
    cli.py's argparse tree, without running any command.

    main() builds the subparsers tree inline -- there is no standalone
    build-parser function to import -- so real introspection means letting
    that inline setup run. We monkeypatch add_parser to record every call,
    then invoke cli.main(argv=[]). With an explicit empty argv, args.cmd
    stays None, so main() falls straight through to `parser.print_help();
    return 0`: none of the dispatch branches that call the real command
    functions (start the daemon, touch the filesystem, etc.) ever fire.
    Verified by hand: rc == 0 and only print_help's own stdout is produced.
    """
    calls = []
    orig_add_parser = argparse._SubParsersAction.add_parser

    def recording_add_parser(self, name, **kwargs):
        calls.append((name, list(kwargs.get("aliases", []))))
        return orig_add_parser(self, name, **kwargs)

    argparse._SubParsersAction.add_parser = recording_add_parser
    try:
        rc = cli.main(argv=[])
    finally:
        argparse._SubParsersAction.add_parser = orig_add_parser
    assert rc == 0, "cli.main(argv=[]) must fall through to print_help(), not dispatch a command"
    return calls


def _find_matching_close(html, open_tag_start):
    """Index just past the </div> matching the <div ...> tag starting at
    open_tag_start, found by counting nested opens/closes. Robust to
    reformatting -- makes no assumption about indentation."""
    tag_end = html.index(">", open_tag_start) + 1
    depth = 1
    for m in re.finditer(r"<div\b|</div>", html[tag_end:]):
        depth += 1 if m.group(0) != "</div>" else -1
        if depth == 0:
            return tag_end + m.end()
    raise AssertionError("unbalanced <div> tags in index.html while scanning for a closing tag")


def _extract_view(html, view_name):
    marker = f'data-view-panel="{view_name}"'
    marker_pos = html.index(marker)
    open_tag_start = html.rfind("<div", 0, marker_pos)
    close_pos = _find_matching_close(html, open_tag_start)
    return html[open_tag_start:close_pos]


_HELP_VIEW_HTML = _extract_view(_INDEX_HTML, "help")

# The one documented, intentional duplicate: `desktop` gets a command row
# under the normal `cmd-desktop` id AND a second mention inside the "Using
# the Claude desktop app" walkthrough, under `cmd-desktop2`. Naming it here
# (rather than a loose id/name regex) means a genuine stray or typo'd id
# still gets caught.
_KNOWN_DUPLICATE_COMMAND_ROW_ID = "cmd-desktop2"
_KNOWN_DUPLICATE_COMMAND_ROW_NAME = "desktop"


def _documented_command_rows(help_html):
    """{command_name: element_id} for every `<code id="cmd-NAME">cu NAME</code>`
    row in the Help view, excluding the one known cmd-desktop2 duplicate."""
    rows = {}
    for element_id, cmd_name in re.findall(
        r'<code id="(cmd-[a-z0-9-]+)">cu ([a-z][a-z0-9-]*)</code>', help_html
    ):
        if element_id == _KNOWN_DUPLICATE_COMMAND_ROW_ID:
            assert cmd_name == _KNOWN_DUPLICATE_COMMAND_ROW_NAME, (
                f"{_KNOWN_DUPLICATE_COMMAND_ROW_ID} is the documented exception for "
                f"{_KNOWN_DUPLICATE_COMMAND_ROW_NAME!r}, but documents {cmd_name!r} instead"
            )
            continue
        expected_id = f"cmd-{cmd_name}"
        assert element_id == expected_id, (
            f"command row id {element_id!r} does not match its own command text 'cu {cmd_name}' "
            f"(expected id {expected_id!r}) -- fix the id, or if this is a deliberate second "
            f"mention, name it explicitly the way {_KNOWN_DUPLICATE_COMMAND_ROW_ID!r} is"
        )
        rows[cmd_name] = element_id
    return rows


class _I18nTextExtractor(HTMLParser):
    """For every element carrying data-i18n="help.*", collect its full
    rendered text (including any nested non-i18n markup's text, entities
    decoded) -- i.e. what el.textContent would be before app.js's
    `el.textContent = t(el.dataset.i18n)` (app.js:155) overwrites it.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._stack = []  # [[tag, key_or_None, [text_chunks]], ...]
        self.results = {}

    def handle_starttag(self, tag, attrs):
        key = dict(attrs).get("data-i18n")
        self._stack.append([tag, key, []])

    def handle_data(self, data):
        for frame in self._stack:
            frame[2].append(data)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                frame = self._stack.pop(i)
                del self._stack[i:]
                if frame[1] and frame[1].startswith("help."):
                    text = re.sub(r"\s+", " ", "".join(frame[2])).strip()
                    self.results[frame[1]] = text
                return


def _help_i18n_texts(help_html):
    parser = _I18nTextExtractor()
    parser.feed(help_html)
    return parser.results


def test_every_cli_subcommand_has_a_help_row():
    registered = {name for name, _aliases in _registered_subcommands()}
    documented = set(_documented_command_rows(_HELP_VIEW_HTML))
    undocumented = registered - documented
    assert not undocumented, (
        f"cli.py registers {sorted(undocumented)} but the Help view has no "
        f"`cu <name>` row for them -- add a command row under data-view-panel=\"help\""
    )


def test_every_help_command_row_is_a_real_subcommand():
    registered = {name for name, _aliases in _registered_subcommands()}
    documented = set(_documented_command_rows(_HELP_VIEW_HTML))
    stale = documented - registered
    assert not stale, (
        f"the Help view documents {sorted(stale)} but cli.py no longer registers "
        f"them as subcommands -- remove the stale row(s)"
    )


def test_every_settings_section_is_mentioned_in_help_or_exempted():
    sections = {}
    for tag_match in re.finditer(r"<div\b[^>]*>", _INDEX_HTML):
        tag = tag_match.group(0)
        if 'class="section-title"' not in tag:
            continue
        key_match = re.search(r'data-i18n="(settings\.[A-Za-z_]+\.title)"', tag)
        if key_match:
            sections[key_match.group(1)] = _EN[key_match.group(1)]
    assert sections, "found no settings.*.title section headers -- the extraction regex is broken"

    help_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _HELP_VIEW_HTML))
    unexempted_misses = [
        key for key, label in sections.items()
        if label not in help_text and key not in _SETTINGS_SECTIONS_EXEMPT_FROM_HELP
    ]
    assert not unexempted_misses, (
        f"these Settings sections are not mentioned anywhere in the Help view and are not in "
        f"_SETTINGS_SECTIONS_EXEMPT_FROM_HELP: {sorted(unexempted_misses)} -- document them in "
        f"Help, or add a justified exemption"
    )

    stale_exemptions = [key for key in _SETTINGS_SECTIONS_EXEMPT_FROM_HELP if key not in sections]
    assert not stale_exemptions, (
        f"_SETTINGS_SECTIONS_EXEMPT_FROM_HELP names sections that no longer exist: "
        f"{sorted(stale_exemptions)} -- the section was removed or renamed, clean up the exemption"
    )


def test_help_inline_fallback_text_matches_en_json():
    texts = _help_i18n_texts(_HELP_VIEW_HTML)
    assert texts, "found no data-i18n=\"help.*\" elements with inline text -- extraction is broken"

    mismatches = {}
    for key, html_text in texts.items():
        assert key in _EN, f"{key} is used in the Help view but missing from en.json"
        en_text = re.sub(r"\s+", " ", _EN[key]).strip()
        if en_text != html_text:
            mismatches[key] = (html_text, en_text)

    assert not mismatches, (
        "index.html's inline fallback text has drifted from en.json for: "
        + "; ".join(
            f"{key} (html={html_text!r} vs en.json={en_text!r})"
            for key, (html_text, en_text) in sorted(mismatches.items())
        )
    )
