const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const apiSource = html.slice(html.indexOf('  const pendingReads ='), html.indexOf('  function el('));
const refreshSource = html.slice(html.indexOf('  let refreshInFlight ='), html.indexOf('  async function refreshDashboard('));
const pollSource = html.slice(html.indexOf('  function pollDashboard('), html.indexOf('  document.addEventListener("visibilitychange"'));
const defer = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const response = (payload, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => payload });

test('concurrent identical GETs share the network request only while pending', async () => {
  const first = defer();
  let calls = 0;
  const ctx = vm.createContext({ fetch: () => { calls++; return calls === 1 ? first.promise : Promise.resolve(response({ n: calls })); } });
  vm.runInContext(apiSource, ctx);
  const a = ctx.api('/api/quota');
  const b = ctx.api('/api/quota');
  assert.equal(calls, 1);
  first.resolve(response({ n: 1 }));
  assert.equal((await a).n, 1);
  assert.equal((await b).n, 1);
  assert.equal((await ctx.api('/api/quota')).n, 2);
});

test('a mutation makes the following GET independent of earlier reads', async () => {
  const old = defer();
  const fresh = defer();
  let gets = 0;
  const ctx = vm.createContext({ fetch: (url, options) => options ? Promise.resolve(response({})) : (++gets === 1 ? old.promise : fresh.promise) });
  vm.runInContext(apiSource, ctx);
  const a = ctx.api('/api/goals');
  await ctx.api('/api/switch', { method: 'POST' });
  const b = ctx.api('/api/goals');
  old.resolve(response({ version: 'old' }));
  await a;
  const c = ctx.api('/api/goals');
  assert.equal(gets, 2);
  fresh.resolve(response({ version: 'fresh' }));
  assert.equal((await b).version, 'fresh');
  assert.equal((await c).version, 'fresh');
});

test('failed reads are released so retry can succeed', async () => {
  let calls = 0;
  const ctx = vm.createContext({ fetch: async () => ++calls === 1 ? response({ message: 'busy' }, false) : response({ ok: true }) });
  vm.runInContext(apiSource, ctx);
  await assert.rejects(ctx.api('/api/status'), /busy/);
  assert.equal((await ctx.api('/api/status')).ok, true);
});

test('slow dashboard refreshes do not overlap, and hidden polls stay quiet', async () => {
  const waiting = defer();
  let refreshes = 0;
  let loginPolls = 0;
  const ctx = vm.createContext({ readGeneration: 0, document: { hidden: true }, refreshDashboard: () => { refreshes++; return waiting.promise; }, refreshLoginModal: () => { loginPolls++; } });
  vm.runInContext(refreshSource + pollSource, ctx);
  ctx.pollDashboard();
  ctx.pollLoginModal();
  assert.equal(refreshes, 0);
  assert.equal(loginPolls, 0);
  ctx.document.hidden = false;
  const a = ctx.pollDashboard();
  const b = ctx.pollDashboard();
  assert.equal(refreshes, 1);
  ctx.pollLoginModal();
  assert.equal(loginPolls, 1);
  waiting.resolve();
  await Promise.all([a, b]);
  await ctx.refresh();
  assert.equal(refreshes, 2);
});

test('a post-mutation dashboard refresh does not wait for a stale refresh', async () => {
  const old = defer();
  const fresh = defer();
  let calls = 0;
  const ctx = vm.createContext({ readGeneration: 0, refreshDashboard: () => ++calls === 1 ? old.promise : fresh.promise });
  vm.runInContext(refreshSource, ctx);
  const a = ctx.refresh();
  ctx.readGeneration++;
  const b = ctx.refresh();
  assert.equal(calls, 2);
  old.resolve();
  await a;
  const c = ctx.refresh();
  assert.equal(calls, 2);
  fresh.resolve();
  await Promise.all([b, c]);
});
