const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const extract = (start, end) => {
  const from = html.indexOf(start), to = html.indexOf(end, from);
  assert.ok(from >= 0 && to > from, `missing source markers: ${start}`);
  return html.slice(from, to);
};
const apiSource = extract('  const pendingReads =', '  function el(');
const dashboardSource = extract('  function refresh() {', '  async function importAccounts(');
const quotaSource = extract('  async function refreshQuota(', '  let goalState =');
const poolSource = extract('  async function refreshPoolQuotas(', '  async function useResetCredit(');
const snapshotsSource = extract('  async function refreshSnapshots(', '  function renderDesktop(');
const desktopSource = extract('  async function refreshDesktop(', '  async function restartDesktop(');
const trendSource = extract('  async function refreshTrend(', '  function openAccountModal(');
const pollSource = extract('  function pollDashboard(', '  document.addEventListener("visibilitychange"');
const defer = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const response = (payload, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => payload });
const flush = async () => { for (let i = 0; i < 24; i++) await Promise.resolve(); };

function harness(route = () => undefined) {
  const elements = new Map(), calls = [], paints = [], timers = new Map();
  let nextTimer = 0;
  const defaults = {
    '/api/status': { version: 'test', auth: { logged_in: true, email: 'a@example.com', profile_id: 'profile_a' } },
    '/api/accounts': { accounts: [{ email: 'a@example.com', profile_id: 'profile_a' }] },
    '/api/snapshots': { snapshots: [] },
    '/api/quota?background=1': { quota: { ok: true, email: 'a@example.com', profile_id: 'profile_a', used: 20 } },
    '/api/accounts/quotas?background=1': { quotas: {}, pending: [] },
    '/api/auto-switch/status': { auto_switch: { running: true } },
    '/api/desktop/status?background=1': { desktop: { available: true, running: true }, pending: false },
    '/api/quota/history?hours=6': { points: [] },
  };
  const ctx = vm.createContext({
    AbortController,
    setTimeout(fn, ms) { const id = ++nextTimer; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      calls.push({ url, options });
      const custom = route(url, options);
      return custom === undefined ? Promise.resolve(response(defaults[url] || {})) : custom;
    },
    $: selector => {
      if (!elements.has(selector)) elements.set(selector, { textContent: '', className: '', title: '' });
      return elements.get(selector);
    },
    document: { hidden: false },
    currentProfileId: 'profile_a', currentEmail: 'a@example.com', authLoaded: true,
    lastCurrentQuota: null, lastAccounts: [], poolQuotas: {}, lastDesktop: null,
    trendFetchedAt: 0, loginConcurrency: 1,
    setConn: online => paints.push(['connection', online]),
    renderAuth(auth) {
      ctx.currentProfileId = auth && auth.logged_in ? auth.profile_id || '' : '';
      ctx.currentEmail = auth && auth.logged_in ? auth.email || '' : '';
      paints.push(['auth', auth]);
    },
    renderQuota(quota) { if (quota.ok) ctx.lastCurrentQuota = quota; paints.push(['quota', quota]); },
    renderAccounts: value => paints.push(['accounts', value]),
    renderAutoSwitch: value => paints.push(['auto', value]),
    renderSwitch: value => paints.push(['snapshots', value]),
    renderDesktop: value => paints.push(['desktop', value]),
    renderTrend: value => paints.push(['trend', value]),
    renderPool() {}, renderSwitchToggles() {}, renderSwitchOrder() {},
    safeCall(fn, ...args) { return fn(...args); },
    refreshSms: async () => {}, refreshGoals: async () => {}, refreshLoginModal: async () => {},
  });
  vm.runInContext(apiSource + dashboardSource + quotaSource + poolSource + snapshotsSource + desktopSource + trendSource + pollSource, ctx);
  return {
    ctx, calls, paints, timers, elements,
    async fire(ms) {
      const due = Array.from(timers).filter(([, value]) => value.ms === ms);
      due.forEach(([id, value]) => { timers.delete(id); value.fn(); });
      await flush();
    },
    notice(selector) { return ctx.$(selector).textContent; },
    values(name) { return paints.filter(row => row[0] === name).map(row => row[1]); },
  };
}

test('concurrent identical GETs share the request only while pending', async () => {
  const first = defer(); let calls = 0;
  const h = harness(url => url === '/test' ? (++calls === 1 ? first.promise : Promise.resolve(response({ n: calls }))) : undefined);
  const a = h.ctx.api('/test'), b = h.ctx.api('/test');
  assert.equal(calls, 1);
  first.resolve(response({ n: 1 }));
  assert.equal((await a).n, 1); assert.equal((await b).n, 1);
  assert.equal((await h.ctx.api('/test')).n, 2);
  assert.equal(h.timers.size, 0);
});

