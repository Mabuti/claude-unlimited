import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "claude_unlimited" / "locales"
APP_JS = ROOT / "claude_unlimited" / "static" / "app.js"

BUDGET_KEYS = {
    "settings.context1m.budget_one": ("{budget}",),
    "settings.context1m.budget_on": ("{budget}", "{models}"),
    "settings.context1m.budget_list_sep": (),
}


NODE_LOCALE_PRELUDE = r'''
const assert = require('node:assert/strict');
let _strings;
function t(key) {
  return _strings[key] || key;
}
function localized(locale, key, replacements = {}) {
  return Object.entries(replacements).reduce(
    (text, [placeholder, value]) => text.replace(placeholder, () => String(value)),
    locales[locale][key],
  );
}
'''


def _slice_js_function(source, start_marker, end_marker):
    try:
        start = source.index(start_marker)
    except ValueError as exc:
        raise AssertionError(f"missing marker {start_marker!r}") from exc
    try:
        end = source.index(end_marker, start)
    except ValueError as exc:
        raise AssertionError(
            f"missing marker {end_marker!r} after {start_marker!r}"
        ) from exc
    return source[start:end]


def _guard_budget_summary_body():
    """Slice the summary function through the following declaration.

    Keep this source standalone: it may depend only on ``t`` and
    ``_strings`` supplied by the Node harness.
    """
    source = APP_JS.read_text(encoding="utf-8")
    return _slice_js_function(
        source,
        "function _guardBudgetSummary",
        "\nfunction _guardAssumptionLines",
    )


def _guard_verdict_body():
    """Slice each context-guard function through its next declaration.

    Keep this source standalone: it may depend only on ``t``/``_strings`` and
    the DOM methods stubbed by the verdict harness.
    """
    source = APP_JS.read_text(encoding="utf-8")
    slices = [
        _slice_js_function(
            source,
            "function _guardBudgetSummary",
            "\nfunction _guardAssumptionLines",
        ),
        _slice_js_function(
            source,
            "function _guardAssumptionLines",
            "\nfunction renderContext1mVerdict",
        ),
        _slice_js_function(
            source,
            "function renderContext1mVerdict",
            "\nasync function toggleFableLimitAll",
        ),
    ]
    return "\n".join(slices)


def _set_language_body():
    source = APP_JS.read_text(encoding="utf-8")
    return _slice_js_function(source, "async function setLanguage(", "\n// ---- helpers ----")


def test_budget_line_keys_and_placeholders_exist_in_every_locale():
    """Protect translated budget-line keys and their required placeholders."""
    for code in ("en", "de", "es", "ro"):
        strings = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for key, placeholders in BUDGET_KEYS.items():
            assert key in strings, f"{code}.json is missing {key}"
            for placeholder in placeholders:
                assert placeholder in strings[key], f"{code}.json:{key} lacks {placeholder}"


def test_budget_line_locale_values_have_no_legacy_qualifier_or_parenthesis():
    """Keep budget-line translations free of the old qualifier and markup."""
    for code in ("en", "de", "es", "ro"):
        strings = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for key in BUDGET_KEYS:
            value = strings[key]
            assert "smallest" not in value.casefold(), f"{code}.json:{key}"
            assert "(" not in value, f"{code}.json:{key}"


