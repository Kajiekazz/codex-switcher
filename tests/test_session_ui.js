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
  setAttribute(name, value) { this[name] = value; }
  focus() {}
}

function harness(initial = {}) {
  const elements = new Map();
  const calls = [];
  const confirmations = [];
  const timers = [];
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
    setTimeout: callback => { timers.push(callback); return timers.length; },
    fetch: async () => ({ json: async () => ({ processes: [] }) }),
    api: async (url, options) => {
      const body = options && JSON.parse(options.body);
      calls.push({ url, body });
      if (answer) return answer(url, body);
      if (url.startsWith('/api/goals') && !options) return {
        session_api_version: 4, enabled: true, goals: [], sessions: { total: 0 }, auto_resume: { enabled: false, thread_ids: [] },
      };
      if (url === '/api/goals/auto-resume') return { thread_ids: body.enabled ? [body.thread_id] : [] };
      if (url === '/api/goals/cancel-queued') return { results: body.thread_ids.map(thread_id => ({ thread_id, state: 'cancelled' })) };
      return { job: { id: 'test-job' } };
    },
  });
  vm.runInContext(sessionScript + `
    globalThis.ui = {
      goalJobThreads, canCreateGoal, goalResultLabel, autoResumeControl, goalLabel,
      renderGoals, renderGoalJob, renderGoalHistory, goalResumeBadge, sessionRow,
      toggleAutoResume, enableAutoResume, restartAndRetry, cancelGoalResume,
      submitCreateGoals, refreshGoals, runGoalResume, autoResumeSummary,
      queueRow, renderResumeQueue, pauseContinuations, removeContinuations, openLongGoal, closeLongGoal, updateLongGoalControls,
      setState(value) { goalState = value; syncAutoResume(); },
      state() { return goalState; },
      select(ids) { goalSelected.clear(); ids.forEach(id => goalSelected.add(id)); }
    };`, context);
  context.ui.setState({ backendCompatible: true, enabled: true, goals: [], sessions: { total: 0 }, autoResume: { enabled: false, threadIds: [] }, ...initial });
  return { ui: context.ui, $, calls, confirmations, timers,
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
  assert.doesNotMatch(h.ui.sessionRow(thread('done'), work.current, work.remaining).textContent, /等待发送/);
  assert.doesNotMatch(h.ui.sessionRow(thread('skip'), work.current, work.remaining).textContent, /等待发送/);
  assert.match(h.ui.sessionRow(thread('queued'), work.current, work.remaining).textContent, /等待发送/);
  assert.match(h.ui.sessionRow(thread('current'), work.current, work.remaining).textContent, /正在发送/);
});

test('marking an automatic target does not enable the global switch', async () => {
  const h = harness();
  await h.ui.toggleAutoResume('target', true);
  assert.equal(h.ui.state().autoResume.enabled, false);
  assert.equal(h.ui.autoResumeControl('target').label, '切号自动恢复：已暂停（全局）');
  assert.match(h.$('#goalMsg').textContent, /总开关目前暂停/);
  assert.equal(h.calls.some(call => call.url === '/api/settings'), false);
});

test('the master switch is an explicit hot-applied pause/resume control', async () => {
  const h = harness();
  await h.ui.enableAutoResume(true);
  assert.deepEqual(h.calls[0], { url: '/api/settings', body: { confirm: true, values: { auto_resume_after_switch: true } } });
  await h.ui.enableAutoResume(false);
  assert.deepEqual(h.calls[2].body, { confirm: true, values: { auto_resume_after_switch: false } });
  assert.equal(h.confirmations.length, 0);
});

test('explicit retry just queues the threads again, without takeover flags or settings writes', async () => {
  const h = harness();
  await h.ui.restartAndRetry(['busy']);
  assert.deepEqual(h.calls[0], { url: '/api/goals/resume',
    body: { thread_ids: ['busy'], dry_run: false, confirm: true, continue_once: false } });
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
  assert.equal(h.calls[0].url, '/api/goals/cancel-queued');
});

