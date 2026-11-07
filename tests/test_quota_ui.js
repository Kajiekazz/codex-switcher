const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Render the real dashboard functions using synthetic quotas and DOM doubles.
// No network calls or credit redemption can occur in this harness.
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));
const script = extract('  function poolQuotaFor(', '  function poolQuotaLine(') +
  extract('  async function useResetCredit(', '  function renderAccounts(') +
  extract('  function renderAccounts(', '  async function saveAccountOrder(') +
  extract('  function renderQuota(', '  // Why the auto-reset gate') +
  extract('  $("#quotaResetBtn").addEventListener(', '  $("#importBtn").addEventListener(');

class Element {
  constructor(tag = 'div', text = '', className = '') {
    this.tagName = tag;
    this._text = text || '';
    this.className = className || '';
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
    this.classList = { add() {}, remove() {} };
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}

function harness() {
  const elements = new Map();
  const calls = [];
  let resetResponse = { ok: true, result: { windows_reset: 2 } };
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const ctx = vm.createContext({
    $, poolQuotas: {}, currentEmail: 'test@example.com', snapshots: [], draggingEmail: null,
    lastAccounts: [{ email: 'test@example.com', has_auth_snapshot: true }],
    el: (tag, text, cls) => new Element(tag, text, cls),
    pill: (text, tone) => new Element('span', text, tone),
    empty: text => new Element('div', text), initials: () => 'T', statusTone: () => '',
    poolQuotaLine: () => new Element(),
    kv: (box, label, value) => box.appendChild(new Element('div', label + ':' + value)),
    setMeter() {}, setStat() {}, formatReset: () => '', formatAge: () => '',
    formatDate: () => '', formatDuration: () => '',
    confirm: () => true,
    api: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return resetResponse;
    },
    async refreshPoolQuotas() {}, refresh() {}, refreshTrend() {},
  });
  vm.runInContext(script + '\nglobalThis.ui = {renderAccounts, renderQuota, poolCredits};', ctx);
  return {
    $, ui: ctx.ui, calls,
    assign(values) { Object.assign(ctx, values); },
    setResetResponse(value) { resetResponse = value; },
    setCurrentEmail(value) { ctx.currentEmail = value; },
    render(quota) {
      ctx.poolQuotas = { 'test@example.com': quota };
      ctx.ui.renderAccounts([{ email: 'test@example.com', has_auth_snapshot: true }]);
      ctx.ui.renderQuota(quota);
    },
    account() { return $('#accountsWrap').children[0]; },
  };
}

const quota = reset_credits => ({ ok: true, email: 'test@example.com', reset_credits });
const descendants = node => [node, ...node.children.flatMap(descendants)];
const resetAction = h => descendants(h.account()).find(node => node.tagName === 'button' && node.textContent === '用券');

test('same-email workspaces show independent current identity and redeem only the selected profile', async () => {
  const h = harness();
  const personal = { email: 'test@example.com', profile_id: 'profile_personal', plan: 'plus', priority: 100 };
  const team = { email: 'test@example.com', profile_id: 'profile_team', workspace_name: 'Example Team', plan: 'team', priority: 100 };
  h.assign({ currentProfileId: personal.profile_id, poolQuotas: {
    profile_personal: quota({ known: true, available_count: 0 }),
    profile_team: quota({ known: true, available_count: 2 }),
  } });
  h.ui.renderAccounts([{ email: personal.email, has_auth_snapshot: true, profiles: [personal, team] }]);
  const sections = descendants(h.account()).filter(n => n.className === 'workspace-entry');
  assert.equal(sections.length, 2);
  assert.equal(descendants(sections[0]).find(n => n.tagName === 'button' && n.textContent === '用券').disabled, true);
  assert.match(sections[0].textContent, /当前工作区/);
  assert.doesNotMatch(sections[1].textContent, /当前工作区/);
  const button = descendants(sections[1]).find(n => n.tagName === 'button' && n.textContent === '用券');
  assert.equal(button.disabled, false);
  await button.listeners.click();
  assert.equal(h.calls.find(c => c.url === '/api/quota/reset').body.profile_id, team.profile_id);
});

test('current quota card binds redemption to the rendered workspace', async () => {
  const h = harness();
  h.ui.renderQuota({ ...quota({ known: true, available_count: 2 }), profile_id: 'profile_team' });
  await h.$('#quotaResetBtn').listeners.click();
  assert.equal(h.calls.find(c => c.url === '/api/quota/reset').body.profile_id, 'profile_team');
});