test('mutation prevents reuse of earlier GETs and old completion cannot release a newer read', async () => {
  const old = defer(), fresh = defer(); let gets = 0;
  const h = harness((url, options) => url === '/test' && options.method !== 'POST'
    ? (++gets === 1 ? old.promise : fresh.promise) : undefined);
  const a = h.ctx.api('/test');
  await h.ctx.api('/test', { method: 'POST' });
  const b = h.ctx.api('/test');
  old.resolve(response({ version: 'old' })); await a;
  const c = h.ctx.api('/test'); assert.equal(gets, 2);
  fresh.resolve(response({ version: 'fresh' }));
  assert.equal((await b).version, 'fresh'); assert.equal((await c).version, 'fresh');
});

test('failed GET releases its pending entry so retry succeeds', async () => {
  let calls = 0;
  const h = harness(url => url === '/test' ? Promise.resolve(++calls === 1 ? response({ message: 'busy' }, false) : response({ ok: true })) : undefined);
  await assert.rejects(h.ctx.api('/test'), /busy/);
  assert.equal((await h.ctx.api('/test')).ok, true);
});

test('a hung GET is aborted, released and retriable even if fetch ignores abort', async () => {
  let calls = 0;
  const h = harness(url => url === '/test' ? (++calls === 1 ? new Promise(() => {}) : Promise.resolve(response({ ok: true }))) : undefined);
  const pending = h.ctx.api('/test'), rejection = assert.rejects(pending, /超时/);
  await h.fire(12000); await rejection;
  assert.equal(h.calls[0].options.signal.aborted, true);
  assert.equal((await h.ctx.api('/test')).ok, true);
  assert.equal(h.timers.size, 0);
});

test('GET timeout also covers a hanging body; mutations have no client deadline', async () => {
  const mutation = defer();
  const h = harness((url, options) => options.method === 'POST' ? mutation.promise
    : url === '/test' ? Promise.resolve({ ok: true, json: () => new Promise(() => {}) }) : undefined);
  const get = h.ctx.api('/test'), rejection = assert.rejects(get, /超时/);
  const post = h.ctx.api('/operation', { method: 'POST' });
  await h.fire(12000); await rejection;
  assert.equal(h.calls.find(call => call.url === '/operation').options.signal, undefined);
  mutation.resolve(response({ ok: true })); assert.equal((await post).ok, true);
});

test('fast dashboard modules paint before a slow snapshot, and keep polling independently', async () => {
  const snapshots = defer();
  const h = harness(url => url === '/api/snapshots' ? snapshots.promise : undefined);
  const first = h.ctx.refresh(); await flush();
  assert.equal(h.values('auth').length, 1); assert.ok(h.values('accounts').some(list => list.length === 1));
  assert.equal(h.values('quota').length, 1); assert.equal(h.values('snapshots').length, 0);
  const second = h.ctx.refresh(); await flush();
  assert.equal(h.values('auth').length, 2);
  assert.equal(h.calls.filter(call => call.url === '/api/snapshots').length, 1);
  snapshots.resolve(response({ snapshots: ['ready'] }));
  await Promise.all([first, second]); assert.equal(h.values('snapshots').length, 1);
});

test('auto-switch failure cannot erase a successful quota or mark the whole dashboard offline', async () => {
  const h = harness(url => url === '/api/auto-switch/status' ? Promise.reject(new Error('auto unavailable')) : undefined);
  await h.ctx.refresh();
  assert.equal(h.values('quota').at(-1).used, 20);
  assert.deepEqual(h.values('connection'), [true]);
  assert.match(h.notice('#autoNotice'), /auto unavailable/); assert.equal(h.notice('#quotaMsg'), '');
});

test('account-list failure keeps the last list while other modules update', async () => {
  let fail = false;
  const h = harness(url => fail && url === '/api/accounts' ? Promise.reject(new Error('accounts unavailable')) : undefined);
  await h.ctx.refresh(); const list = h.ctx.lastAccounts;
  fail = true; await h.ctx.refresh();
  assert.equal(h.ctx.lastAccounts, list); assert.equal(h.values('quota').length, 2);
  assert.deepEqual(h.values('connection'), [true, true]);
  assert.match(h.notice('#accountsNotice'), /已有数据保留/);
});

test('mutation discards an older account response even when the new response arrives first', async () => {
  const old = defer(); let reads = 0;
  const h = harness(url => url === '/api/accounts' ? (++reads === 1 ? old.promise : Promise.resolve(response({ accounts: [{ email: 'new@example.com' }] }))) : undefined);
  const first = h.ctx.refreshAccounts(); await flush();
  await h.ctx.api('/operation', { method: 'POST' }); await h.ctx.refreshAccounts();
  old.resolve(response({ accounts: [{ email: 'old@example.com' }] })); await first;
  assert.equal(h.ctx.lastAccounts[0].email, 'new@example.com'); assert.equal(h.values('accounts').length, 1);
});

