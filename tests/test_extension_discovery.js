const test = require('node:test');
const assert = require('node:assert/strict');
const { createClient, normalize } = require('../extension/local-service.js');

const BASE = 'http://127.0.0.1:';
const health = { ok: true, service: 'codex-switcher' };
const reply = value => ({ ok: true, text: async () => JSON.stringify(value) });
const settle = () => new Promise(resolve => setImmediate(resolve));

function harness(seed = {}, endpoints = {}) {
  const stored = { ...seed }, calls = [], listeners = [];
  let clock = 0;
  const options = {
    now: () => clock,
    storage: { get: async defaults => ({ ...defaults, ...stored }),
      set: async values => { Object.assign(stored, values); } },
    changes: { addListener: fn => listeners.push(fn) },
    fetch: async (url, init = {}) => {
      calls.push({ url, init });
      const target = new URL(url), port = Number(target.port);
      if (target.pathname === '/api/health') {
        const value = endpoints[port];
        if (typeof value === 'function') return value(url, init);
        return reply(value === true ? health : value || { ok: true, service: 'other-app' });
      }
      if (endpoints.request) return endpoints.request(url, init);
      return reply({ ok: true });
    },
  };
  const client = createClient(options);
  return { client, calls, stored, endpoints, options, setClock: value => { clock = value; },
    changed(address) { listeners.forEach(fn => fn({ cs_service_address: { newValue: address } }, 'local')); },
    probes: () => calls.filter(call => call.url.endsWith('/api/health')),
    requests: () => calls.filter(call => !call.url.endsWith('/api/health')) };
}

test('custom address accepts only an explicit numeric IPv4 loopback port', () => {
  assert.equal(normalize(' http://127.0.0.1:9888/ '), BASE + '9888');
  for (const value of ['http://localhost:8787', 'https://127.0.0.1:8787', 'http://127.0.0.1',
    'http://127.0.0.1:0', 'http://127.0.0.1:65536', 'http://127.0.0.1:1/a',
    'http://127.0.0.1:1?x=1', 'http://user@127.0.0.1:1', 'http://127.0.0.1.evil:8787',
    'http://2130706433:8787', 'http://127.0.0.1:8787#fragment']) assert.throws(() => normalize(value));
});

test('first connection bypasses a foreign default port using only health probes', async () => {
  const h = harness({}, { 8788: true });
  await h.client.fetch('/api/status');
  assert.equal(h.requests()[0].url, BASE + '8788/api/status');
  assert.equal(h.stored.cs_service_discovered_url, BASE + '8788');
  assert.ok(h.probes().every(call => call.init.method === 'GET' && !call.init.body));
  assert.ok(h.calls.every(call => call.init.redirect === 'error' && call.init.credentials === 'omit'));
});

test('legacy service identity requires all compatibility fields', async () => {
  const h = harness({}, { 8787: { ok: true, service_pid: 321, session_api_version: 5 } });
  assert.equal(await h.client.resolve(), BASE + '8787');
  const foreign = harness({}, { 8787: { ok: true, service_pid: 321 } });
  await assert.rejects(foreign.client.resolve(), /未找到/);
});

test('parallel reads share discovery and healthy polling does not scan again', async () => {
  const h = harness({}, { 8787: true });
  await Promise.all([h.client.fetch('/api/status'), h.client.fetch('/api/quota')]);
  await h.client.fetch('/api/status');
  assert.equal(h.probes().length, 1);
  assert.equal(h.requests().length, 3);
});

test('persisted discovered address is checked first after a worker restarts', async () => {
  const h = harness({ cs_service_discovered_url: BASE + '8793' }, { 8793: true });
  assert.equal(await h.client.resolve(), BASE + '8793');
  assert.deepEqual(h.probes().map(call => call.url), [BASE + '8793/api/health']);
});

test('unavailable service scans at most the bounded range and backs off between polls', async () => {
  const h = harness();
  await assert.rejects(h.client.resolve());
  assert.equal(h.probes().length, 11);
  await assert.rejects(h.client.resolve());
  assert.equal(h.probes().length, 11);
  h.setClock(5000);
  await assert.rejects(h.client.resolve());
  assert.equal(h.probes().length, 22);
  h.setClock(9000);
  await assert.rejects(h.client.resolve());
  assert.equal(h.probes().length, 22);
  h.setClock(15000);
  h.endpoints[8797] = true;
  assert.equal(await h.client.resolve(), BASE + '8797');
});

