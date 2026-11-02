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

test('owned credits remain visible when none currently apply in both views', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 1, applicable_available_count: 0 }));
  assert.equal(h.ui.poolCredits('TEST@example.com'), 1);
  assert.match(h.account().textContent, /券 1/);
  const chip = descendants(h.account()).find(node => node.textContent === '券 1');
  assert.match(chip.title, /剩余 1 张重置券 · 当前适用 0 张/);
  assert.match(h.$('#quotaBox').textContent, /剩余 1 张 · 当前适用 0 张/);
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('applicable credits enable both manual reset controls', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 2, applicable_available_count: 1 }));
  assert.match(h.account().textContent, /券 2/);
  assert.match(h.$('#quotaBox').textContent, /剩余 2 张 · 当前适用 1 张/);
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
});

test('zero cached inventory does not block manual redemption', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 0, applicable_available_count: 0 }));
  assert.equal(resetAction(h).disabled, false);
  assert.equal(h.$('#quotaResetBtn').hidden, false);
  assert.equal(h.$('#quotaResetBtn').disabled, false);
  assert.match(h.$('#quotaBox').textContent, /剩余 0 张 · 当前适用 0 张/);
});

test('missing applicability stays unknown and does not erase known inventory', () => {
  const h = harness();
  h.render(quota({ known: true, available_count: 2 }));
  assert.match(h.account().textContent, /券 2/);
  assert.match(h.$('#quotaBox').textContent, /剩余 2 张 · 当前适用数量未知/);
});

test('unknown information is never displayed as zero owned credits', () => {
  const h = harness();
  h.render(quota({ known: false, available_count: 0, applicable_available_count: 0 }));
  assert.equal(h.ui.poolCredits('test@example.com'), null);
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