test('an external workspace switch discards the old quota and rereads without reusing its request', async () => {
  const old = defer(); let reads = 0;
  const h = harness(url => {
    if (url === '/api/status') return Promise.resolve(response({ auth: { logged_in: true, profile_id: 'profile_b', email: 'a@example.com' } }));
    if (url === '/api/quota?background=1') return ++reads === 1 ? old.promise
      : Promise.resolve(response({ quota: { ok: true, profile_id: 'profile_b', email: 'a@example.com', used: 80 } }));
  });
  const first = h.ctx.refreshQuota(); await flush();
  await h.ctx.refreshStatus(); await flush();
  old.resolve(response({ quota: { ok: true, profile_id: 'profile_a', email: 'a@example.com', used: 10 } })); await first;
  assert.equal(reads, 2); assert.equal(h.values('quota').at(-1).profile_id, 'profile_b');
  assert.equal(h.values('quota').at(-1).used, 80);
  assert.equal(h.values('quota').some(row => row.used === 10), false);
});

test('a quota response for another workspace is never painted under the current workspace', async () => {
  const h = harness(url => url === '/api/quota?background=1'
    ? Promise.resolve(response({ quota: { ok: true, profile_id: 'profile_other', email: 'a@example.com', used: 99 } })) : undefined);
  await h.ctx.refreshQuota(); await flush();
  assert.equal(h.values('quota').length, 0);
  assert.equal(h.calls.some(call => call.url === '/api/status'), true);
});

test('quota refresh failure retains the rendered data and its original sampling time', async () => {
  let fail = false;
  const h = harness(url => url === '/api/quota?background=1' ? fail ? Promise.reject(new Error('network down'))
    : Promise.resolve(response({ quota: { ok: true, profile_id: 'profile_a', used: 30, fetched_at: '2026-01-01T00:00:00Z' } })) : undefined);
  await h.ctx.refreshQuota(); fail = true; await h.ctx.refreshQuota();
  assert.equal(h.values('quota').length, 1);
  assert.equal(h.ctx.lastCurrentQuota.fetched_at, '2026-01-01T00:00:00Z');
  assert.match(h.notice('#quotaMsg'), /已有数据保留.*network down/);
});

test('pending pool shows completed members immediately; short polling never forces upstream refresh', async () => {
  let reads = 0;
  const h = harness(url => url.startsWith('/api/accounts/quotas?') ? Promise.resolve(response(++reads === 1
    ? { quotas: { profile_a: { ok: true, used: 20 }, profile_b: { ok: false, pending: true, error: 'quota_pending' } }, pending: ['profile_b'] }
    : { quotas: { profile_a: { ok: true, used: 20 }, profile_b: { ok: true, used: 50 } }, pending: [] })) : undefined);
  await h.ctx.refreshPoolQuotas(true);
  assert.equal(h.ctx.poolQuotas.profile_a.used, 20); assert.equal(h.ctx.poolQuotas.profile_b.pending, true);
  assert.match(h.notice('#poolQuotaNotice'), /1 个工作区/);
  await h.fire(1500);
  assert.equal(h.ctx.poolQuotas.profile_b.used, 50);
  assert.deepEqual(h.calls.map(call => call.url), ['/api/accounts/quotas?background=1&force=1', '/api/accounts/quotas?background=1']);
  assert.equal(h.timers.size, 0);
});

test('manual refresh preserves old successful data while a new cache is pending', async () => {
  const h = harness(url => url.includes('background=1') ? Promise.resolve(response(url.startsWith('/api/accounts/')
    ? { quotas: { profile_a: { ok: false, pending: true } }, pending: ['profile_a'] }
    : { quota: { ok: false, pending: true }, pending: true })) : undefined);
  h.ctx.lastCurrentQuota = { ok: true, profile_id: 'profile_a', used: 30, fetched_at: '2026-01-01T00:00:00Z' };
  h.ctx.poolQuotas = { profile_a: h.ctx.lastCurrentQuota };
  await Promise.all([h.ctx.refreshQuota(true), h.ctx.refreshPoolQuotas(true)]);
  assert.equal(h.values('quota').at(-1).used, 30); assert.equal(h.values('quota').at(-1).pending, true);
  assert.equal(h.ctx.poolQuotas.profile_a.used, 30);
  assert.equal(h.ctx.poolQuotas.profile_a.fetched_at, '2026-01-01T00:00:00Z');
});

