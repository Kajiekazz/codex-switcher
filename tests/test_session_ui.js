const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Execute the actual session-management functions, with DOM and HTTP doubles.
// No browser, local service, account, or app-server is contacted by these tests.
const html = fs.readFileSync(path.join(__dirname, '../codex_switcher/dashboard.html'), 'utf8');
const sessionScript = html.slice(html.indexOf('  let goalState = {'), html.indexOf('  function renderSwitchToggles('));

class Element {
  constructor(tag = 'div', text = '', className = '') {
    this.tagName = tag;
    this._text = text || '';
    this.className = className || '';
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.classList = { toggle() {}, add() {}, remove() {} };
    this.value = '';
    this.hidden = true;
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(event, callback) { this.listeners[event] = callback; }
  focus() {}
}

function harness(initial = {}) {
  const elements = new Map();
  const calls = [];
  const confirmations = [];
  let accept = true;
  let answer;
  const $ = selector => {
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  };
  const context = vm.createContext({
    $, document: { createElement: tag => new Element(tag) },
    el: (tag, text, className) => new Element(tag, text, className),
    pill: (text, tone) => new Element('span', text, tone),
    empty: (title, detail) => new Element('div', title + detail),
    initials: () => 'T', formatAge: () => '刚刚', formatDuration: n => String(n),
    localStorage: { getItem: () => null, setItem() {} },
    window: { confirm: text => { confirmations.push(text); return accept; } },
    setTimeout: () => 1,
    fetch: async () => ({ json: async () => ({ processes: [] }) }),
    api: async (url, options) => {
      const body = options && JSON.parse(options.body);
      calls.push({ url, body });
      if (answer) return answer(url, body);
      if (url.startsWith('/api/goals') && !options) return {
        session_api_version: 1, enabled: true, goals: [], sessions: { total: 0 }, auto_resume: { enabled: false, thread_ids: [] },
      };
      if (url === '/api/goals/auto-resume') return { thread_ids: body.enabled ? [body.thread_id] : [] };
      if (url === '/api/goals/cancel') return { cancelled: true, skipped: body.thread_ids || [] };
      return { job: { id: 'test-job' } };
    },
  });
  vm.runInContext(sessionScript + `
    globalThis.ui = {
      goalJobThreads, canCreateGoal, goalResultLabel, autoResumeControl, goalLabel,
      renderGoals, renderGoalJob, renderGoalHistory, goalResumeBadge, sessionRow,
      toggleAutoResume, enableAutoResume, restartAndRetry, cancelGoalResume,
      submitCreateGoals, refreshGoals, runGoalResume, autoResumeSummary,
      setState(value) { goalState = value; syncAutoResume(); },
      state() { return goalState; },
      select(ids) { goalSelected.clear(); ids.forEach(id => goalSelected.add(id)); }
    };`, context);
  context.ui.setState({ backendCompatible: true, enabled: true, goals: [], sessions: { total: 0 }, autoResume: { enabled: false, threadIds: [] }, ...initial });
  return { ui: context.ui, $, calls, confirmations,
    confirm(value) { accept = value; }, respond(fn) { answer = fn; } };
}

const thread = (id, fields = {}) => ({ thread_id: id, title: id, has_goal: true, armable: true, goal_status: 'active', ...fields });

test('remaining work excludes finished and skipped threads but includes the current thread', () => {
  const { ui } = harness();
  const result = ui.goalJobThreads({ state: 'running', thread_ids: ['done', 'current', 'queued', 'skip'],
    current_thread: 'current', results: [{ thread_id: 'done' }], skipped: ['skip'] });
  assert.equal(result.current, 'current');
  assert.deepEqual(Array.from(result.remaining), ['current', 'queued']);
  assert.equal(ui.goalJobThreads({ state: 'partial', thread_ids: ['old'] }).remaining.size, 0);
});

test('rendered rows do not label completed or skipped threads as queued', () => {
  const h = harness();
  const job = { state: 'running', id: 'job', current_thread: 'current',
    thread_ids: ['done', 'current', 'queued', 'skip'], results: [{ thread_id: 'done', ok: true }], skipped: ['skip'] };
  const work = h.ui.goalJobThreads(job);
  h.ui.setState({ enabled: true, job });
  assert.doesNotMatch(h.ui.sessionRow(thread('done'), work.current, work.remaining).textContent, /等待续跑/);
  assert.doesNotMatch(h.ui.sessionRow(thread('skip'), work.current, work.remaining).textContent, /等待续跑/);
  assert.match(h.ui.sessionRow(thread('queued'), work.current, work.remaining).textContent, /等待续跑/);
  assert.match(h.ui.sessionRow(thread('current'), work.current, work.remaining).textContent, /正在续跑/);
});

test('marking an automatic target does not enable the global switch', async () => {
  const h = harness();
  await h.ui.toggleAutoResume('target', true);
  assert.equal(h.ui.state().autoResume.enabled, false);
  assert.equal(h.ui.autoResumeControl('target').label, '已标记');
  assert.match(h.$('#goalMsg').textContent, /尚未启用/);
  assert.equal(h.calls.some(call => call.url === '/api/settings'), false);
});

test('global auto-resume activation requires confirmation and sends only its setting', async () => {
  const h = harness();
  h.confirm(false);
  await h.ui.enableAutoResume();
  assert.equal(h.calls.length, 0);
  h.confirm(true);
  await h.ui.enableAutoResume();
  assert.deepEqual(h.calls[0], { url: '/api/settings', body: { confirm: true, values: { auto_resume_after_switch: true } } });
});

test('explicit retry just queues the threads again, without takeover flags or settings writes', async () => {
  const h = harness();
  await h.ui.restartAndRetry(['busy']);
  assert.deepEqual(h.calls[0], { url: '/api/goals/resume',
    body: { thread_ids: ['busy'], dry_run: false, confirm: true } });
  assert.equal(h.calls.some(call => call.url === '/api/settings'), false);
  assert.equal(h.confirmations.length, 0);
  assert.match(h.$('#goalMsg').textContent, /已重新排队 1 个目标/);
  assert.match(h.$('#goalMsg').textContent, /进度在 Codex 里看/);
});

test('stop selection ignores finished and skipped threads and targets only remaining work', async () => {
  const h = harness({ job: { state: 'running', thread_ids: ['done', 'current', 'queued', 'skip'],
    current_thread: 'current', results: [{ thread_id: 'done' }], skipped: ['skip'] } });
  h.ui.select(['done', 'queued', 'skip']);
  await h.ui.cancelGoalResume();
  assert.deepEqual(h.calls[0].body, { confirm: true, thread_ids: ['queued'] });
  assert.match(h.confirmations[0], /其他目标继续执行/);
});

test('stopping the current thread says the queued message is not withdrawn', async () => {
  const h = harness({ job: { state: 'running', thread_ids: ['current', 'queued'], current_thread: 'current' } });
  h.ui.select(['current']);
  await h.ui.cancelGoalResume();
  assert.deepEqual(h.calls[0].body, { confirm: true, thread_ids: ['current'] });
  assert.match(h.confirmations[0], /立即结束本次任务/);
  assert.match(h.confirmations[0], /不会撤回/);
});

test('read-only state blocks creation and internal sessions cannot be selected for creation', async () => {
  const h = harness({ enabled: false, sessions: { total: 1, recent: [thread('new', { has_goal: false })] } });
  h.ui.select(['new']);
  h.ui.renderGoals();
  assert.equal(h.$('#goalCreateBtn').disabled, true);
  await h.ui.submitCreateGoals();
  assert.equal(h.calls.length, 0);
  assert.equal(h.ui.canCreateGoal({ has_goal: false, internal: true }), false);
  assert.equal(h.ui.canCreateGoal({ has_goal: false, internal: false }), true);
});

test('active goals, partial jobs, cancelled results and previews have distinct labels', () => {
  const h = harness({ job: { dry_run: true } });
  assert.equal(h.ui.goalLabel('active'), '已激活');
  assert.equal(h.ui.goalResultLabel({ ok: false, cancelled: true, error: 'cancelled' }), '已取消');
  assert.equal(h.ui.goalResultLabel({ ok: false, error: 'skipped_by_user' }), '已跳过');
  assert.equal(h.ui.goalResultLabel({ ok: true, delivered: true }), '成功');
  assert.equal(h.ui.goalResultLabel({ ok: true, pending: true, delivered: false }), '已排队');
  h.ui.renderGoalHistory([{ state: 'partial', results: [{ thread_id: 'cancelled', cancelled: true }] }]);
  assert.match(h.$('#goalHistory').textContent, /部分完成/);
  assert.match(h.$('#goalHistory').textContent, /已取消/);
  assert.match(h.ui.sessionRow(thread('preview'), 'preview', new Set(['preview'])).textContent, /正在预览/);
});

test('a filtered list does not offer to delete unseen automatic marks', () => {
  const h = harness({ autoResume: { enabled: true, threadIds: ['not-in-current-page'] } });
  h.ui.renderGoals();
  assert.doesNotMatch(h.$('#goalHint').textContent, /失效|清理/);
});

test('truncated lists show loaded counts and the search boundary', () => {
  const h = harness({ sessions: { total: 800, shown: 500, truncated: true } });
  h.ui.renderGoals();
  assert.match(h.$('#goalCount').textContent, /500\/800/);
  assert.match(h.$('#goalHint').textContent, /搜索仅覆盖已加载会话/);
});

test('late status responses cannot overwrite a newer rendered state', async () => {
  const h = harness();
  const resolve = [];
  h.respond(() => new Promise(done => resolve.push(done)));
  const older = h.ui.refreshGoals();
  const newer = h.ui.refreshGoals();
  resolve[1]({ enabled: true, sessions: { total: 2 }, auto_resume: {} });
  await newer;
  resolve[0]({ enabled: false, sessions: { total: 1 }, auto_resume: {} });
  await older;
  assert.equal(h.ui.state().enabled, true);
  assert.equal(h.ui.state().sessions.total, 2);
});

test('an old backend is visibly blocked from starting work while cancellation remains available', async () => {
  const h = harness();
  h.respond(() => ({
    enabled: true, running: true,
    goals: [thread('current')], sessions: { total: 1, recent: [thread('current')] },
    job: { id: 'old-job', state: 'running', thread_ids: ['current'], current_thread: 'current' },
  }));
  await h.ui.refreshGoals();
  h.ui.select(['current']);
  h.ui.renderGoals();
  assert.match(h.$('#goalFix').textContent, /服务仍在运行旧代码/);
  assert.equal(h.$('#goalResumeBtn').disabled, true);
  assert.equal(h.$('#goalPreviewBtn').disabled, true);
  assert.equal(h.$('#goalCreateBtn').disabled, true);
  assert.equal(h.$('#goalStopBtn').disabled, false);
  const before = h.calls.length;
  await h.ui.runGoalResume(false);
  await h.ui.restartAndRetry(['current']);
  await h.ui.submitCreateGoals();
  await h.ui.enableAutoResume();
  await h.ui.toggleAutoResume('current', true);
  assert.equal(h.calls.length, before);
  assert.equal(h.confirmations.length, 0);
});

test('switch outcomes distinguish submitted, skipped and failed auto resume', () => {
  const h = harness();
  assert.match(h.ui.autoResumeSummary({ ok: true, job: { id: 'job', thread_ids: ['a'] } }), /任务已提交：job · 1 个目标/);
  assert.match(h.ui.autoResumeSummary({ ok: true, skipped: 'no_marked_goal' }), /没有标记自动续跑/);
  assert.match(h.ui.autoResumeSummary({ ok: true, skipped: 'auto_resume_disabled' }), /总开关已关闭/);
  assert.match(h.ui.autoResumeSummary({ ok: true, skipped: 'goal_resume_disabled' }), /功能未启用/);
  assert.match(h.ui.autoResumeSummary({ ok: false, error: 'callback failed' }), /自动续跑失败：callback failed/);
  assert.match(h.ui.autoResumeSummary(null), /服务未返回结果/);
  h.ui.setState({ enabled: true, running: true,
    job: { id: 'job', state: 'running', thread_ids: ['a'], current_thread: 'a' },
    autoResume: { enabled: true, last: { ok: false, skipped: 'job_running' } },
  });
  h.ui.renderGoals();
  assert.match(h.$('#goalAutoStatus').textContent, /旧任务尚未释放/);
});
