"""Run the parity editor's state transitions with a minimal DOM, offline."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


def _run_editor(checks):
    markup = (Path(__file__).parents[1] / "claude_unlimited/static/index.html").read_text()
    assert re.search(r'<button\b(?=[^>]*\btype="button")(?=[^>]*\bid="parityAddBtn")[^>]*>', markup)
    source = (Path(__file__).parents[1] / "claude_unlimited/static/app.js").read_text()
    start = source.index("let _parityRows =")
    end = source.index("async function resetModelParity()", start)
    # Include Reset's function body, ending before the next section.
    end = source.index("\n}\n", end) + 3
    harness = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const callbacks = new Map();
function element(tag = 'div') {
  const attributes = new Set();
  const node = { style: {}, dataset: {}, title: '', children: [],
    hasAttribute: name => attributes.has(name),
    querySelectorAll: () => [],
    get innerHTML() { return this.html || ''; },
    set innerHTML(html) {
      this.children.forEach(id => elements.delete(id));
      this.children = [];
      this.html = html;
      for (const match of html.matchAll(/\bid="([^"]+)"/g)) {
        assert.ok(!elements.has(match[1]), `duplicate element: ${match[1]}`);
        elements.set(match[1], element());
        this.children.push(match[1]);
      }
    }
  };
  if (tag === 'button') Object.defineProperty(node, 'disabled', {
    get: () => attributes.has('disabled'),
    set: value => value ? attributes.add('disabled') : attributes.delete('disabled'),
  });
  return node;
}
for (const id of ['parityRows', 'parityGridRows', 'parityAddBtn', 'modelParitySection',
                  'parityNewBanner', 'parityNewBannerSub', 'parityAddMissingBtn']) elements.set(id, element(['parityAddBtn', 'parityAddMissingBtn'].includes(id) ? 'button' : 'div'));
const document = { getElementById: id => elements.get(id) || null };
const esc = (v) => String(v).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const t = (key) => key === 'settings.parity.reset_confirm' ? 'Changed: {changed}\nRemoved: {removed}' : key;
const setupSelectInput = (element, options, callback) => {
  assert.ok(element, 'cannot initialize a missing selector');
  callbacks.set(element, { options, callback });
};
let toasts = [];
const showToast = (...args) => { toasts.push(args); };
let confirmed = false;
let confirmation = null;
const window = { confirm: message => { confirmation = message; return confirmed; } };
const refreshCodexSelectOptions = () => {};
let _selectableModels = ['base', 'override'];
let _reasoningEfforts = ['none', 'low', 'medium', 'high', 'xhigh', 'max'];
let _codexMappingRows = null;
let calls = [];
let patches = [];
const api = async (path, options) => {
  calls.push(path);
  if (path === '/api/settings') { patches.push(JSON.parse(options.body)); return {}; }
  if (path === '/api/codex/model-map') return {
    mapping: [{claude_model: 'claude-fable-5-1', source_effort: '*', openai_model: 'base', reasoning_effort: 'follow'}],
    claude_efforts: ['low', 'medium', 'high', 'xhigh', 'max']
  };
  return { grid: [{claude_model: '<model>', requested_effort: null, row_index: 0,
                   source_effort: '*', model: 'base', effort: 'high', claude_effort: null, note: 'follows'},
                  {claude_model: '<model>', requested_effort: 'high', row_index: 9, source_effort: 'high',
                   model: 'override', effort: 'high', claude_effort: null, configured_claude_effort: 'max', note: 'override; not supported by this model'}] };
};
'''
    result = subprocess.run(["node", "-"], input=harness + source[start:end] + checks,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_effort_editor_groups_and_grid():
    checks = r'''
(async () => {
  _parityMeta.claude_efforts = ['low', 'medium', 'high', 'xhigh', 'max'];
  _parityRows = [
    {claude_model: 'claude-mythos-5-1', model: 'gpt-6-astra', effort: 'xhigh'},
    {claude_model: 'claude-mythos-5-1-20261001', model: 'gpt-6-luna', effort: 'low'},
    {claude_model: 'claude-opus-5-5', model: 'gpt-6-astra', effort: 'xhigh'},
    {claude_model: 'claude-opus-5-5-20260101', model: 'gpt-5.6-terra', effort: 'low'},
  ];
  renderParityRows();
  for (let i = 0; i < 4; i++) {
    assert.ok(document.getElementById('parityRows').innerHTML.includes(`id="parity_claude_${i}"`));
  }
  onAddEffortOverride(1);
  assert.equal(_parityRows[2].claude_model, 'claude-mythos-5-1-20261001');
  onDeleteParityRow(1);
  assert.equal(_parityRows[0].claude_model, 'claude-mythos-5-1');
  assert.equal(_parityRows.length, 3);
  _parityRows = [{claude_model: 'claude-fable-5-1', source_effort: '*', model: 'base', effort: 'follow'},
                 {claude_model: 'claude-sonnet-5', source_effort: '*', model: 'base', effort: 'low'}];
  onAddEffortOverride(0);
  assert.equal(_parityRows[1].source_effort, 'low');
  callbacks.get(document.getElementById('parity_source_1')).callback('max');
  callbacks.get(document.getElementById('parity_model_1')).callback('override');
  const saved = collectParity();
  assert.equal(saved[1].source_effort, 'max');
  assert.equal(saved[1].model, 'override');
  assert.equal(_codexEffortOptions()[0].value, 'follow');
  const rename = callbacks.get(document.getElementById('parity_claude_0')).callback;
  rename('claude-fable-6');
  assert.equal(_parityRows[1].claude_model, 'claude-fable-6');
  onDeleteParityRow(1);
  assert.equal(_parityRows.length, 2);
  onAddEffortOverride(0);
  onDeleteParityRow(0);
  assert.deepEqual(_parityRows.map(r => r.claude_model), ['claude-sonnet-5']);
  onDeleteParityRow(0);
  assert.equal(_parityRows.length, 1);
  onAddEffortOverride(0);
  _parityRows[1].source_effort = 'max';
  await saveModelParity();
  assert.equal(patches[0].model_parity[1].source_effort, 'max');
  assert.deepEqual(calls, ['/api/settings', '/api/codex/model-map', '/api/model-parity/grid']);
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('&lt;model>'));
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('settings.parity.leave_requested'));
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('<td>settings.parity.any</td>'));
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('<td>settings.parity.note_override: high</td>'));
  assert.ok(!document.getElementById('parityGridRows').innerHTML.includes('<td>10</td>'));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    _run_editor(checks)


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_two_adds_at_catalogue_exhaustion_and_independent_blank_rows():
    _run_editor(r"""
