const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../extension/sidepanel.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../extension/sidepanel.html'), 'utf8');
const helpers = script.slice(script.indexOf('function profileRef('), script.indexOf('function renderStatus('));
const render = script.slice(script.indexOf('function updateSwitchButton('), script.indexOf('async function refreshSnapshots('));
const actions = script.slice(script.indexOf('async function doSwitch('), script.indexOf('$("#refreshBtn").addEventListener('));

function harness() {
  const nodes = new Map(), calls = [], prompts = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      children: [], value: '', checked: false, disabled: false, _text: '',
      set textContent(value) { this._text = value; this.children = []; },
      get textContent() { return this._text; },
      appendChild(option) { this.children.push(option); if (this.children.length === 1) this.value = option.value; },
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    $, Option: function(text, value) { this.text = text; this.value = value; },
    window: { confirm: text => { prompts.push(text); return true; } },
    refresh: async () => {}, invalidatePanelReads() {},
    apiPost: async (url, body) => {
      calls.push({ url, body: JSON.parse(JSON.stringify(body)) });
      return url === '/api/accounts/import' ? { inserted: 1, updated: 0 } : {
        switch: { target_email: 'same@example.com', target_profile_id: body.profile_id,
                  target_plan: 'team', target_workspace_name: 'Team B', target_account_id: 'workspace-b' },
      };
    },
  });
  vm.runInContext('let switchState = {};\n' + helpers + render + actions +
    '\nglobalThis.ui = { renderSwitch, doSwitch, doImport };', context);
  return { $, calls, prompts, ui: context.ui };
}

const profiles = [
  { email: 'same@example.com', profile_id: 'profile_a', account_id: 'workspace-a', plan: 'plus', is_current: true },
  { email: 'same@example.com', profile_id: 'profile_b', account_id: 'workspace-b', plan: 'team', workspace_name: 'Team B', is_current: false },
  { email: 'same@example.com', profile_id: 'profile_c', account_id: 'workspace-c', plan: 'team', workspace_name: 'Team C', is_current: false },
];

test('extension retains sibling workspaces of the current email as distinct targets', () => {
  const h = harness();
  h.ui.renderSwitch({ switching: { enabled: true }, current: profiles[0], snapshots: profiles });
  assert.deepEqual(h.$('#switchTarget').children.map(option => option.value), ['profile_b', 'profile_c']);
  assert.match(h.$('#switchTarget').children[0].text, /Team B/);
  h.$('#switchTarget').value = 'profile_c';
  h.ui.renderSwitch({ switching: { enabled: true }, current: profiles[0], snapshots: profiles });
  assert.equal(h.$('#switchTarget').value, 'profile_c');
});

test('extension labels Business variants as Team while retaining the original plan', () => {
  for (const [plan, identity, label] of [
    ['self_serve_business_prolite', undefined, 'Team 工作区'],
    ['self_serve_business_usage_based', undefined, 'Team 工作区'],
    ['enterprise_cbp_automation', undefined, 'Team 工作区'],
    ['future_workspace_plan', 'team', 'Team 工作区'],
    ['self_serve_business_prolite', 'personal', '个人空间'],
  ]) {
    const h = harness();
    h.ui.renderSwitch({ switching: { enabled: true }, current: profiles[0], snapshots: [
      { ...profiles[1], workspace_name: undefined, plan, identity_type: identity },
    ] });
    const option = h.$('#switchTarget').children[0];
    assert.equal(option.value, 'profile_b');
    assert.ok(option.text.includes(label));
    assert.ok(option.text.includes(plan));
  }
});

test('extension submits profile id and names the selected workspace in confirmation', async () => {
  const h = harness();
  h.ui.renderSwitch({ switching: { enabled: true }, current: profiles[0], snapshots: profiles });
  h.$('#switchTarget').value = 'profile_b';
  h.$('#switchConfirm').checked = true;
  await h.ui.doSwitch();
  assert.deepEqual(h.calls[0], { url: '/api/switch', body: { profile_id: 'profile_b', confirm: true } });
  assert.match(h.prompts[0], /Team B/);
  assert.match(h.$('#switchMsg').textContent, /Team B/);
});

test('extension keeps email-only compatibility for an old single-identity service', async () => {
  const h = harness();
  h.ui.renderSwitch({ switching: { enabled: true }, current: { email: 'one@example.com' },
    snapshots: [{ email: 'one@example.com' }, { email: 'two@example.com' }] });
  assert.equal(h.$('#switchTarget').children.length, 1);
  h.$('#switchConfirm').checked = true;
  await h.ui.doSwitch();
  assert.equal(h.calls[0].body.email, 'two@example.com');
  assert.equal(h.calls[0].body.profile_id, undefined);
});

test('extension does not offer invalid migrated identity snapshots', () => {
  const h = harness();
  h.ui.renderSwitch({ switching: { enabled: true }, current: profiles[0],
    snapshots: [profiles[0], { ...profiles[1], identity_error: 'snapshot_email_mismatch' }] });
  assert.equal(h.$('#switchTarget').children[0].value, '');
  assert.equal(h.$('#switchBtn').disabled, true);
});

test('extension import defaults to personal and forwards the selected Team identity', async () => {
  const h = harness();
  assert.match(html, /id="importIdentity"[\s\S]*?<option value="personal">/);
  h.$('#importText').value = 'one@example.com|password';
  await h.ui.doImport();
  assert.equal(h.calls[0].body.identity_type, 'personal');
  h.$('#importIdentity').value = 'team';
  await h.ui.doImport();
  assert.equal(h.calls[1].body.identity_type, 'team');
});
