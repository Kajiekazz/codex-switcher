const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const loginScript = html.slice(html.indexOf('  let loginModalEmail'), html.indexOf('  function renderQuota('));
const importScript = html.slice(html.indexOf('  async function importAccounts('), html.indexOf('  function syncThemeButton('));
class Element {
  constructor() {
    this._text = ''; this.style = {}; this.children = []; this.listeners = {}; this.value = '';
    const classes = new Set();
    this.classList = { add: x => classes.add(x), remove: x => classes.delete(x), contains: x => classes.has(x) };
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
function harness(sessions) {
  const nodes = new Map(), calls = [];
  const $ = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const context = vm.createContext({
    $, el: (tag, text) => { const e = new Element(); e.textContent = text; return e; },
    refresh: async () => {},
    api: async (url, options) => {
      const body = options && JSON.parse(options.body);
      calls.push({ url, body });
      if (url === '/api/login/status') return { sessions, concurrency: 2 };
      if (url.startsWith('/api/login/session/')) return { session: sessions.find(s => url.endsWith(s.id)) };
      if (url === '/api/accounts/import') return { inserted: sessions.length, updated: 0,
        login_auto: true, login_queued: sessions.map(s => s.email),
        login_tasks: sessions.map(session => ({ email: session.email, state: 'queued', session })) };
      if (url === '/api/accounts/login') return { login: { state: 'queued', session: sessions[0] } };
      if (url === '/api/login/cancel') { sessions.find(s => s.id === body.session_id).status = 'cancelled'; return { cancelled: true }; }
      throw Error(url);
    },
  });
  vm.runInContext(loginScript + importScript + `\nglobalThis.ui = { openLoginModal, openImportedLogins, importAccounts,
    startLoginFor, cancelLogin, refreshLoginModal, state: () => ({ loginSessionId, loginModalEmail }) };`, context);
  return { ui: context.ui, $, calls };
}
const session = (id, status = 'queued') => ({ id, email: id + '@example.com', status, phase: 'queued', message: '', source: 'import' });
const settle = () => new Promise(resolve => setImmediate(resolve));

test('single-account import opens progress and attaches to its existing job', async () => {
  const h = harness([session('one')]);
  await h.ui.importAccounts(); await settle();
  assert.equal(h.$('#loginModal').classList.contains('open'), true);
  assert.equal(h.ui.state().loginSessionId, 'one');
  assert.match(h.$('#importMsg').textContent, /账号已保存/);
  assert.equal(h.calls.filter(call => call.body).length, 1);
});

test('batch import shows every account and viewing a row never starts a login', async () => {
  const h = harness([session('one'), session('two')]);
  await h.ui.importAccounts(); await settle();
  assert.equal(h.$('#loginBatchList').hidden, false);
  assert.equal(h.$('#loginBatchList').children.length, 2);
  h.$('#loginBatchList').children[1].listeners.click(); await settle();
  assert.equal(h.ui.state().loginModalEmail, 'two@example.com');
  assert.equal(h.calls.filter(call => call.url === '/api/accounts/login').length, 0);
});

test('viewing a failed login only reads it; retry is a separate explicit action', async () => {
  const failed = { ...session('one', 'failed'), phase: 'generating_device_code', message: 'fixture failure' };
  const h = harness([failed]);
  h.ui.openLoginModal(failed.email); await settle();
  assert.match(h.$('#loginModalMsg').textContent, /账号已保存，本次登录失败/);
  assert.equal(h.calls.some(call => call.body), false);
  await h.ui.startLoginFor(failed.email);
  assert.equal(h.calls.filter(call => call.url === '/api/accounts/login').length, 1);
});

test('a queued import can be cancelled using its stable task id', async () => {
  const h = harness([session('one')]);
  h.ui.openImportedLogins([{ email: 'one@example.com', session: session('one') }]); await settle();
  assert.equal(h.$('#loginModalCancel').style.display, '');
  await h.ui.cancelLogin();
  assert.deepEqual(h.calls.find(call => call.url === '/api/login/cancel').body, { session_id: 'one' });
  assert.match(h.$('#loginModalMsg').textContent, /登录已取消/);
});
