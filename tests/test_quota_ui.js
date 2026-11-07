const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Render the real dashboard functions using synthetic quotas and DOM doubles.
// No network calls or credit redemption can occur in this harness.
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const extract = (start, end) => html.slice(html.indexOf(start), html.indexOf(end));
const script = extract('  function accountStatusLabel(', '  function statusTone(') +
  extract('  function renderAuth(', '  function renderPool(') +
  extract('  function poolQuotaFor(', '  function poolQuotaLine(') +
  extract('  function poolQuotaLine(', '  async function refreshPoolQuotas(') +
  extract('  async function useResetCredit(', '  function renderAccounts(') +
  extract('  function renderAccounts(', '  async function saveAccountOrder(') +
  extract('  function renderSnapshots(', '  async function refreshSnapshots(') +
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
  get options() { return this.children; }
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
    $, poolQuotas: {}, currentEmail: 'test@example.com', snapshots: [], draggingEmail: null, switchState: {},
    lastAccounts: [{ email: 'test@example.com', has_auth_snapshot: true }],
    el: (tag, text, cls) => new Element(tag, text, cls),
    pill: (text, tone) => new Element('span', text, tone),
    empty: text => new Element('div', text), initials: () => 'T', statusTone: () => '',
    usedPercent: window => window && typeof window.used_percent === 'number' ? window.used_percent : null,
    kv: (box, label, value) => box.appendChild(new Element('div', label + ':' + value)),
    setMeter() {}, setStat(selector, value, _tone, hint) {
      $(selector).textContent = value;
      if (hint !== undefined) $(selector + 'Hint').textContent = hint;
    }, formatReset: () => '', formatAge: () => '',
    formatDate: () => '', formatDuration: () => '',
    confirm: () => true,
    Option: function (text, value) { const option = new Element('option', text); option.value = value; return option; },
    api: async (url, options) => {
      calls.push({ url, body: JSON.parse(options.body) });
      return resetResponse;
    },
    async refreshPoolQuotas() {}, refresh() {}, refreshTrend() {},
  });
  vm.runInContext(script + '\nglobalThis.ui = {renderAuth, renderSwitch, renderAccounts, renderQuota, poolCredits, poolQuotaLine, planLabel};', ctx);
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

test('Business workspace labels honor the server identity and support older services', () => {
  for (const profile of [
    { plan: 'self_serve_business_prolite' },
    { plan: 'future_workspace_plan', identity_type: 'team' },
  ]) {
    const h = harness();
    h.ui.renderAccounts([{ email: 'test@example.com', has_auth_snapshot: true, profiles: [
      { email: 'test@example.com', profile_id: 'profile_selected', ...profile },
    ] }]);
    assert.match(h.account().textContent, /Team 工作区/);
    assert.doesNotMatch(h.account().textContent, /个人空间/);
  }
});

for (const [raw, label] of [
  ['self_serve_business_prolite', 'Business（ProLite）'],
  ['self_serve_business_usage_based', 'Business（按量）'],
  ['pro', 'Pro (More)'], ['prolite', 'Pro'], ['promax', 'Pro (Max)'],
  ['future_workspace_plan', 'future_workspace_plan'],
]) {
  test(`plan ${raw} has a consistent display label without changing its underlying identity`, () => {
    const h = harness();
    const profile = { email: 'test@example.com', profile_id: 'profile_selected', plan: raw };
    const data = { ...quota({ known: true, available_count: 1 }), ...profile, primary_window: { used_percent: 35 } };
    h.ui.renderAuth({ ...profile, logged_in: true });
    h.render(data);
    h.ui.renderSwitch({ snapshots: [profile] });
    for (const selector of ['#authBox', '#statCurrentHint', '#accountsWrap', '#quotaBox', '#snapshotsWrap', '#switchTarget']) {
      assert.ok(h.$(selector).textContent.includes(label), `${selector} should show ${label}`);
      if (raw !== label) assert.equal(h.$(selector).textContent.includes(raw), false, `${selector} leaked the raw plan`);
    }
    assert.equal(profile.plan, raw);
    assert.equal(data.plan, raw);
    assert.equal(h.ui.planLabel(raw), label);
  });
}

