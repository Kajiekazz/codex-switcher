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
      if (url === '/api/accounts/totp/setup') return { login: { state: 'queued', session: { ...sessions[0], purpose: 'totp_setup' } } };
      if (url === '/api/settings' || url === '/api/accounts/totp/import' || url === '/api/login/manual') return { ok: true };
      if (url === '/api/login/cancel') { sessions.find(s => s.id === body.session_id).status = 'cancelled'; return { cancelled: true }; }
      if (url === '/api/login/code') {
        const s = sessions.find(s => s.id === body.session_id);
        s.challenge.submitting = true;
        return { ok: true, accepted: true, session: s };
      }
      throw Error(url);
    },
  });
  vm.runInContext(loginScript + importScript + `\nglobalThis.ui = { openLoginModal, openImportedLogins, importAccounts,
    startLoginFor, cancelLogin, refreshLoginModal, saveLoginTotp, takeOverLogin, submitLoginCode, closeLoginModal, state: () => ({ loginSessionId, loginModalEmail, loginPurpose }) };`, context);
  return { ui: context.ui, $, calls };
}
const session = (id, status = 'queued') => ({ id, email: id + '@example.com', status, phase: 'queued', message: '', source: 'import' });
const settle = () => new Promise(resolve => setImmediate(resolve));

test('identity selection does not attach an older personal login or start before selection', async () => {
  const s = session('one', 'success');
  const h = harness([s]);
  h.ui.openLoginModal(s.email, { chooseIdentity: true, identity_type: 'team' });
  await settle();
  await h.ui.refreshLoginModal();
  assert.equal(h.ui.state().loginSessionId, null);
  assert.equal(h.calls.some(call => call.url === '/api/accounts/login'), false);
  assert.equal(h.$('#loginIdentity').value, 'team');
  await h.ui.startLoginFor(s.email);
  assert.equal(h.calls.find(call => call.url === '/api/accounts/login').body.identity_type, 'team');
});

test('reauthorization keeps the exact Team profile while a new workspace has no target id', async () => {
  const s = session('one');
  const h = harness([s]);
  h.ui.openLoginModal(s.email, { chooseIdentity: true, profile: { profile_id: 'profile_team', plan: 'team' } });
  await h.ui.startLoginFor(s.email);
  assert.equal(h.calls.find(call => call.url === '/api/accounts/login').body.profile_id, 'profile_team');
  h.ui.openLoginModal(s.email, { chooseIdentity: true, identity_type: 'team' });
  await h.ui.startLoginFor(s.email);
  const calls = h.calls.filter(call => call.url === '/api/accounts/login');
  assert.equal(calls[1].body.profile_id, undefined);
});

test('viewing an in-progress new workspace follows its session id despite a saved personal profile', async () => {
  const personal = { ...session('one', 'success'), profile_id: 'profile_personal' };
  const team = { ...session('two', 'waiting_for_input'), email: personal.email, identity_type: 'team', profile_id: null };
  const h = harness([personal, team]);
  h.ui.openLoginModal(personal.email, { sessionId: team.id });
  await settle();
  assert.equal(h.ui.state().loginSessionId, team.id);
  assert.equal(h.$('#loginIdentity').value, 'team');
  assert.equal(h.calls.some(call => call.url === '/api/accounts/login'), false);
});

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

test('missing verification is an active manual wait, not a failed login', async () => {
  const s = { ...session('one', 'waiting_for_input'), phase: 'manual_verification', needs_input: true, driver: 'playwright' };
  const h = harness([s]);
  h.ui.openLoginModal(s.email, { session: s }); await settle();
  assert.match(h.$('#loginModalStatus').textContent, /等待手动验证/);
  assert.equal(h.$('#loginModalCancel').style.display, '');
  assert.equal(h.$('#loginModalRetry').style.display, 'none');
});

test('TOTP configuration does not attach an older ordinary login or start without a click', async () => {
  const h = harness([session('one', 'success')]);
  h.ui.openLoginModal('one@example.com', { purpose: 'totp_setup' }); await settle();
  assert.equal(h.ui.state().loginSessionId, null);
  assert.equal(h.$('#loginTotpFields').hidden, false);
  assert.equal(h.$('#loginModalRetry').textContent, '一键开启并保存');
  assert.equal(h.calls.some(call => call.body), false);
  await h.ui.startLoginFor('one@example.com');
  assert.deepEqual(h.calls.find(call => call.url === '/api/accounts/totp/setup').body, { email: 'one@example.com', confirm: true });
  assert.equal(h.calls.some(call => call.url === '/api/accounts/login'), false);
});

test('manual key fallback saves through the dedicated endpoint and clears the field', async () => {
  const h = harness([]);
  h.ui.openLoginModal('one@example.com', { purpose: 'totp_setup' }); await settle();
  h.$('#loginTotpSecret').value = 'JBSWY3DPEHPK3PXP';
  await h.ui.saveLoginTotp();
  assert.deepEqual(h.calls.find(call => call.url === '/api/accounts/totp/import').body,
    { confirm: true, email: 'one@example.com', totp: 'JBSWY3DPEHPK3PXP' });
  assert.equal(h.$('#loginTotpSecret').value, '');
  assert.match(h.$('#loginModalMsg').textContent, /加密保存/);
});

test('email code is submitted for the exact task and step, then remains verifying', async () => {
  const s = { ...session('one', 'waiting_for_input'), driver: 'playwright', needs_input: true,
    challenge: { id: 'step-1', kind: 'email', length: 6, submitting: false } };
  const h = harness([s]);
  h.ui.openLoginModal(s.email, { session: s }); await settle();
  assert.equal(h.$('#loginVerification').hidden, false);
  assert.match(h.$('#loginVerificationLabel').textContent, /邮箱验证码/);
  h.$('#loginVerificationCode').value = '123456';
  await h.ui.submitLoginCode();
  assert.deepEqual(h.calls.find(c => c.url === '/api/login/code').body,
    { confirm: true, session_id: 'one', challenge_id: 'step-1', email: s.email, code: '123456' });
  assert.equal(h.$('#loginVerificationCode').value, '');
  assert.equal(h.$('#loginVerificationSubmit').disabled, true);
  assert.match(h.$('#loginVerificationMsg').textContent, /正在验证/);
  assert.equal(h.calls.some(c => c.url === '/api/accounts/totp/import'), false);
});

test('invalid codes stay local; changing accounts clears input; closing does not cancel', async () => {
  const s = { ...session('one', 'waiting_for_input'), challenge: { id: 'auth-1', kind: 'authenticator', length: 6 } };
  const h = harness([s, session('two')]);
  h.ui.openLoginModal(s.email, { session: s }); await settle();
  h.$('#loginVerificationCode').value = '123';
  await h.ui.submitLoginCode();
  assert.equal(h.calls.some(c => c.url === '/api/login/code'), false);
  h.ui.openLoginModal('two@example.com', { session: session('two') }); await settle();
  assert.equal(h.$('#loginVerificationCode').value, '');
  assert.equal(h.$('#loginVerification').hidden, true);
  h.ui.closeLoginModal();
  assert.equal(h.calls.some(c => c.url === '/api/login/cancel'), false);
});