def test_budget_line_does_not_reintroduce_english_formatting_literals():
    """Protect budget summaries from English literals and unsafe data replacement."""
    source = _guard_budget_summary_body()

    assert "smallest of these" not in source
    assert "} on ${" not in source
    assert "K on " not in source


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_guard_budget_summary_renders_all_cases_in_every_locale():
    """Run rendered budget lines instead of only checking their source."""
    source = _guard_budget_summary_body()
    locales = {
        code: json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for code in ("en", "de", "es", "ro")
    }
    harness = (
        NODE_LOCALE_PRELUDE
        + "\nconst locales = "
        + json.dumps(locales)
        + ";\n_strings = locales.en;\n"
    )
    checks = r'''
const budgetOne = 'settings.context1m.budget_one';
const budgetOn = 'settings.context1m.budget_on';
const listSeparator = 'settings.context1m.budget_list_sep';

assert.equal(
  _guardBudgetSummary({budget: 226400}),
  '~226K',
);
assert.equal(
  _guardBudgetSummary({budget: 226400}),
  localized('en', budgetOne, {'{budget}': 226}),
);

const sharedEntries = [
  {model: 'gpt-5.6-sol', budget: 226400},
  {model: 'gpt-5.6-terra', budget: 226400},
];
assert.equal(
  _guardBudgetSummary({model_budgets: sharedEntries}),
  '~226K on gpt-5.6-sol, gpt-5.6-terra',
);
assert.equal(
  _guardBudgetSummary({model_budgets: sharedEntries}),
  localized('en', budgetOn, {
    '{budget}': 226,
    '{models}': sharedEntries.map(({model}) => model).join(localized('en', listSeparator)),
  }),
);

const distinctBudgets = [
  {model: 'gpt-6.1-sol', budget: 443000},
  {model: 'gpt-6-sol', budget: 226400},
];
_strings = locales.en;
assert.equal(
  _guardBudgetSummary({model_budgets: distinctBudgets}),
  '~443K on gpt-6.1-sol, ~226K on gpt-6-sol',
);
assert.equal(
  _guardBudgetSummary({model_budgets: distinctBudgets}),
  distinctBudgets.map(({model, budget}) => localized('en', budgetOn, {
    '{budget}': Math.floor(budget / 1000),
    '{models}': model,
  })).join(localized('en', listSeparator)),
);

const renderedByLocale = {};
for (const code of Object.keys(locales)) {
  _strings = locales[code];
  const noList = _guardBudgetSummary({budget: 226400});
  const shared = _guardBudgetSummary({model_budgets: sharedEntries});
  const distinct = _guardBudgetSummary({model_budgets: distinctBudgets});
  renderedByLocale[code] = {noList, shared, distinct};
  assert.equal(noList, localized(code, budgetOne, {'{budget}': 226}));
  assert.equal(shared, localized(code, budgetOn, {
    '{budget}': 226,
    '{models}': sharedEntries.map(({model}) => model).join(localized(code, listSeparator)),
  }));
  assert.equal(distinct, distinctBudgets.map(({model, budget}) => localized(code, budgetOn, {
    '{budget}': Math.floor(budget / 1000),
    '{models}': model,
  })).join(localized(code, listSeparator)));
  assert.ok(!noList.includes(budgetOne), `${code} rendered a raw locale key`);
  assert.ok(!shared.includes(budgetOn), `${code} rendered a raw locale key`);
  assert.ok(!distinct.includes(budgetOn), `${code} rendered a raw locale key`);
}
assert.equal(renderedByLocale.de.shared, '~226K für gpt-5.6-sol, gpt-5.6-terra');
assert.equal(renderedByLocale.de.distinct, '~443K für gpt-6.1-sol, ~226K für gpt-6-sol');
assert.notEqual(renderedByLocale.de.shared, renderedByLocale.en.shared);
assert.notEqual(renderedByLocale.es.shared, renderedByLocale.en.shared);
assert.notEqual(renderedByLocale.ro.shared, renderedByLocale.en.shared);

_strings = locales.en;
const literalModel = "gpt-$&-$'-x";
assert.equal(
  _guardBudgetSummary({model_budgets: [{model: literalModel, budget: 272000}]}),
  "~272K on gpt-$&-$'-x",
);
assert.equal(
  _guardBudgetSummary({model_budgets: [{model: literalModel, budget: 272000}]}),
  localized('en', budgetOn, {'{budget}': 272, '{models}': literalModel}),
);

_strings = locales.de;
const german = _guardBudgetSummary({
  model_budgets: sharedEntries,
});
assert.equal(
  german,
  '~226K für gpt-5.6-sol, gpt-5.6-terra',
);
'''
    result = subprocess.run(
        ["node", "-"],
        input=harness + source + checks,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_set_language_rerenders_context1m_verdict_in_new_locale():
    """Repaint the settings verdict with the newly selected locale strings."""
    locales = {
        code: json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for code in ("en", "de")
    }
    harness = (
        NODE_LOCALE_PRELUDE
        + "\nconst locales = "
        + json.dumps(locales)
        + ";\n_strings = locales.en;\n"
        + r'''
let _lastContext1mPreview = null;
const rendered = [];
const verdict = {
  style: {},
  classList: {toggle: () => {}},
  replaceChildren: (...children) => {
    rendered.splice(0, rendered.length, ...children.map(child => child.textContent));
  },
};
const document = {
  getElementById: id => id === 'context1mVerdict' ? verdict : null,
  querySelectorAll: () => [],
  createElement: tag => {
    assert.equal(tag, 'div');
    return {textContent: ''};
  },
};
const applyTranslations = () => {};
const renderNotifList = () => {};
const renderLauncherRows = () => {};
const renderParityRows = () => {};
const renderParityGrid = () => {};
const refreshCodexSelectOptions = () => {};
const updateCodexModelHelp = () => {};
const api = async path => {
  assert.equal(path, '/api/locales/de');
  return {strings: locales.de};
};
'''
    )
    checks = r'''
(async () => {
  const preview = {
    reason: 'enabled_1m_guarded',
    enabled: true,
    guard: {profiles: [{
      name: 'Team',
      model_budgets: [{model: 'gpt-6.1-sol', budget: 443000}],
    }]},
  };
  renderContext1mVerdict(preview);
  assert.ok(rendered[1].includes('on'));
  await setLanguage('de', {persist: false});
  assert.ok(rendered[1].includes('für'), rendered.join('\n'));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    result = subprocess.run(
        ["node", "-"],
        input=harness + _guard_verdict_body() + _set_language_body() + checks,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_render_context1m_verdict_replacers_keep_profile_and_model_literals():
    """Render verdict lines through a minimal DOM and preserve replacement text."""
    source = _guard_verdict_body()
    locales = {
        "en": json.loads((LOCALES / "en.json").read_text(encoding="utf-8")),
    }
    harness = (
        NODE_LOCALE_PRELUDE
        + "\nconst locales = "
        + json.dumps(locales)
        + ";\n_strings = locales.en;\n"
        + r'''
const rendered = [];
const verdict = {
  style: {},
  classList: {toggle: () => {}},
  replaceChildren: (...children) => {
    rendered.splice(0, rendered.length, ...children.map(child => child.textContent));
  },
};
const document = {
  getElementById: id => {
    assert.equal(id, 'context1mVerdict');
    return verdict;
  },
  createElement: tag => {
    assert.equal(tag, 'div');
    return {textContent: ''};
  },
};
'''
    )
    checks = r'''
renderContext1mVerdict({
  reason: 'enabled_1m_guarded',
  enabled: true,
  guard: {profiles: [{
    name: 'Team $& lab',
    assumed: true,
    model_budgets: [{model: 'gpt-$&-x', budget: 272000}],
    assumptions: [{reason: 'unknown_model', model: 'gpt-$&-x'}],
  }]},
});
assert.equal(rendered.length, 3);
const guardLine = rendered[1];
const assumptionLine = rendered[2];
assert.equal(guardLine, localized('en', 'settings.context1m.guard_line', {
  '{name}': 'Team $& lab',
  '{budget_summary}': '~272K on gpt-$&-x',
}));
assert.equal(assumptionLine, localized('en', 'settings.context1m.guard_assumed', {
  '{name}': 'Team $& lab',
  '{models}': 'gpt-$&-x',
}));
assert.ok(guardLine.includes('Team $& lab'));
assert.ok(guardLine.includes('gpt-$&-x'));
assert.ok(assumptionLine.includes('Team $& lab'));
assert.ok(assumptionLine.includes('gpt-$&-x'));
'''
    result = subprocess.run(
        ["node", "-"],
        input=harness + source + checks,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