test('cancelling a dispatching chat requests queue withdrawal without claiming turn interruption', async () => {
  const h = harness({ job: { state: 'running', thread_ids: ['current', 'queued'], current_thread: 'current' } });
  h.ui.select(['current']);
  await h.ui.cancelGoalResume();
  assert.deepEqual(h.calls[0].body, { confirm: true, thread_ids: ['current'] });
  assert.equal(h.calls[0].url, '/api/goals/cancel-queued');
  assert.doesNotMatch(h.$('#goalMsg').textContent, /已停止/);
});

test('read-only state blocks creation and internal sessions cannot be selected for creation', async () => {
  const h = harness({ enabled: false, sessions: { total: 1, recent: [thread('new', { has_goal: false })] } });
  h.ui.select(['new']);
  h.ui.renderGoals();
  h.ui.openLongGoal('new');
  assert.equal(h.$('#longGoalSave').disabled, true);
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
  assert.equal(h.ui.goalResultLabel({ ok: true, delivered: true }), '消息已出队 · 未找到对应回合');
  assert.equal(h.ui.goalResultLabel({ ok: true, pending: true, delivered: false }), '已排队');
  // The row is queued but the conversation is archived: it will never run.
  assert.equal(
    h.ui.goalResultLabel({ ok: true, pending: false, delivered: false, queue_abandoned: true }),
    '未续跑 · 会话已归档'
  );
  h.ui.renderGoalHistory([{ state: 'partial', results: [{ thread_id: 'cancelled', cancelled: true }] }]);
  assert.match(h.$('#goalHistory').textContent, /部分完成/);
  assert.match(h.$('#goalHistory').textContent, /已取消/);
  assert.match(h.ui.sessionRow(thread('preview'), 'preview', new Set(['preview'])).textContent, /正在预览/);
});

test('queue disappearance does not claim that a turn started', () => {
  const h = harness();
  const queued = h.ui.goalResumeBadge({ ok: true, pending: true, delivered: false, finished_at: '2026-11-02T09:26:59Z', watch: { attempts: 2 } });
  assert.match(queued.textContent, /已排队 · 等桌面端取走/);
  assert.match(queued.textContent, /重试 2 次/);
  assert.doesNotMatch(queued.textContent, /已续跑/);
  assert.equal(queued.className, 'warn');

  // Repeated wakes that never reach the app are a different problem than
  // "the app is just busy": say so after a few retries.
  const silent = h.ui.goalResumeBadge({ ok: true, pending: true, delivered: false, finished_at: '2026-11-02T09:26:59Z', watch: { attempts: 4 } });
  assert.match(silent.textContent, /已排队 · 桌面端未响应唤醒/);
  assert.match(silent.title, /PrimaryRuntime/);
  assert.doesNotMatch(silent.textContent, /已续跑/);

  const taken = h.ui.goalResumeBadge({ ok: true, pending: false, delivered: true, finished_at: '2026-11-02T09:26:59Z' });
  assert.match(taken.textContent, /消息已出队 · 未找到对应回合/);
  assert.doesNotMatch(taken.textContent, /已续跑/);
  assert.match(taken.title, /不算执行成功/);
  assert.equal(taken.className, 'warn');

  const expired = h.ui.goalResumeBadge({ ok: true, pending: false, delivered: false, queue_expired: true });
  assert.match(expired.textContent, /桌面端未取走/);
  assert.equal(expired.className, 'bad');

  // Archived conversation: the row can never be taken, so don't imply waiting.
  const archived = h.ui.goalResumeBadge({ ok: true, pending: false, delivered: false, queue_abandoned: true });
  assert.match(archived.textContent, /未续跑 · 会话已归档/);
  assert.doesNotMatch(archived.textContent, /已排队|已续跑/);
  assert.match(archived.title, /取消归档/);
});

