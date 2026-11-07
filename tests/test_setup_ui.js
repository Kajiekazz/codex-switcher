const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const script = html.slice(html.indexOf('  // ---- first-run guide'), html.indexOf('  // ---- dock:'));

function harness(respond) {
  const nodes = new Map(), calls = [];
  const element = () => {
    const classes = new Set();
    return { _text: '', hidden: false, disabled: false, checked: false, value: '', children: [], style: {},
      set textContent(value) { this._text = value || ''; this.children = []; },
      get textContent() { return this._text + this.children.map(child => child.textContent).join(''); },
      classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle() {} },
      setAttribute() {}, removeAttribute() {}, focus() {}, scrollIntoView() {}, addEventListener() {},
      appendChild(child) { this.children.push(child); return child; },
      querySelector() { if (!this.paragraph) this.paragraph = element(); return this.paragraph; } };
  };
  const $ = key => {
    if (!nodes.has(key)) nodes.set(key, element());
    return nodes.get(key);
  };
  $('#setupSteps').children = [0, 1, 2, 3].map(element);
  const context = vm.createContext({ $, closeSettings() {}, openLoginModal() {},
    el: (tag, text) => { const e = element(); e.textContent = text; return e; },
    LOGIN_LABELS: { waiting_for_input: '等待手动验证', queued: '排队中' }, api: async (url, options) => {
    const body = options && JSON.parse(options.body);
    calls.push({ url, body });
    return respond(url, body);
  } });
  vm.runInContext(script + `
    globalThis.ui = { renderSetup, refreshSetupGuide, finishSetup, loadStartup, changeStartup, installLoginBrowser,
      nextSetup, addSetupAccount, saveSetupProgress,
      setState(state, step = 0, sessions = []) { setupState = state; setupStep = step; setupSessions = sessions; },
      setChoice(value) { setupLocalChoice = value; },
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
  assert.equal(h.$('#setupComponents').hidden, false);
  assert.equal(h.$('#setupAccount').hidden, true);
  assert.match(h.$('#setupBrowserStatus').textContent, /network unavailable/);
});

test('the selected local account is imported on next without downloading a browser', async () => {
  let state = { codex: { available: true }, browser: { ready: false }, browser_mode: 'local',
    accounts: { total: 0, ready: 0 }, local_account: { available: true, email_masked: 'f***@example.com', fingerprint: 'fixture-hash', imported: false } };
  const h = harness((url, body) => {
    if (url === '/api/setup/import') {
      state = { ...state, accounts: { total: 1, ready: 1 }, local_account: { ...state.local_account, imported: true } };
      return { setup: state, errors: [] };
    }
    if (url === '/api/setup/progress') return { ...state, ...body };
    return { sessions: [] };
  });
  h.ui.setState(state, 2);
  h.ui.renderSetup();
  assert.equal(h.$('#setupImportLocal').checked, true);
  assert.equal(h.$('#setupNext').disabled, false);
  await h.ui.nextSetup();
  const imported = h.calls.find(call => call.url === '/api/setup/import');
  assert.deepEqual(imported.body, { confirm: true, text: '', identity_type: 'personal', import_local: true, local_fingerprint: 'fixture-hash' });
  assert.equal(h.calls.find(call => call.url === '/api/setup/progress').body.step, 3);
  assert.equal(h.calls.some(call => call.url === '/api/setup/browser'), false);
});

test('manual authorization can skip components while automatic login waits for them', () => {
  const h = harness(() => ({}));
  for (const [mode, disabled] of [['auto', true], ['manual', false], ['local', false]]) {
    h.ui.setState({ codex: { available: true }, browser: { ready: false }, accounts: {}, browser_mode: mode }, 1);
    h.ui.renderSetup();
    assert.equal(h.$('#setupNext').disabled, disabled);
    assert.equal(h.$('#setupManualLogin').textContent, mode === 'manual' ? '切换为自动登录' : '改用手动授权');
  }
});

test('an unchecked local account is not silently imported, and pending accounts stay visible', () => {
  const h = harness(() => ({}));
  h.ui.setState({ codex: { available: true }, accounts: { ready: 0, total: 1 },
    local_account: { available: true, imported: false },
    account_items: [{ email: 'fixture@example.com', email_masked: 'f***@example.com', ready: false }] }, 2,
    [{ email: 'fixture@example.com', status: 'waiting_for_input', message: '请填写邮箱验证码' }]);
  h.ui.setChoice(false);
  h.ui.renderSetup();
  assert.equal(h.$('#setupNext').disabled, true);
  assert.match(h.$('#setupAccounts').textContent, /等待手动验证/);
  assert.match(h.$('#setupAccounts').textContent, /邮箱验证码/);
});

test('later closes the guide only after the server persists the decision', async () => {
  const h = harness(() => ({}));
  await h.ui.finishSetup('later');
  assert.deepEqual(h.calls, [{ url: '/api/setup/finish', body: { confirm: true, action: 'later' } }]);
  assert.equal(h.$('#setupReturn').hidden, true);
});

test('a stale guide read cannot undo a newly saved component choice', async () => {
  let release;
  const initial = { codex: { available: true }, accounts: {}, browser: {}, browser_mode: 'auto' };
  const h = harness((url, body) => {
    if (url === '/api/setup/status') return new Promise(resolve => { release = resolve; });
    if (url === '/api/setup/progress') return { ...initial, browser_mode: body.browser_mode };
    return { sessions: [] };
  });
  h.ui.setState(initial, 1);
  const reading = h.ui.refreshSetupGuide();
  await h.ui.saveSetupProgress({ browser_mode: 'manual' });
  release(initial);
  await reading;
  h.ui.renderSetup();
  assert.equal(h.$('#setupNext').disabled, false);
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
