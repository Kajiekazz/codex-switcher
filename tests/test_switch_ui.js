const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const fragment = html.slice(html.indexOf('  function renderSwitchToggles('), html.indexOf('  function renderSms('));
const binding = html.split('\n').find(line => line.includes('$("#toggleAutoSwitch").addEventListener('));

function harness() {
  const nodes = new Map();
  const calls = [];
  let accept = true;
  const $ = selector => {
    assert.notEqual(selector, '#toggleSwitch', 'removed permission checkbox must not be accessed');
    if (!nodes.has(selector)) nodes.set(selector, {
      checked: false, disabled: false, textContent: '', listeners: {},
      addEventListener(event, callback) { this.listeners[event] = callback; },
    });
    return nodes.get(selector);
  };
  const context = vm.createContext({
    $, window: { confirm: () => accept }, async refresh() {}, renderAutoSwitch() {},
    api: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return {};
    },
  });
  const manual = html.slice(html.indexOf('  function updateSwitchButton('), html.indexOf('  function renderSwitch('));
  vm.runInContext(fragment + manual + binding + '\nglobalThis.render = renderSwitchToggles; globalThis.update = updateSwitchButton;', context);
  return { $, calls, render: context.render, update: context.update, decline() { accept = false; } };
}

test('automatic switching remains available without a manual permission checkbox', () => {
  assert.doesNotMatch(html, /id="toggleSwitch"/);
  const h = harness();
  h.render({ switching: { enabled: false }, auto_switch: { enabled: false } });
  assert.equal(h.$('#toggleAutoSwitch').disabled, false);
  assert.match(h.$('#toggleHint').textContent, /只在手动点/);
});

test('the automatic switch checkbox submits its own setting', async () => {
  const h = harness();
  await h.$('#toggleAutoSwitch').listeners.change({ target: { checked: true } });
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].url, '/api/switching/toggle');
  assert.equal(h.calls[0].body.key, 'enable_auto_switch');
  assert.equal(h.calls[0].body.value, true);
  assert.equal(h.calls[0].body.confirm, true);
});

test('declining the automatic switch confirmation sends nothing', async () => {
  const h = harness();
  h.decline();
  await h.$('#toggleAutoSwitch').listeners.change({ target: { checked: true } });
  assert.equal(h.calls.length, 0);
});

test('manual switch only needs a target and operation confirmation', () => {
  const h = harness();
  h.$('#switchTarget').value = '';
  h.update();
  assert.equal(h.$('#switchBtn').disabled, true);
  h.$('#switchTarget').value = 'target@example.com';
  h.update();
  assert.equal(h.$('#switchBtn').disabled, true);
  h.$('#switchConfirm').checked = true;
  h.update();
  assert.equal(h.$('#switchBtn').disabled, false);
});
