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
    return { _text: '', hidden: false, disabled: false, checked: false, value: '', children: [], style: {}, dataset: {},
      set textContent(value) { this._text = value || ''; this.children = []; },
      get textContent() { return this._text + this.children.map(child => child.textContent).join(''); },
      classList: { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x), toggle() {} },
      setAttribute(name, value) { this[name] = value; }, removeAttribute(name) { delete this[name]; }, focus() {}, scrollIntoView() {}, addEventListener() {},
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
      nextSetup, addSetupAccount, saveSetupProgress, changeBrowserDownloadSource,
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

const downloadSources = [
  { id: 'official_cdn', label: '官方 CDN', host: 'cdn.example.invalid', status: 'pending' },
  { id: 'official_cft', label: '官方 Chrome for Testing', host: 'storage.example.invalid', status: 'pending' },
];
const componentState = browser => ({ codex: { available: true }, accounts: {}, browser_mode: 'auto', browser });

test('download choices come only from the server and selecting a source does not install', () => {
  const h = harness(() => ({}));
  h.ui.setState(componentState({ state: 'missing', sources: downloadSources }), 1);
  h.ui.renderSetup();
  assert.deepEqual(h.$('#setupBrowserSource').children.map(option => option.value), ['auto', 'official_cdn', 'official_cft']);
  assert.equal(h.$('#setupInstallBrowser').textContent, '测速并下载');
  h.$('#setupBrowserSource').value = 'official_cft';
  h.ui.changeBrowserDownloadSource();
  assert.equal(h.$('#setupInstallBrowser').textContent, '从此源下载');
  assert.equal(h.calls.length, 0);
  h.ui.setState(componentState({ state: 'missing', sources: downloadSources,
    source: downloadSources[0] }), 1);
  h.ui.renderSetup();
  assert.equal(h.$('#setupBrowserSource').value, 'official_cft');
});

test('probing shows per-source results and does not invent download throughput', () => {
  const h = harness(() => ({}));
  h.ui.setState(componentState({ state: 'installing', phase: 'probing', progress: 0,
    speed_bps: null, downloaded_bytes: null, total_bytes: null,
    sources: [{ ...downloadSources[0], status: 'testing' },
      { ...downloadSources[1], status: 'available', latency_ms: 42, speed_bps: 2097152 }] }), 1);
  h.ui.renderSetup();
  assert.match(h.$('#setupBrowserTransfer').textContent, /正在测速选择下载源/);
  assert.doesNotMatch(h.$('#setupBrowserTransfer').textContent, /下载速度|已下载|NaN/);
  assert.match(h.$('#setupBrowserSources').textContent, /官方 CDN · 测速中/);
  assert.match(h.$('#setupBrowserSources').textContent, /测速 2.0 MB\/s · 42 ms/);
  assert.equal(h.$('#setupInstallBrowser').textContent, '测速中…');
  assert.equal(h.$('#setupBrowserSource').disabled, true);
  assert.equal(h.$('#setupBrowserProgress').value, undefined);
  assert.equal(h.$('#setupNext').disabled, true);
});

test('download phase names its source, attempt, bytes and actual transfer speed', () => {
  const h = harness(() => ({}));
  h.ui.setState(componentState({ state: 'installing', phase: 'downloading', progress: 25,
    source: downloadSources[1], sources: downloadSources, attempt: 2, attempts: 3,
    downloaded_bytes: 10485760, total_bytes: 41943040, speed_bps: 1048576 }), 1);
  h.ui.renderSetup();
  assert.match(h.$('#setupBrowserTransfer').textContent, /当前下载源：官方 Chrome for Testing/);
  assert.match(h.$('#setupBrowserTransfer').textContent, /尝试 2\/3/);
  assert.match(h.$('#setupBrowserTransfer').textContent, /已下载 10.0 MB \/ 40.0 MB/);
  assert.match(h.$('#setupBrowserTransfer').textContent, /下载速度 1.0 MB\/s/);
  assert.equal(h.$('#setupBrowserProgress').value, 25);
});

test('failed sources remain understandable and retry/manual authorization stay available', () => {
  const h = harness(() => ({}));
  h.ui.setState(componentState({ state: 'failed', phase: 'failed', message: '所有下载源暂时不可用，可重试或改用手动授权',
    sources: [{ ...downloadSources[0], status: 'failed', error: '连接超时' }] }), 1);
  h.ui.renderSetup();
  assert.match(h.$('#setupBrowserSources').textContent, /官方 CDN · 不可用：连接超时/);
  assert.equal(h.$('#setupInstallBrowser').disabled, false);
  assert.equal(h.$('#setupInstallBrowser').textContent, '重试下载');
  assert.equal(h.$('#setupBrowserSource').disabled, false);
  assert.equal(h.$('#setupBrowserProgress').hidden, true);
  assert.equal(h.$('#setupManualLogin').textContent, '改用手动授权');
});

test('the selected source id is submitted once, and a source cannot change during submission', async () => {
  const state = componentState({ state: 'missing', sources: downloadSources });
  let complete;
  const h = harness(url => url === '/api/setup/browser' ? new Promise(resolve => { complete = resolve; }) : state);
  h.ui.setState(state, 1); h.ui.renderSetup();
  h.$('#setupBrowserSource').value = 'official_cft'; h.ui.changeBrowserDownloadSource();
  const first = h.ui.installLoginBrowser();
  for (let i = 0; i < 8; i++) await Promise.resolve();
  await h.ui.installLoginBrowser();
  h.$('#setupBrowserSource').value = 'official_cdn'; h.ui.changeBrowserDownloadSource();
  assert.equal(h.$('#setupInstallBrowser').disabled, true);
  const starts = h.calls.filter(call => call.url === '/api/setup/browser');
  assert.equal(starts.length, 1);
  assert.deepEqual(starts[0].body, { confirm: true, source_id: 'official_cft' });
  complete({ browser: { state: 'installing', phase: 'probing', sources: downloadSources } });
  await first;
  assert.equal(h.$('#setupBrowserSource').value, 'official_cft');
});

test('an unsupported selected source falls back to auto and is never forwarded as a URL', async () => {
  const state = componentState({ state: 'missing', sources: downloadSources });
  const h = harness(url => url === '/api/setup/browser' ? { browser: { state: 'installing', phase: 'probing' } } : state);
  h.ui.setState(state, 1); h.ui.renderSetup();
  h.$('#setupBrowserSource').value = 'https://untrusted.example.invalid'; h.ui.changeBrowserDownloadSource();
  await h.ui.installLoginBrowser();
  assert.equal(h.calls.find(call => call.url === '/api/setup/browser').body.source_id, 'auto');
});

test('the ready state unlocks the next step only after verification succeeds', () => {
  const h = harness(() => ({}));
  for (const [state, phase, ready] of [['installing', 'verifying', false], ['ready', 'ready', true]]) {
    h.ui.setState(componentState({ state, phase, ready, progress: 100, sources: downloadSources }), 1);
    h.ui.renderSetup();
    assert.equal(h.$('#setupNext').disabled, !ready);
    assert.equal(h.$('#setupInstallBrowser').textContent, ready ? '组件已准备' : '验证中…');
  }
});
