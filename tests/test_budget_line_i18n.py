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


def _guard_budget_summary_body():
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function _guardBudgetSummary")
    end = source.index("\nfunction _guardAssumptionLines", start)
    return source[start:end]


def test_budget_line_keys_and_placeholders_exist_in_every_locale():
    """Protect translated budget-line keys and their required placeholders."""
    for code in ("en", "de", "es", "ro"):
        strings = json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for key, placeholders in BUDGET_KEYS.items():
            assert key in strings, f"{code}.json is missing {key}"
            for placeholder in placeholders:
                assert placeholder in strings[key], f"{code}.json:{key} lacks {placeholder}"


def test_budget_line_does_not_reintroduce_english_formatting_literals():
    """Protect budget summaries from English literals and unsafe data replacement."""
    source = _guard_budget_summary_body()

    assert "smallest of these" not in source
    assert "} on ${" not in source
    assert "K on " not in source
    assert (
        ".replace('{models}', () => modelList)" in source
        and ".replace('{models}', modelList)" not in source
    )


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_guard_budget_summary_runs_in_node_with_real_locales():
    """Run the rendered budget lines instead of only checking their source."""
    source = _guard_budget_summary_body()
    locales = {
        code: json.loads((LOCALES / f"{code}.json").read_text(encoding="utf-8"))
        for code in ("en", "de")
    }
    harness = r'''
const assert = require('node:assert/strict');
const locales = ''' + json.dumps(locales) + r''';
let _strings = locales.en;
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
    checks = r'''
const budgetOne = 'settings.context1m.budget_one';
const budgetOn = 'settings.context1m.budget_on';
const listSeparator = 'settings.context1m.budget_list_sep';

assert.equal(
  _guardBudgetSummary({budget: 272000}),
  localized('en', budgetOne, {'{budget}': 272}),
);

const sharedModels = ['gpt-a', 'gpt-b'];
assert.equal(
  _guardBudgetSummary({model_budgets: sharedModels.map(model => ({model, budget: 272000}))}),
  localized('en', budgetOn, {
    '{budget}': 272,
    '{models}': sharedModels.join(localized('en', listSeparator)),
  }),
);

const distinctBudgets = [
  {model: 'gpt-a', budget: 272000},
  {model: 'gpt-b', budget: 128000},
];
assert.equal(
  _guardBudgetSummary({model_budgets: distinctBudgets}),
  distinctBudgets.map(({model, budget}) => localized('en', budgetOn, {
    '{budget}': budget / 1000,
    '{models}': model,
  })).join(localized('en', listSeparator)),
);

const literalModel = "gpt-$&-$'-x";
assert.equal(
  _guardBudgetSummary({model_budgets: [{model: literalModel, budget: 272000}]}),
  localized('en', budgetOn, {'{budget}': 272, '{models}': literalModel}),
);

_strings = locales.de;
const germanModels = ['gpt-a', 'gpt-b'];
const german = _guardBudgetSummary({
  model_budgets: germanModels.map(model => ({model, budget: 272000})),
});
assert.equal(
  german,
  localized('de', budgetOn, {
    '{budget}': 272,
    '{models}': germanModels.join(localized('de', listSeparator)),
  }),
);
assert.ok(!german.includes(budgetOn));
assert.notEqual(german, localized('en', budgetOn, {
  '{budget}': 272,
  '{models}': germanModels.join(localized('en', listSeparator)),
}));
'''
    result = subprocess.run(
        ["node", "-"],
        input=harness + source + checks,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