test('owned credits remain visible when none currently apply in both views', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 1, applicable_available_count: 0 }));
  assert.equal(h.ui.poolCredits('TEST@example.com'), 1);
  assert.match(h.account().textContent, /剩余 1 张券/);
  const chip = descendants(h.account()).find(node => node.textContent === '剩余 1 张券');
  assert.match(chip.title, /剩余 1 张重置券 · 当前适用 0 张/);
  assert.match(h.$('#quotaBox').textContent, /剩余 1 张 · 当前适用 0 张/);
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('Pro explicitly missing a primary limit is not shown as five-hour quota', () => {
  const h = harness();
  h.render({ ok: true, plan: 'pro', allowed: true, primary_window: null,
    secondary_window: { used_percent: 80, window_seconds: 604800 },
    window_states: { primary_window: 'not_applicable' } });
  assert.equal(h.$('#quotaPrimaryName').textContent, '主窗口');
  assert.equal(h.$('#quota5hLabel').textContent, '不适用');
  assert.equal(h.$('#quotaSecondaryName').textContent, '7 天窗口');
  assert.match(h.$('#quota7dLabel').textContent, /已用 80%/);
});

test('Free custom windows and unknown usage do not become full quota', () => {
  const h = harness();
  h.render({ ok: true, plan: 'free', primary_window: { window_seconds: 10800, used_percent: null } });
  assert.equal(h.$('#quotaPrimaryName').textContent, '3 小时窗口');
  assert.equal(h.$('#quota5hLabel').textContent, '未知');
  assert.match(h.$('#quotaBox').textContent, /未知，等待接口确认/);
  h.render({ ok: true, allowed: false, limit_reached: false });
  assert.match(h.$('#quotaBox').textContent, /已用尽/);
});

test('applicable credits enable both manual reset controls', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 2, applicable_available_count: 1 }));
  assert.match(h.account().textContent, /剩余 2 张券/);
  assert.match(h.$('#quotaBox').textContent, /剩余 2 张 · 当前适用 1 张/);
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('zero owned credits are visible and block both manual controls', async () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 0, applicable_available_count: 0 }));
  assert.match(h.account().textContent, /剩余 0 张券/);
  assert.equal(resetAction(h).disabled, true);
  assert.match(resetAction(h).title, /没有可用的重置券/);
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, true);
  assert.match(h.$('#quotaBox').textContent, /剩余 0 张 · 当前适用 0 张/);
  await resetAction(h).listeners.click();
  await h.$('#quotaResetBtn').listeners.click();
  assert.equal(h.calls.length, 0);
});

test('missing applicability stays unknown and does not erase known inventory', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 2 }));
  assert.match(h.account().textContent, /剩余 2 张券/);
  assert.match(h.$('#quotaBox').textContent, /剩余 2 张 · 当前适用数量未知/);
});

test('unknown information is never displayed as zero owned credits', () => {
  const h = harness();
  h.render(quota({ known: false, available_count: 0, applicable_available_count: 0 }));
  assert.equal(h.ui.poolCredits('test@example.com'), null);
  assert.match(h.account().textContent, /券数未知/);
  assert.doesNotMatch(h.account().textContent, /剩余 0 张券/);
  assert.equal(resetAction(h).disabled, false);
  assert.match(h.$('#quotaBox').textContent, /重置券:未知/);
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('a failed quota probe keeps manual reset available for the current account', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 1, applicable_available_count: 1 }));
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  h.setCurrentEmail('other@example.com');
  h.ui.renderQuota({ ok: false, error: 'request_failed' });
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
  assert.equal(h.$('#quotaResetBtn').dataset.email, 'other@example.com');
});

test('both manual controls submit even with zero applicable credits', async () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 1, applicable_available_count: 0 }));
  await resetAction(h).listeners.click();
  await h.$('#quotaResetBtn').listeners.click();
  assert.equal(h.calls.length, 2);
  for (const call of h.calls) {
    assert.equal(call.url, '/api/quota/reset');
    assert.equal(call.body.email, 'test@example.com');
    assert.equal(call.body.confirm, true);
  }
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('upstream rejection is displayed without leaving manual reset disabled', async () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 1, applicable_available_count: 0 }));
  h.setResetResponse({ ok: false, result: { error: 'nothing_to_reset', message: '当前无需重置' } });
  await h.$('#quotaResetBtn').listeners.click();
  assert.match(h.$('#quotaMsg').textContent, /当前无需重置/);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

for (const source of ['account pool', 'current account']) {
  test(`using the last credit from ${source} updates both views and keeps both controls disabled`, async () => {
    const h = harness();
    h.render(quota({ known: true, available_count: 1, applicable_available_count: 1 }));
    h.setResetResponse({ ok: true, result: { windows_reset: 2 },
      quota: quota({ known: true, available_count: 0, applicable_available_count: 0 }) });
    const button = source === 'account pool' ? resetAction(h) : h.$('#quotaResetBtn');
    await button.listeners.click();
    assert.equal(h.calls.length, 1);
    assert.match(h.account().textContent, /剩余 0 张券/);
    assert.equal(resetAction(h).disabled, true);
    assert.equal(h.$('#quotaResetBtn').disabled, true);
    await resetAction(h).listeners.click();
    await h.$('#quotaResetBtn').listeners.click();
    assert.equal(h.calls.length, 1);
  });
}

test('refreshing replenished credits enables both controls again', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 0, applicable_available_count: 0 }));
  assert.equal(resetAction(h).disabled, true);
  h.render(quota({ known: true, available_count: 2, applicable_available_count: 0 }));
  assert.match(h.account().textContent, /剩余 2 张券/);
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});
