const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const auth = (id = 'a', extra = {}) => ({ logged_in: true, profile_id: 'profile_' + id,
  workspace_id: 'workspace-' + id, email: 'same@example.test', plan: 'team', ...extra });
const quota = (id = 'a', extra = {}) => ({ ok: true, profile_id: 'profile_' + id,
  workspace_id: 'workspace-' + id, email: 'same@example.test', plan: 'team',
  fetched_at: '2026-11-07T16:00:00Z', primary_window: { used_percent: 20 },
  secondary_window: { used_percent: 30 }, ...extra });

function harness(seed = {}) {
  const stored = { ...seed }, calls = [], timers = new Map(), titles = [], notifications = [], messages = [];
  let timerId = 0;
  let storageHold = null;
  const event = () => ({ addListener() {} });
  const context = vm.createContext({
    console, AbortController,
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    fetch(url, options) {
      return new Promise((resolve, reject) => calls.push({
        path: url.replace('http://127.0.0.1:8787', ''), options, resolve, reject, done: false,
      }));
    },
    chrome: {
      action: { setBadgeText: async () => {}, setBadgeBackgroundColor: async () => {},
        setTitle: async ({ title }) => { titles.push(title); } },
      storage: { local: { get: async defaults => {
        const result = Object.fromEntries(Object.entries(defaults).map(([key, value]) =>
          [key, Object.hasOwn(stored, key) ? stored[key] : value]));
        if (storageHold && Object.hasOwn(defaults, storageHold.key)) {
          const hold = storageHold;
          hold.waiting = true;
          return new Promise(resolve => {
            hold.release = () => { storageHold = null; resolve(result); };
          });
        }
        return result;
      },
        set: async value => Object.assign(stored, value) } },
      notifications: { create: (id, value) => notifications.push(value), onClicked: event() },
      alarms: { onAlarm: event() },
      runtime: { onInstalled: event(), onStartup: event(), onMessage: { addListener(fn) { messages.push(fn); } } },
    },
  });
  vm.runInContext(source, context);
  const take = prefix => {
    const call = calls.find(item => !item.done && item.path.startsWith(prefix));
    assert.ok(call, 'pending request ' + prefix);
    call.done = true;
    return call;
  };
  return {
    refresh: context.refresh, stored, calls, timers, titles, notifications,
    resolve(prefix, data) { take(prefix).resolve({ ok: true, text: async () => JSON.stringify(data) }); },
    reject(prefix, message = 'fixture timeout') { take(prefix).reject(new Error(message)); },
    stallBody(prefix) { take(prefix).resolve({ ok: true, text: () => new Promise(() => {}) }); },
    tick(ms) {
      const entries = [...timers].filter(([, timer]) => timer.ms === ms);
      assert.ok(entries.length, 'timer at ' + ms);
      for (const [id, timer] of entries) { timers.delete(id); timer.fn(); }
    },
    ping() { messages[0]({ type: 'cs-refresh-badge' }, {}, () => {}); },
    holdStorage(key) { storageHold = { key, waiting: false }; return storageHold; },
  };
}

async function complete(h, identity = 'a', result = quota(identity)) {
  h.resolve('/api/status', { auth: auth(identity), accounts: { total: 2 } });
  h.resolve('/api/quota', { quota: result });
  await settle();
  await settle();
}

test('badge starts reads together and merges overlapping alarm/panel reads', async () => {
  const h = harness();
  const first = h.refresh(), second = h.refresh(), third = h.refresh();
  assert.equal(first, second);
  assert.equal(first, third);
  assert.deepEqual(h.calls.map(call => call.path), ['/api/status', '/api/quota?background=1']);
  await complete(h);
  const snapshot = await first;
  assert.equal(snapshot.remaining_5h, 80);
  assert.equal(snapshot.online, true);
  assert.equal(snapshot.quota_pending, false);
});

test('slow quota paints confirmed online status before the quota finishes', async () => {
  const h = harness();
  const pending = h.refresh();
  h.resolve('/api/status', { auth: auth() });
  await settle();
  assert.equal(h.stored.cs_snapshot.online, true);
  assert.equal(h.stored.cs_snapshot.logged_in, true);
  assert.equal(h.stored.cs_snapshot.quota_pending, true);
  assert.match(h.titles.at(-1), /服务在线.*正在读取/);
  h.resolve('/api/quota', { quota: quota() });
  assert.equal((await pending).quota_ok, true);
});

test('quota failure preserves only the current workspace sample and marks it stale', async () => {
  const h = harness({ cs_quota_sample: quota() });
  const pending = h.refresh();
  h.resolve('/api/status', { auth: auth() });
  h.reject('/api/quota');
  const snapshot = await pending;
  assert.equal(snapshot.online, true);
  assert.equal(snapshot.logged_in, true);
  assert.equal(snapshot.quota_ok, true);
  assert.equal(snapshot.remaining_5h, 80);
  assert.equal(snapshot.quota_stale, true);
  assert.equal(snapshot.quota_at, '2026-11-07T16:00:00Z');
  assert.match(h.titles.at(-1), /刷新失败/);
  assert.equal(h.notifications.length, 0);
  assert.equal([...h.timers.values()].some(timer => timer.ms === 1500), false);
});

