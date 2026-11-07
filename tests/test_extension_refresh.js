const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../extension/sidepanel.js'), 'utf8');
// Keep production event bindings and request/render functions, but start each
// refresh explicitly. All HTTP requests and browser APIs are local doubles.
const script = source.slice(0, source.indexOf('\ninitNotifyToggle();'));
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const nodes = new Map(), calls = [], timers = new Map(), messages = [];
  let timerId = 0;
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      children: [], value: '', checked: false, disabled: false, hidden: false,
      style: {}, dataset: {}, listeners: {}, _text: '',
      set textContent(value) { this._text = value; this.children = []; },
      get textContent() { return this._text; },
      appendChild(child) { this.children.push(child); if (this.children.length === 1) this.value = child.value; },
      addEventListener(event, fn) { this.listeners[event] = fn; },
      classList: { toggle() {}, contains() { return false; } },
    });
    return nodes.get(id);
  };
  const document = { hidden: false, querySelector: node, getElementById: id => node('#' + id),
    documentElement: node('html'), addEventListener() {} };
  const context = vm.createContext({
    document, AbortController, console,
    Option: function(text, value) { this.text = text; this.value = value; },
    setTimeout(fn, ms) { const id = ++timerId; timers.set(id, { fn, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    window: { matchMedia: () => ({ matches: false, addEventListener() {} }), addEventListener() {}, confirm: () => true },
    localStorage: { setItem() {} },
    chrome: { runtime: { getManifest: () => ({ version: 'test' }),
      sendMessage(message, callback) { messages.push(message); if (callback) callback(); } } },
    fetch(url, options) {
      return new Promise((resolve, reject) => {
        const call = { path: url.replace('http://127.0.0.1:8787', ''), options, resolve, reject, done: false };
        calls.push(call);
        options.signal.addEventListener('abort', () => {
          call.done = true;
          reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
        });
      });
    },
  });
  vm.runInContext(script + '\nglobalThis.ui = { refresh, refreshStatus, refreshQuota, setQuotaIdentity, acceptQuota, suspendPanel, panelVisibilityChanged };', context);
  return {
    ui: context.ui, document, calls, timers, messages, $: node,
    resolve(prefix, payload) {
      const call = calls.find(item => !item.done && item.path.startsWith(prefix));
      assert.ok(call, 'pending request ' + prefix);
      call.done = true;
      call.resolve({ ok: true, text: async () => JSON.stringify(payload) });
      return call;
    },
    poll() {
      const entry = [...timers].find(([, timer]) => timer.ms === 1000);
      assert.ok(entry, 'pending quota poll');
      timers.delete(entry[0]); entry[1].fn();
    },
  };
}

const auth = (id = 'a') => ({ logged_in: true, profile_id: 'profile_' + id,
  email: 'same@example.com', account_id: 'workspace-' + id, plan: 'plus' });
const quota = (id = 'a', extra = {}) => ({ quota: { ok: true, profile_id: 'profile_' + id,
  email: 'same@example.com', account_id: 'workspace-' + id, fetched_at: '2026-11-07T13:00:00Z',
  primary_window: { used_percent: 20 }, ...extra } });

test('sidepanel starts independent reads and paints identity and targets before a slow quota', async () => {
  const h = harness();
  const pending = h.ui.refresh();
  assert.deepEqual(h.calls.map(c => c.path), ['/api/theme', '/api/status', '/api/snapshots', '/api/quota?background=1']);
  h.resolve('/api/snapshots', { switching: { enabled: true }, current: auth(), snapshots: [auth('b')] });
  await settle();
  assert.equal(h.$('#switchTarget').children[0].value, 'profile_b');
  h.resolve('/api/status', { auth: auth(), accounts: { total: 2 }, switching: { enabled: true } });
  await settle();
  assert.match(h.$('#account').textContent, /same@example.com/);
  assert.match(h.$('#quotaHint').textContent, /正在读取额度/);
  h.resolve('/api/quota', quota());
  h.resolve('/api/theme', { theme: 'light' });
  await pending;
  assert.match(h.$('#quota5h').textContent, /剩 80%/);
});

test('cached quotas keep their real sample time and short-poll without repeated force or badge work', async () => {
  const h = harness();
  h.ui.setQuotaIdentity(auth());
  h.resolve('/api/quota', quota());
  await settle();
  const freshText = h.$('#quotaHint').textContent;
  h.ui.acceptQuota({ quota: { ok: false, profile_id: 'profile_a', fetched_at: null,
    error: 'quota_pending', pending: true, refreshing: true } });
  assert.match(h.$('#quota5h').textContent, /剩 80%/);
  assert.ok(h.$('#quotaHint').textContent.startsWith(freshText));
  const forced = h.ui.refreshQuota(true);
  assert.equal(h.calls.at(-1).path, '/api/quota?background=1&force=1');
  h.resolve('/api/quota', quota('a', { pending: true, refreshing: true, stale: true }));
  await forced;
  assert.ok(h.$('#quotaHint').textContent.startsWith(freshText));
  assert.match(h.$('#quotaHint').textContent, /正在更新/);
  assert.equal(h.messages.length, 1);
  h.poll();
  assert.equal(h.calls.at(-1).path, '/api/quota?background=1');
  h.resolve('/api/quota', quota('a', { pending: false, refreshing: false,
    refresh_error: { error: 'timeout' }, stale: true }));
  await settle();
  assert.ok(h.$('#quotaHint').textContent.startsWith(freshText));
  assert.match(h.$('#quotaHint').textContent, /更新失败，保留上次结果/);
  assert.equal([...h.timers.values()].some(timer => timer.ms === 1000), false);
});

test('a cold pending quota stays loading until the background sample arrives', async () => {
  const h = harness();
  h.ui.setQuotaIdentity(auth());
  h.resolve('/api/quota', { quota: { ok: false, error: 'quota_pending', profile_id: 'profile_a',
    fetched_at: null, pending: true, refreshing: false } });
  await settle();
  assert.match(h.$('#quotaHint').textContent, /正在读取额度/);
  assert.doesNotMatch(h.$('#quotaHint').textContent, /不可用|更新于/);
  h.poll();
  h.resolve('/api/quota', quota());
  await settle();
  assert.match(h.$('#quota5h').textContent, /剩 80%/);
  assert.equal([...h.timers.values()].some(timer => timer.ms === 1000), false);
});

test('an old same-email workspace response cannot replace the newly selected workspace quota', async () => {
  const h = harness();
  h.ui.setQuotaIdentity(auth());
  h.ui.setQuotaIdentity(auth('b'));
  h.resolve('/api/quota', quota('a', { primary_window: { used_percent: 99 } }));
  await settle();
  assert.doesNotMatch(h.$('#quota5h').textContent, /剩 1%/);
  h.resolve('/api/quota', quota('b', { primary_window: { used_percent: 40 } }));
  await settle();
  assert.match(h.$('#quota5h').textContent, /剩 60%/);
  h.ui.acceptQuota(quota('b', { fetched_at: '2026-11-07T12:59:00Z', primary_window: { used_percent: 99 } }));
  assert.match(h.$('#quota5h').textContent, /剩 60%/);
});

test('hidden sidepanels abort owned reads, stop quota polls and make no background refresh requests', async () => {
  const h = harness();
  h.ui.setQuotaIdentity(auth());
  h.resolve('/api/quota', quota('a', { pending: true, refreshing: true }));
  await settle();
  h.poll();
  const last = h.calls.at(-1);
  h.document.hidden = true;
  h.ui.panelVisibilityChanged();
  assert.equal(last.options.signal.aborted, true);
  await settle();
  const count = h.calls.length;
  await h.ui.refresh();
  await h.ui.refreshQuota();
  assert.equal(h.calls.length, count);
  assert.equal(h.timers.size, 0);
  h.document.hidden = false;
  h.ui.panelVisibilityChanged();
  assert.equal(h.calls.length, count + 4);
});
