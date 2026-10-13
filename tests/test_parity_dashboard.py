"""Run the parity editor's state transitions with a minimal DOM, offline."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node unavailable")
def test_effort_editor_groups_and_grid():
    source = (Path(__file__).parents[1] / "claude_unlimited/static/app.js").read_text()
    start = source.index("let _parityRows =")
    end = source.index("async function resetModelParity()", start)
    # Include Reset's function body, ending before the next section.
    end = source.index("\n}\n", end) + 3
    harness = r'''
const assert = require('node:assert/strict');
const elements = new Map();
const callbacks = new Map();
const document = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { innerHTML: '', style: {}, querySelectorAll: () => [] });
  return elements.get(id);
}};
const esc = (v) => String(v).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const t = (key) => key;
const setupSelectInput = (element, options, callback) => callbacks.set(element, { options, callback });
const showToast = () => {};
const refreshCodexSelectOptions = () => {};
let _selectableModels = ['base', 'override'];
let _reasoningEfforts = ['none', 'low', 'medium', 'high', 'xhigh', 'max'];
let _codexMappingRows = null;
let calls = [];
const api = async (path, options) => {
  calls.push(path);
  if (path === '/api/settings') { assert.equal(JSON.parse(options.body).model_parity[1].source_effort, 'max'); return {}; }
  if (path === '/api/codex/model-map') return {
    mapping: [{claude_model: 'claude-fable-5-1', source_effort: '*', openai_model: 'base', reasoning_effort: 'follow'}],
    claude_efforts: ['low', 'medium', 'high', 'xhigh', 'max']
  };
  return { grid: [{claude_model: '<model>', requested_effort: null, row_index: 0,
                   model: 'base', effort: 'high', claude_effort: null, note: 'follows'}] };
};
'''
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
  assert.deepEqual(calls, ['/api/settings', '/api/codex/model-map', '/api/model-parity/grid']);
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('&lt;model>'));
  assert.ok(document.getElementById('parityGridRows').innerHTML.includes('settings.parity.leave_requested'));
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
    result = subprocess.run(["node", "-"], input=harness + source[start:end] + checks,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