test('plan labels tolerate absent and mixed-case values while preserving unknown plans', () => {
  const h = harness();
  for (const value of [undefined, null, '', false, 42]) assert.equal(h.ui.planLabel(value), '—');
  assert.equal(h.ui.planLabel(' SELF_SERVE_BUSINESS_PROLITE '), 'Business（ProLite）');
  assert.equal(h.ui.planLabel('future_business_beta'), 'future_business_beta');
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

test('cold pending quotas show loading instead of an unavailable or empty allowance', () => {
  const h = harness();
  h.render({ ok: false, pending: true, error: 'quota_pending', message: '正在刷新额度' });
  assert.match(h.account().textContent, /正在刷新/);
  assert.doesNotMatch(h.account().textContent, /额度：不可用/);
  assert.match(h.$('#quotaBox').textContent, /正在刷新/);
  assert.doesNotMatch(h.$('#quotaBox').textContent, /状态:不可用/);
  assert.equal(h.$('#quota5hLabel').textContent, '—');
});

test('pending refresh keeps usage, coupon inventory and original sampling age visible', () => {
  const h = harness();
  h.assign({ formatAge: () => '2 分钟前', formatDate: value => value });
  h.render({ ...quota({ known: true, available_count: 2 }), pending: true,
    primary_window: { used_percent: 35 }, fetched_at: '2026-01-01T00:00:00Z' });
  assert.match(h.account().textContent, /已用 35%.*2 分钟前.*正在刷新/);
  assert.match(h.account().textContent, /剩余 2 张券/);
  assert.match(h.$('#quotaBox').textContent, /2026-01-01T00:00:00Z · 2 分钟前/);
  assert.match(h.$('#quotaBox').textContent, /正在后台更新，显示上次数据/);
});

test('refresh failure is distinguished from unavailable quota while preserving known values', () => {
  const h = harness();
  h.render({ ...quota({ known: true, available_count: 0 }), stale: true,
    primary_window: { used_percent: 60 }, refresh_error: { message: 'network down' } });
  assert.match(h.account().textContent, /已用 60%.*刷新失败，保留上次数据/);
  assert.match(h.$('#quotaBox').textContent, /刷新失败，显示上次数据/);
  assert.match(h.$('#quota5hLabel').textContent, /已用 60%/);
  assert.equal(h.$('#quotaResetBtn').disabled, true);
});

test('rejected single-workspace authorization stays visible with a targeted relogin action', async () => {
  const h = harness();
  const profile = { email: 'test@example.com', profile_id: 'profile_personal', plan: 'plus' };
  const logins = [];
  h.assign({ poolQuotas: { profile_personal: { ...quota({ known: true, available_count: 2 }),
    primary_window: { used_percent: 35 }, fetched_at: '2026-01-01T00:00:00Z', retry_after_seconds: 300,
    refresh_error: { error: 'auth_error', status_code: 401 } } },
    openLoginModal: (email, options) => logins.push({ email, options }) });
  h.ui.renderAccounts([{ email: profile.email, has_auth_snapshot: true, profiles: [profile] }]);
  assert.match(h.account().textContent, /已用 35%.*授权被拒绝（HTTP 401）.*请重新登录或检查工作区权限.*300 秒后自动重试/);
  assert.match(h.account().textContent, /授权待更新/);
  assert.doesNotMatch(h.account().textContent, /可切换/);
  const login = descendants(h.account()).find(n => n.tagName === 'button' && n.textContent === '重新登录');
  assert.match(login.className, /primary/);
  await login.listeners.click();
  assert.equal(logins[0].email, profile.email);
  assert.equal(logins[0].options.profile.profile_id, profile.profile_id);
});

test('one rejected workspace does not mark the other same-email workspace as invalid', async () => {
  const h = harness();
  const personal = { email: 'test@example.com', profile_id: 'profile_personal', plan: 'plus' };
  const team = { email: 'test@example.com', profile_id: 'profile_team', plan: 'team' };
  const logins = [];
  h.assign({ poolQuotas: {
    profile_personal: quota({ known: true, available_count: 2 }),
    profile_team: { ok: false, pending: true, error: 'auth_error', status_code: 403 },
  }, openLoginModal: (email, options) => logins.push({ email, options }) });
  h.ui.renderAccounts([{ email: personal.email, has_auth_snapshot: true, profiles: [personal, team] }]);
  const sections = descendants(h.account()).filter(n => n.className === 'workspace-entry');
  assert.doesNotMatch(sections[0].textContent, /授权待更新|授权被拒绝|封禁|删除/);
  assert.match(sections[1].textContent, /授权待更新.*授权被拒绝（HTTP 403）.*正在重试/);
  assert.doesNotMatch(sections[1].textContent, /封禁|账号已删除/);
  const login = descendants(sections[1]).find(n => n.tagName === 'button' && n.textContent === '重新登录');
  assert.match(login.className, /primary/);
  await login.listeners.click();
  assert.equal(logins[0].options.profile.profile_id, team.profile_id);
});

test('temporary quota errors show the reason and retry delay in the account row', () => {
  const h = harness();
  h.render({ ...quota({ known: true, available_count: 1 }), primary_window: { used_percent: 35 },
    retry_after_seconds: 30, refresh_error: { error: 'request_failed', message: '连接超时' } });
  assert.match(h.account().textContent, /已用 35%.*网络请求失败：连接超时.*30 秒后自动重试/);
  assert.doesNotMatch(h.account().textContent, /授权待更新|重新登录/);
});