test('the header badge counts messages still waiting in the queue', () => {
  const h = harness({ queueWatch: { pending: 2 } });
  h.ui.renderGoals();
  assert.match(h.$('#goalResumeBadge').textContent, /可继续会话/);
  assert.match(h.$('#goalResumeBadge').textContent, /队列 2/);
});

test('a filtered list does not offer to delete unseen automatic marks', () => {  const h = harness({ autoResume: { enabled: true, threadIds: ['not-in-current-page'] } });
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

test('an old backend blocks new mutations until the service is updated', async () => {
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
  h.ui.openLongGoal('current');
  assert.equal(h.$('#longGoalContinue').disabled, true);
  assert.equal(h.$('#goalStopBtn').disabled, true);
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

test('an ordinary chat requests goal preparation through the resume endpoint without a separate create call', async () => {
  const h = harness({ sessions: { total: 1, recent: [thread('ordinary', { has_goal: false, armable: false })] } });
  await h.ui.runGoalResume(false, ['ordinary']);
  assert.deepEqual(h.calls[0], { url: '/api/goals/resume', body: { thread_ids: ['ordinary'], dry_run: false, confirm: true, continue_once: false } });
  assert.equal(h.calls.some(call => call.url === '/api/goals/create'), false);
  assert.equal(h.confirmations.length, 0);
});

test('the queue keeps goal preparation warnings separate from continuation failures', () => {
  const h = harness();
  const row = h.ui.queueRow({ thread_id: 'fallback', title: 'fallback', label: '已排队', group: 'waiting',
    result: { ok: true, goal_creation_error: 'goal store unavailable' } });
  assert.match(row.textContent, /已排队/);
  assert.match(row.textContent, /补建长期目标失败，本次使用普通消息续跑：goal store unavailable/);
  assert.doesNotMatch(row.textContent, /投递失败/);
});

test('retrying a paused goal uses recovery even when its old request was continue-once', async () => {
  const h = harness({ sessions: { total: 1, recent: [thread('paused', { goal_status: 'paused' })] } });
  const row = h.ui.queueRow({ thread_id: 'paused', label: '需处理', group: 'attention', goal_status: 'paused',
    can_retry: true, continue_once: true });
  const actions = row.children.find(child => child.className === 'row-actions');
  const resume = actions.children.find(child => child.textContent === '继续');
  await resume.listeners.click();
  assert.equal(h.calls[0].body.continue_once, false);
});

test('a goal active flag does not show a running turn or a stop action', () => {
  const h = harness();
  const row = h.ui.sessionRow(thread('idle'), null, new Set(), 'recent');
  assert.doesNotMatch(row.textContent, /会话运行中|前往 Codex 停止/);
  const running = h.ui.sessionRow(thread('running', { execution: { state: 'running' } }), null, new Set(), 'recent');
  assert.match(running.textContent, /会话运行中.*前往 Codex 停止/);
});

test('resuming a long-running goal is a separate explicit operation', async () => {
  const h = harness({ sessions: { total: 1, recent: [thread('goal', { goal_status: 'paused' })] } });
  await h.ui.runGoalResume(false, ['goal'], false);
  assert.equal(h.calls[0].body.continue_once, false);
  assert.equal(h.confirmations.length, 0);
});

test('queue cancellation remains available after the dispatch job has finished', async () => {
  const h = harness({ running: false, lastResume: { pending: { owned_queue: true, pending: true } } });
  h.ui.renderGoals();
  assert.equal(h.$('#goalStopBtn').disabled, false);
  await h.ui.cancelGoalResume();
  assert.deepEqual(h.calls[0], { url: '/api/goals/cancel-queued', body: { confirm: true, thread_ids: ['pending'] } });
});

test('a matched queue turn replaces transport status with running and then its terminal result', () => {
  const h = harness();
  const result = { ok: true, delivered: true, execution: {
    state: 'running', turn_id: 'this-turn', started_at: '2026-11-02T12:57:40Z', correlation: 'queue_turn_message',
  } };
  const running = h.ui.goalResumeBadge(result);
  assert.match(running.textContent, /正在续跑/);
  assert.doesNotMatch(running.textContent, /出队|待确认/);
  assert.match(running.title, /this-turn/);
  assert.match(running.title, /实际开始/);
  assert.equal(h.ui.goalResultLabel({ ...result, execution: { ...result.execution, state: 'completed' } }), '本次续跑已结束');
  assert.equal(h.ui.goalResultLabel({ ...result, execution: { ...result.execution, state: 'failed' } }), '本次续跑失败');
  assert.equal(h.ui.goalResultLabel({ ...result, execution: { ...result.execution, state: 'interrupted' } }), '本次续跑已中断');
});

test('missing execution evidence becomes a warning with a concrete action', () => {
  const h = harness();
  const badge = h.ui.goalResumeBadge({ ok: true, delivered: true, execution: { state: 'unconfirmed' } });
  assert.match(badge.textContent, /未找到本次续跑回合/);
  assert.equal(badge.className, 'warn');
  assert.match(badge.title, /90 秒/);
  assert.match(badge.title, /打开会话核对/);
  assert.doesNotMatch(badge.textContent, /成功|正在续跑/);
});

test('receipt monitoring keeps polling after the queue and dispatch job are both done', async () => {
  const h = harness();
  h.respond(() => ({ session_api_version: 4, enabled: true, goals: [],
    sessions: { total: 0 }, execution_watch: { active: 1 }, queue_watch: { pending: 0 } }));
  await h.ui.refreshGoals();
  assert.equal(h.ui.state().executionWatch.active, 1);
  assert.equal(h.timers.length, 1);
});

test('queue actions show pending manual stop without claiming a completed interruption', async () => {
  const h = harness({ resumeQueue: { items: [{ thread_id: 'chat', title: 'target', group: 'running',
    state: 'running', label: '正在续跑', can_pause: true }], counts: { running: 1 } } });
  h.respond((url, body) => url === '/api/goals/pause'
    ? { results: [{ thread_id: 'chat', goal_paused: true, needs_manual_stop: true }] }
    : { session_api_version: 4, enabled: true, resume_queue: { items: [], counts: {} } });
  await h.ui.pauseContinuations(['chat', 'chat']);
  assert.deepEqual(h.calls[0], { url: '/api/goals/pause', body: { confirm: true, thread_ids: ['chat'] } });
  assert.match(h.$('#resumeQueueMsg').textContent, /当前回合仍需前往 Codex 停止/);
  assert.doesNotMatch(h.$('#resumeQueueMsg').textContent, /已停止/);
  assert.equal(h.confirmations.length, 0);
});

test('the queue uses the same automatic recovery state as the session row', () => {
  const h = harness({ autoResume: { enabled: true, threadIds: ['chat'] } });
  const row = h.ui.queueRow({ thread_id: 'chat', title: 'target', group: 'running', label: '正在续跑',
    needs_manual_stop: true, attempts: [] });
  assert.match(row.textContent, /切号自动恢复：已开启/);
  assert.match(row.textContent, /前往 Codex 停止/);
  assert.equal(h.ui.autoResumeControl('chat').label, '切号自动恢复：已开启');
});

test('the unified goal entry reuses the existing objective and never calls create', async () => {
  const h = harness({ goals: [{ thread_id: 'goal', status: 'paused', objective: 'keep this objective' }],
    sessions: { total: 1, recent: [thread('goal', { goal_status: 'paused' })] } });
  h.ui.openLongGoal('goal');
  assert.equal(h.$('#longGoalObjective').value, 'keep this objective');
  assert.equal(h.$('#longGoalObjective').readOnly, true);
  assert.equal(h.$('#longGoalSave').hidden, true);
  await h.ui.submitCreateGoals(true);
  assert.equal(h.calls[0].url, '/api/goals/resume');
  assert.equal(h.calls[0].body.continue_once, false);
  assert.equal(h.calls.some(call => call.url === '/api/goals/create'), false);
});

test('creating a goal through the unified entry saves paused before an explicit start', async () => {
  const h = harness({ sessions: { total: 1, recent: [thread('ordinary', { has_goal: false })] } });
  let created = false;
  h.respond((url, body) => {
    if (url === '/api/goals/create') { created = true; return { created: [{ thread_id: 'ordinary' }] }; }
    if (url === '/api/goals/resume') return { job: { id: 'test-job' } };
    return { session_api_version: 4, enabled: true,
      goals: created ? [{ thread_id: 'ordinary', status: 'paused', objective: 'concrete outcome' }] : [],
      sessions: { total: 1, recent: [thread('ordinary', { has_goal: false })] } };
  });
  h.ui.openLongGoal('ordinary');
  await h.ui.submitCreateGoals(true);
  assert.equal(h.calls.length, 0); // Missing objective is a visible validation error.
  h.$('#longGoalObjective').value = 'concrete outcome';
  await h.ui.submitCreateGoals(true);
  assert.deepEqual(h.calls[0].body, { thread_ids: ['ordinary'], objective: 'concrete outcome', status: 'paused', confirm: true });
  assert.equal(h.calls.filter(call => call.url === '/api/goals/resume').length, 1);
});

test('a pending owned message blocks another continuation request', async () => {
  const h = harness({ pendingResume: { ordinary: { pending: true, owned_queue: true } },
    sessions: { total: 1, recent: [thread('ordinary', { has_goal: false })] } });
  await h.ui.runGoalResume(false, ['ordinary']);
  assert.equal(h.calls.length, 0);
  assert.match(h.$('#goalMsg').textContent, /已有续跑请求在排队/);
});

test('the managed queue continues polling across goal turns after the first receipt is terminal', async () => {
  const h = harness();
  h.respond(() => ({ session_api_version: 4, enabled: true, resume_queue: { items: [], counts: {}, active: 1 },
    execution_watch: { active: 0 } }));
  await h.ui.refreshGoals();
  assert.equal(h.timers.length, 1);
});

test('only the unified goal entry exists in the page, and the master switch is in settings', () => {
  assert.doesNotMatch(html, /id="goalAutoMaster"|id="goalGoalResumeBtn"|id="goalCreateBtn"/);
  assert.match(html, /id="longGoalModal"/);
  assert.match(html, /id="resumeQueue"/);
});

test('paused continuations and their continue action remain visible outside collapsed history', () => {
  const h = harness({ resumeQueue: { items: [{ thread_id: 'paused', title: 'paused target',
    group: 'recent', state: 'paused', label: '已暂停', can_resume: true, can_remove: true }], counts: { recent: 1 } } });
  h.ui.renderResumeQueue();
  assert.match(h.$('#resumeQueueList').textContent, /已暂停/);
  assert.match(h.$('#resumeQueueList').textContent, /继续/);
  assert.match(h.$('#resumeQueueList').textContent, /删除/);
  assert.doesNotMatch(h.$('#resumeQueueRecentList').textContent, /paused target/);
});

test('delete requests targeted force-stop and leaves a failed stop visible', async () => {
  const h = harness();
  h.respond((url) => url === '/api/goals/remove'
    ? { results: [{ thread_id: 'chat', removed: false, error: 'desktop_stop_not_confirmed' }] }
    : { session_api_version: 4, enabled: true });
  await h.ui.removeContinuations(['chat']);
  assert.deepEqual(h.calls[0], { url: '/api/goals/remove', body: { confirm: true, thread_ids: ['chat'] } });
  assert.match(h.$('#resumeQueueMsg').textContent, /未删除.*desktop_stop_not_confirmed/);
});