test('background refresh failure keeps old success and ends short polling', async () => {
  const h = harness(url => url.startsWith('/api/quota?') ? Promise.resolve(response({ quota: {
    ok: true, profile_id: 'profile_a', used: 40, pending: false, stale: true,
    fetched_at: '2026-01-01T00:00:00Z', refresh_error: { message: 'upstream down' },
  }, pending: false })) : undefined);
  await h.ctx.refreshQuota();
  assert.equal(h.values('quota').at(-1).used, 40); assert.equal(h.timers.size, 0);
  assert.match(h.notice('#quotaMsg'), /显示上次数据.*upstream down/);
});

test('hiding stops pending and desktop polls; returning resumes all modules immediately', async () => {
  const h = harness(url => url === '/api/quota?background=1'
    ? Promise.resolve(response({ quota: { ok: false, pending: true }, pending: true })) : undefined);
  await h.ctx.refreshQuota(); assert.equal(Array.from(h.timers.values()).some(timer => timer.ms === 1500), true);
  h.ctx.document.hidden = true; h.ctx.dashboardVisibilityChanged();
  h.ctx.pollDashboard(); h.ctx.pollDesktop(); h.ctx.pollLoginModal(); await h.fire(1500);
  assert.equal(h.timers.size, 0); assert.equal(h.calls.length, 1);
  h.ctx.document.hidden = false; h.ctx.dashboardVisibilityChanged(); await flush();
  assert.equal(h.calls.some(call => call.url === '/api/status'), true);
  assert.equal(h.calls.some(call => call.url === '/api/desktop/status?background=1'), true);
  assert.equal(h.calls.filter(call => call.url === '/api/quota?background=1').length, 2);
  assert.match(html, /setInterval\(pollDesktop, 15000\)/);
});

test('a pending response that arrives after hiding does not schedule another poll', async () => {
  const waiting = defer();
  const h = harness(url => url === '/api/quota?background=1' ? waiting.promise : undefined);
  const pending = h.ctx.refreshQuota(); await flush(); h.ctx.document.hidden = true;
  waiting.resolve(response({ quota: { ok: false, pending: true }, pending: true })); await pending;
  assert.equal(h.timers.size, 0);
});

test('desktop cold probe reports pending and polls without saying the app is closed', async () => {
  let ready = false;
  const h = harness(url => url === '/api/desktop/status?background=1'
    ? Promise.resolve(response(ready
      ? { desktop: { available: true, running: true, pids: [123] }, pending: false }
      : { desktop: null, pending: true })) : undefined);
  await h.ctx.refreshDesktop();
  assert.match(h.notice('#desktopHint'), /正在检测/);
  assert.equal(h.values('desktop').length, 0);
  ready = true; await h.fire(1500);
  assert.equal(h.values('desktop').at(-1).running, true);
  assert.equal(h.timers.size, 0);
});

test('desktop refresh failure preserves the last sample and reveals its error', async () => {
  let fail = false;
  const h = harness(url => url === '/api/desktop/status?background=1'
    ? Promise.resolve(response(fail
      ? { desktop: null, pending: false, refresh_error: { message: 'probe failed' } }
      : { desktop: { available: true, running: true }, pending: false })) : undefined);
  await h.ctx.refreshDesktop(); fail = true; await h.ctx.refreshDesktop();
  assert.equal(h.values('desktop').at(-1).running, true);
  assert.match(h.notice('#desktopHint'), /更新失败.*上次状态/);
  assert.equal(h.ctx.$('#desktopHint').title, 'probe failed');
});

test('post-operation desktop refresh bypasses an older view and its response', async () => {
  const waiting = defer();
  const h = harness(url => url === '/api/desktop/status?background=1' ? waiting.promise
    : url === '/api/desktop/status?background=1&force=1'
      ? Promise.resolve(response({ desktop: { available: true, running: true, pids: [2] }, pending: false })) : undefined);
  const old = h.ctx.refreshDesktop(); await flush();
  await h.ctx.refreshDesktop(true);
  waiting.resolve(response({ desktop: { available: true, running: false, pids: [] }, pending: false }));
  await old;
  assert.deepEqual(h.values('desktop').map(row => row.running), [true]);
});

test('hiding clears a pending desktop follow-up', async () => {
  const h = harness(url => url === '/api/desktop/status?background=1'
    ? Promise.resolve(response({ desktop: null, pending: true })) : undefined);
  await h.ctx.refreshDesktop();
  h.ctx.document.hidden = true; h.ctx.dashboardVisibilityChanged(); await h.fire(1500);
  assert.equal(h.calls.length, 1);
  assert.equal(h.timers.size, 0);
});

test('the complete dashboard script remains valid JavaScript', () => {
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  assert.doesNotThrow(() => new vm.Script(script));
});