test('stalled health bodies cannot block discovery and concurrent probes are bounded', async () => {
  const h = harness({}, Object.fromEntries(Array.from({ length: 11 }, (_, i) =>
    [8787 + i, () => ({ ok: true, text: () => new Promise(() => {}) })])));
  const timers = new Map();
  let timerId = 0, maximum = 0;
  const client = createClient({ ...h.options,
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); maximum = Math.max(maximum, timers.size); return id; },
    clearTimeout(id) { timers.delete(id); } });
  const pending = assert.rejects(client.resolve(), /未找到/);
  for (let batch = 0; batch < 5; batch++) {
    await settle();
    for (const [id, fn] of [...timers]) { timers.delete(id); fn(); }
  }
  await pending;
  assert.equal(h.probes().length, 11);
  assert.ok(maximum <= 3);
});

test('manual custom port probes only that address and automatic mode can be restored', async () => {
  const h = harness({}, { 29123: true, 8789: true });
  assert.equal(await h.client.setAddress(BASE + '29123'), BASE + '29123');
  assert.equal(h.probes().length, 1);
  assert.equal(h.stored.cs_service_address, BASE + '29123');
  assert.equal(await h.client.setAddress(''), BASE + '8789');
  assert.equal(h.stored.cs_service_address, '');
  await assert.rejects(h.client.setAddress('https://example.com'), /地址须为/);
  assert.equal(h.client.peek(), BASE + '8789');
});

test('connection failure invalidates selection so the next read discovers the moved service', async () => {
  const h = harness({}, { 8787: true });
  await h.client.resolve();
  h.endpoints.request = () => { throw new Error('disconnected'); };
  await assert.rejects(h.client.fetch('/api/status'), /disconnected/);
  delete h.endpoints[8787];
  delete h.endpoints.request;
  h.endpoints[8788] = true;
  await h.client.fetch('/api/status');
  assert.equal(h.requests().at(-1).url, BASE + '8788/api/status');
});

test('mutation is sent once and never replayed when its response is lost', async () => {
  const h = harness({}, { 8787: true });
  h.endpoints.request = () => { throw new Error('response lost'); };
  await assert.rejects(h.client.fetch('/api/switch', { method: 'POST', body: '{"confirm":true}' }), /response lost/);
  assert.equal(h.requests().length, 1);
  assert.equal(h.requests()[0].init.method, 'POST');
});

test('mutation revalidates identity and never submits to a foreign replacement', async () => {
  const h = harness({}, { 8787: true });
  await h.client.resolve();
  delete h.endpoints[8787];
  h.endpoints[8788] = true;
  await assert.rejects(h.client.fetch('/api/accounts/import', { method: 'POST', body: 'fixture-secret' }), /未找到/);
  assert.equal(h.requests().length, 0);
});

test('settings changed in another extension context invalidate the selected endpoint', async () => {
  const h = harness({}, { 8787: true, 29234: true });
  await h.client.resolve();
  h.changed(BASE + '29234');
  await h.client.fetch('/api/status');
  assert.equal(h.requests()[0].url, BASE + '29234/api/status');
});

test('HTTP failure from a replaced service invalidates the old endpoint', async () => {
  const h = harness({}, { 8787: true });
  await h.client.resolve();
  delete h.endpoints[8787];
  h.endpoints.request = () => ({ ok: false, status: 404, text: async () => 'another local app' });
  await assert.rejects(h.client.fetch('/api/status'), /未找到/);
  delete h.endpoints.request;
  h.endpoints[8788] = true;
  assert.equal(await h.client.resolve(), BASE + '8788');
});

test('caller-reported request timeout allows discovery of a new listening port', async () => {
  const h = harness({}, { 8787: true });
  await h.client.resolve();
  delete h.endpoints[8787];
  h.endpoints[8790] = true;
  h.client.connectionFailed();
  assert.equal(await h.client.resolve(), BASE + '8790');
});