test('same email in another workspace cannot borrow cached quota or a raced response', async () => {
  const h = harness({ cs_quota_sample: quota('a') });
  const pending = h.refresh();
  h.resolve('/api/status', { auth: auth('b') });
  h.resolve('/api/quota', { quota: quota('a') });
  const snapshot = await pending;
  assert.equal(snapshot.profile_id, 'profile_b');
  assert.equal(snapshot.quota_ok, false);
  assert.equal(snapshot.remaining_5h, null);
  assert.equal(snapshot.quota_pending, true);
  assert.equal(h.notifications.length, 0);
});

test('legacy identity fallback requires both workspace and user/email', async () => {
  for (const workspace of ['workspace-a', 'workspace-b', '']) {
    const h = harness();
    const pending = h.refresh();
    h.resolve('/api/status', { auth: auth('a', { profile_id: '', workspace_id: workspace }) });
    h.resolve('/api/quota', { quota: quota('a', { profile_id: '' }) });
    const snapshot = await pending;
    assert.equal(snapshot.quota_ok, workspace === 'workspace-a');
  }
});

test('status failure remains offline even when the quota request succeeds', async () => {
  const h = harness();
  const pending = h.refresh();
  h.reject('/api/status', 'fixture service unavailable');
  h.resolve('/api/quota', { quota: quota() });
  const snapshot = await pending;
  assert.equal(snapshot.online, false);
  assert.equal(snapshot.logged_in, false);
  assert.equal(h.stored.cs_quota_sample, undefined);
  assert.match(h.titles.at(-1), /本地服务未启动/);
});

test('a stalled quota body is bounded and does not retain the single-flight lock', async () => {
  const h = harness();
  const pending = h.refresh();
  h.resolve('/api/status', { auth: auth() });
  h.stallBody('/api/quota');
  await settle();
  h.tick(8000);
  const snapshot = await pending;
  assert.equal(snapshot.online, true);
  assert.equal(snapshot.quota_ok, false);
  assert.match(snapshot.quota_error, /超时/);
  assert.equal(h.calls[1].options.signal.aborted, true);
  const retry = h.refresh();
  await complete(h);
  assert.equal((await retry).remaining_5h, 80);
});

test('pending quota gets bounded short polls without forcing upstream refreshes', async () => {
  const h = harness();
  const pendingQuota = quota('a', { ok: false, pending: true, fetched_at: null });
  let pending = h.refresh();
  await complete(h, 'a', pendingQuota);
  await pending;
  for (let i = 0; i < 8; i++) {
    h.tick(1500);
    await complete(h, 'a', pendingQuota);
  }
  assert.equal(h.calls.length, 18);
  assert.ok(h.calls.filter(call => call.path.startsWith('/api/quota')).every(call => call.path === '/api/quota?background=1'));
  assert.equal([...h.timers.values()].some(timer => timer.ms === 1500), false);
});

test('stale exhaustion does not notify or clear the last confirmed notification', async () => {
  const h = harness();
  const exhausted = quota('a', { limit_reached: true, primary_window: { used_percent: 100 } });
  let pending = h.refresh();
  await complete(h, 'a', { ...exhausted, pending: true, stale: true });
  await pending;
  assert.equal(h.notifications.length, 0);
  h.tick(1500);
  await complete(h, 'a', exhausted);
  assert.equal(h.notifications.length, 1);
  pending = h.refresh();
  h.resolve('/api/status', { auth: auth() });
  h.reject('/api/quota');
  await pending;
  assert.equal(h.stored.cs_prev_exhausted, true);
  pending = h.refresh();
  await complete(h, 'a', exhausted);
  await pending;
  assert.equal(h.notifications.length, 1);
});

test('a sidebar sample invalidates an older read and coalesces a follow-up refresh', async () => {
  const h = harness();
  const old = h.refresh();
  h.ping(); h.ping();
  assert.equal(h.calls.length, 2);
  await complete(h, 'a');
  await old;
  assert.equal(h.stored.cs_snapshot, undefined);
  assert.equal(h.calls.length, 4);
  await complete(h, 'b');
  assert.equal(h.stored.cs_snapshot.profile_id, 'profile_b');
  assert.equal(h.stored.cs_quota_sample.profile_id, 'profile_b');
});

test('a sidebar switch during notification storage reads suppresses the old workspace notification', async () => {
  for (const key of ['cs_prev_exhausted', 'notify_on_exhausted']) {
    const h = harness();
    const hold = h.holdStorage(key);
    const old = h.refresh();
    await complete(h, 'a', quota('a', { limit_reached: true, primary_window: { used_percent: 100 } }));
    assert.equal(hold.waiting, true, 'notification paused at ' + key);
    h.ping();
    hold.release();
    await old;
    assert.equal(h.notifications.length, 0);
    assert.equal(h.stored.cs_prev_exhausted, undefined, 'old notification state was not persisted');
    await complete(h, 'b');
    assert.equal(h.stored.cs_snapshot.profile_id, 'profile_b');
    assert.equal(h.stored.cs_prev_exhausted_identity, 'profile:profile_b');
    assert.equal(h.stored.cs_prev_exhausted, false);
    assert.equal(h.notifications.length, 0);
  }
});
