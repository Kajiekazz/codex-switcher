const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const script = html.slice(html.indexOf('  // ---- first-run guide'), html.indexOf('  // ---- dock:'));

function harness(respond) {
  const nodes = new Map(), calls = [];
  const $ = key => {
    if (!nodes.has(key)) {
      const classes = new Set();
      nodes.set(key, { textContent: '', hidden: false, disabled: false, checked: false,
        children: [], classList: { add: x => classes.add(x), remove: x => classes.delete(x),
          contains: x => classes.has(x), toggle: () => {} },
        setAttribute() {}, removeAttribute() {}, focus() {}, scrollIntoView() {} });
    }
    return nodes.get(key);
  };
  $('#setupSteps').children = [0, 1, 2].map(() => ({ classList: { toggle() {} } }));
  const context = vm.createContext({ $, closeSettings() {}, api: async (url, options) => {
    const body = options && JSON.parse(options.body);
    calls.push({ url, body });
    return respond(url, body);
  } });
  vm.runInContext(script + `
    globalThis.ui = { renderSetup, refreshSetupGuide, finishSetup, loadStartup, changeStartup, installLoginBrowser,
      setState(state, step = 0) { setupState = state; setupStep = step; },
      pending() { return setupPending; } };
  `, context);
  return { $, calls, ui: context.ui };
}

test('first run opens a guide, while existing users go straight to the dashboard', async () => {
  for (const first of [true, false]) {
    const h = harness(() => ({ show_guide: first, codex: {}, accounts: {}, browser: {} }));
    await h.ui.refreshSetupGuide(true);
    assert.equal(h.$('#setupModal').classList.contains('open'), first);
    assert.equal(h.calls.some(x => x.url === '/api/setup/browser'), false);
  }
});

test('guide completion waits for actual authorization and renders download failure with retry', () => {
  const h = harness(() => ({}));
  h.ui.setState({ codex: { available: true, path: 'codex.exe' }, accounts: { total: 1, ready: 0 },
    browser: { state: 'failed', message: 'network unavailable' } }, 1);
  h.ui.renderSetup();
  assert.equal(h.$('#setupNext').disabled, true);
  assert.equal(h.$('#setupInstallBrowser').textContent, '重试下载');
  assert.equal(h.$('#setupAccount').hidden, false);
  assert.match(h.$('#setupBrowserStatus').textContent, /network unavailable/);
});

test('later closes the guide only after the server persists the decision', async () => {
  const h = harness(() => ({}));
  await h.ui.finishSetup('later');
  assert.deepEqual(h.calls, [{ url: '/api/setup/finish', body: { confirm: true, action: 'later' } }]);
  assert.equal(h.$('#setupReturn').hidden, true);
});

test('startup changes immediately and an older read cannot undo the new choice', async () => {
  let release;
  const h = harness((url, body) => body ? { enabled: body.enabled } : new Promise(resolve => { release = resolve; }));
  const read = h.ui.loadStartup();
  h.$('#startupToggle').checked = true;
  await h.ui.changeStartup();
  release({ available: true, enabled: false });
  await read;
  assert.equal(h.$('#startupToggle').checked, true);
  assert.deepEqual(h.calls[1].body, { confirm: true, enabled: true });
});

test('a failed startup write restores the checkbox and reports the error', async () => {
  const h = harness(() => { throw Error('registry denied'); });
  h.$('#startupToggle').checked = true;
  await h.ui.changeStartup();
  assert.equal(h.$('#startupToggle').checked, false);
  assert.match(h.$('#startupMsg').textContent, /registry denied/);
});