_parityMeta.claude_selectable = [{id: 'claude-one', label: 'One'}, {id: 'claude-two', label: 'Two'}];
_parityRows = [{claude_model: 'claude-one', model: 'base', effort: 'low'}];
renderParityRows();
const add = document.getElementById('parityAddBtn');
assert.equal(add.hasAttribute('disabled'), false);
onAddParityRow();
onAddParityRow();
assert.deepEqual(_parityRows.map(r => r.claude_model), ['claude-one', 'claude-two']);
assert.equal(add.hasAttribute('disabled'), true);
assert.ok(add.title);
onDeleteParityRow(1);
assert.equal(add.hasAttribute('disabled'), false);
assert.equal(add.title, '');
onAddParityRow();
assert.equal(_parityRows.length, 2);
_parityRows = [{claude_model: '', model: 'base'}, {claude_model: '', model: 'base'}];
renderParityRows();
assert.ok(document.getElementById('parity_claude_0'));
assert.ok(document.getElementById('parity_claude_1'));
callbacks.get(document.getElementById('parity_claude_0')).callback('claude-one');
assert.deepEqual(_parityRows.map(r => r.claude_model), ['claude-one', '']);
_parityRows.push({claude_model: '', model: 'base'});
renderParityRows();
onDeleteParityRow(1);
assert.deepEqual(_parityRows.map(r => r.claude_model), ['claude-one', '']);
""")


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
@pytest.mark.parametrize('action', ['onAddParityRow()', 'onAddMissingParityRows()', 'onAddEffortOverride(0)'])
def test_all_add_paths_cap_at_64_and_reenable_after_delete(action):
    _run_editor(r'''
_parityRows = Array.from({length: 63}, (_, i) => ({claude_model: `claude-test-${i}`, model: 'base', effort: 'follow'}));
_parityMeta.claude_efforts = ['low', 'medium', 'high', 'xhigh', 'max'];
_parityMeta.claude_selectable = [{id: 'claude-new'}, {id: 'claude-another'}];
_parityUnmapped = [{claude_model: 'claude-new', would_map_to: 'base'}, {claude_model: 'claude-another', would_map_to: 'base'}];
renderParityRows();
assert.equal(document.getElementById('parityAddBtn').disabled, false);
''' + action + r''';
assert.equal(_parityRows.length, 64);
assert.equal(_nextParitySeed(), null);
for (const id of ['parityAddBtn', 'parityAddMissingBtn']) {
  assert.equal(document.getElementById(id).disabled, true);
  assert.equal(document.getElementById(id).title, 'settings.parity.row_limit');
}
const overrideButtons = [...document.getElementById('parityRows').innerHTML.matchAll(/<button class="btn parity-override"[^>]+>/g)];
assert.ok(overrideButtons.length);
assert.ok(overrideButtons.every(m => m[0].includes('disabled') && m[0].includes('title="settings.parity.row_limit"')));
onAddParityRow();
onAddMissingParityRows();
onAddEffortOverride(0);
assert.equal(_parityRows.length, 64);
assert.ok(_parityUnmapped.length, 'bulk overflow candidates must remain available');
onDeleteParityRow(63);
assert.equal(_parityRows.length, 63);
assert.equal(document.getElementById('parityAddBtn').disabled, false);
assert.equal(document.getElementById('parityAddMissingBtn').disabled, false);
onAddMissingParityRows();
assert.equal(_parityRows.length, 64);
''')


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_reset_lists_changed_and_removed_ids_cancel_preserves_everything():
    _run_editor(r'''
(async () => {
  _parityMeta.defaults = [
    {claude_model: 'claude-fable-5-1', model: 'base', effort: 'follow'},
    {claude_model: 'claude-opus-5-5', model: 'base', effort: 'follow'},
    {claude_model: 'claude-sonnet-5', model: 'base', effort: 'follow'},
    {claude_model: 'claude-haiku-4-5', model: 'base', effort: 'follow'},
  ];
  _parityRows = [
    {claude_model: 'claude-fable-5-1', model: 'base', effort: 'medium'},
    {claude_model: 'claude-opus-5-5', model: 'override', effort: 'follow'},
    {claude_model: 'claude-sonnet-5', model: 'base', effort: 'follow'},
    {claude_model: 'claude-sonnet-5', source_effort: 'high', model: 'override', effort: 'follow'},
    {claude_model: 'claude-haiku-4-5', model: 'base', effort: 'follow'},
    {claude_model: 'claude-mythos-5-1', model: 'override', effort: 'high'},
  ];
  _paritySavedRows = [{claude_model: 'claude-saved-deleted', model: 'base', effort: 'low'}];
  const before = JSON.stringify(_parityRows);
  const saved = JSON.stringify(_paritySavedRows);
  const defaults = JSON.stringify(_parityMeta.defaults);
  await resetModelParity();
  assert.equal(calls.length, 0);
  assert.equal(JSON.stringify(_parityRows), before);
  assert.equal(JSON.stringify(_paritySavedRows), saved);
  assert.equal(confirmation, 'Changed: claude-fable-5-1: *: base/medium (Claude: settings.parity.leave_requested) → base/settings.parity.follow (Claude: settings.parity.leave_requested)\nclaude-opus-5-5: *: override/settings.parity.follow (Claude: settings.parity.leave_requested) → base/settings.parity.follow (Claude: settings.parity.leave_requested)\nclaude-sonnet-5: *: base/settings.parity.follow (Claude: settings.parity.leave_requested), high: override/settings.parity.follow (Claude: settings.parity.leave_requested) → base/settings.parity.follow (Claude: settings.parity.leave_requested)\nRemoved: claude-saved-deleted: *: base/low (Claude: settings.parity.leave_requested)\nclaude-mythos-5-1: *: override/high (Claude: settings.parity.leave_requested)');
  confirmed = true;
  await resetModelParity();
  assert.equal(patches.length, 1);
  assert.equal(JSON.stringify(patches[0].model_parity), defaults);
  assert.ok(patches[0].model_parity.every(r => r.effort === 'follow'));
  assert.equal(toasts.filter(t => t[0] === 'error').length, 0);
})().catch(e => { console.error(e); process.exitCode = 1; });
''')


@pytest.mark.skipif(not shutil.which('node'), reason='node unavailable')
def test_reset_warns_about_restored_models_and_claude_effort_changes():
    _run_editor(r'''
(async () => {
  _parityMeta.defaults = [
    {claude_model: 'claude-existing', model: 'base', effort: 'follow'},
    {claude_model: 'claude-restored', model: 'base', effort: 'follow'},
  ];
  _parityRows = [{claude_model: 'claude-existing', model: 'base', effort: 'follow', claude_effort: 'high'}];
  await resetModelParity();
  assert.equal(confirmation, 'Changed: claude-existing: *: base/settings.parity.follow (Claude: high) → base/settings.parity.follow (Claude: settings.parity.leave_requested)\nclaude-restored: settings.parity.not_present → base/settings.parity.follow (Claude: settings.parity.leave_requested)\nRemoved: settings.parity.no_changes');
  assert.equal(calls.length, 0);
})().catch(e => { console.error(e); process.exitCode = 1; });
''')


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_grid_distinguishes_suppressed_claude_effort_from_unset():
    _run_editor(r'''
(async () => {
  await loadParityGrid();
  const html = elements.get('parityGridRows').innerHTML;
  assert.ok(html.includes('<td>settings.parity.leave_requested</td>'));
  assert.ok(html.includes('<td><s>max</s> settings.parity.not_injected</td>'));
})().catch(e => { console.error(e); process.exitCode = 1; });
''')
