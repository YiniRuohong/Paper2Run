import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import vm from 'node:vm';
import * as helpers from '../src/features/autonomous/config.ts';
import * as stages from '../src/features/autonomous/stages.ts';
import * as errors from '../src/features/autonomous/errors.ts';
import * as flowTimeline from '../src/features/flow/timeline.ts';
import * as flowDraft from '../src/features/flow/draft.ts';
import * as recovery from '../src/features/autonomous/recovery.ts';
import { ApiError } from '../src/api-client/client.ts';

// Shallow-render TaskPage in a VM: React hooks and every child component are
// stubbed, so these tests exercise only the component's own state machine —
// polling, the three task states, approval gating, recovery gating, and
// isolation of late responses after the task scope changes.

const box = name => function Stub(props) { return { type: name, props: props ?? {}, children: props?.children }; };

async function harness(options = {}) {
  const source = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const slots = []; let cursor = 0; let effects = [];
  const timers = [];
  const calls = { control: [], saveProfile: [], decide: [], task: 0 };
  const makeTask = (id, over = {}) => ({
    id, project_id: 'A', objective: `goal ${id}`, state: 'paused', usage: {}, report: null, error: null,
    pending_decision: null, created_at: '', updated_at: '2026-11-05T00:00:00Z',
    config_snapshot: helpers.defaultConfig('A'), ...over,
  });
  const api = {
    initializeSession: async () => {},
    capabilities: async () => [],
    profiles: async () => options.profiles ?? [],
    tasks: async () => [makeTask('ta'), makeTask('tb')],
    task: async id => { calls.task += 1; return makeTask(id, options.taskOver); },
    events: async (_id, after) => {
      const all = options.events ?? [];
      return { events: all.filter(e => e.sequence > after), next_cursor: all.length ? Math.max(...all.map(e => e.sequence)) : after };
    },
    files: async () => ({ items: [], truncated: false, workspace_available: false }),
    saveProfile: async body => { calls.saveProfile.push(body); const { api_key, ...saved } = body; return { ...saved, secret_configured: !!api_key }; },
    control: async (id, action, text) => { calls.control.push({ id, action, text }); if (options.controlError) throw options.controlError; return makeTask(id); },
    decide: async (id, decisionId, action, note) => { calls.decide.push({ id, decisionId, action, note }); if (options.decideError) throw options.decideError; return makeTask(id, { state: 'queued' }); },
    exportTask: async () => {},
  };
  // The header uses the project (paper) name; without one it falls back to the objective.
  const papers = { getProject: async id => { if (!options.projectName) throw new Error('no name'); return { id, name: options.projectName }; } };
  const react = {
    Fragment: 'fragment',
    createElement: (type, props, ...children) => typeof type === 'function' ? type({ ...props, children }) : ({ type, props: props ?? {}, children }),
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || deps.some((d, j) => d !== slots[i].deps[j])) { slots[i]?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); }); } },
  };
  const modules = {
    react,
    './config': helpers,
    './stages': stages,
    './errors': errors,
    '../flow/timeline': flowTimeline,
    '../flow/draft': flowDraft,
    '../flow/RailHead': { default: box('RailHead') },
    './recovery': recovery,
    '../../api-client/autonomous': { createAutonomousApi: () => api, ApiError },
    '../../api-client/papers': { createPapersApi: () => papers },
    // StepFrame renders its content and the footer controls (actions) of the task page.
    '../flow/StepFrame': { default: props => ({ type: 'StepFrame', props, children: [props.children, props.actions] }) },
    '../../ui/workbench/Workbench': { Workbench: box('Workbench') },
    '../../ui/Card': { Badge: box('Badge'), Card: box('Card'), CardHeader: box('CardHeader') },
    '../../ui/Disclosure': { Disclosure: box('Disclosure') },
    '../../ui/Icon': { Icon: () => null },
    './ApprovalCard': { ApprovalCard: box('ApprovalCard') },
    './CandidateCard': { CandidateCard: box('CandidateCard') },
    './ActivityViews': { ProgressNotes: box('ProgressNotes'), SpecialistCalls: box('SpecialistCalls') },
    './ReportView': { ReportSummary: box('ReportSummary'), ReportDetails: box('ReportDetails') },
    './FiguresView': { FiguresView: box('FiguresView') },
    './UsageView': { UsageView: box('UsageView') },
    './autonomous.css': {},
  };
  const exports = {};
  const context = {
    exports, React: react,
    require: name => modules[name] ?? {},
    window: { location: { hash: '', reload() {} } },
    AbortController, TextEncoder, URLSearchParams,
    setTimeout: fn => { timers.push(fn); return timers.length; },
    clearTimeout() {},
  };
  vm.runInNewContext(code, context);
  let tree; let currentTaskId = 'ta';
  const render = () => { cursor = 0; tree = exports.default({ taskId: currentTaskId }); const pending = effects; effects = []; pending.forEach(fn => fn()); return tree; };
  const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); render(); };
  const nodes = () => {
    const result = [];
    const visit = n => { if (Array.isArray(n)) return n.forEach(visit); if (n && typeof n === 'object') { result.push(n); visit(n.children); } };
    visit(tree);
    return result;
  };
  render(); await settle(); await settle();
  return {
    nodes, calls, api, render, settle, makeTask,
    text: () => JSON.stringify(tree),
    poll: () => { const fn = timers.shift(); if (fn) fn(); },
    switchTask: async id => { currentTaskId = id; await settle(); },
    button: label => nodes().find(n => n.type === 'button' && (n.children ?? []).flat(Infinity).includes(label)),
  };
}

test('paused task routes resume and appended instructions through control', async () => {
  const h = await harness();
  const workbench = h.nodes().find(n => n.type === 'Workbench');
  assert.equal(workbench.props.title, 'goal ta');
  assert.equal(workbench.props.status.text, '已暂停');
  assert.equal(workbench.props.status.tone, 'warn');
  const resume = h.button('继续');
  assert.equal(resume.props.disabled, false);
  resume.props.onClick(); await h.settle();
  assert.deepEqual(h.calls.control[0], { id: 'ta', action: 'resume', text: undefined });

  const textarea = h.nodes().find(n => n.type === 'textarea');
  textarea.props.onChange({ target: { value: 'do x' } }); h.render();
  const send = h.button('发送指令');
  assert.equal(send.props.disabled, false);
  send.props.onClick(); await h.settle();
  const instruction = h.calls.control[1];
  assert.equal(instruction.id, 'ta');
  assert.equal(instruction.text, 'do x');
  assert.ok(!['pause', 'resume', 'stop'].includes(instruction.action), 'an appended instruction is its own control action');
  assert.equal(instruction.action.length, 7);
  assert.equal(h.nodes().find(n => n.type === 'textarea').props.value, '', 'the compose box clears after sending');
});

test('a pending protocol decision shows the approval card and gates resume behind it', async () => {
  const decision = { id: 'dec-1', kind: 'protocol_approval', proposed: { protocol: { metrics: ['accuracy'] }, sha256: 'ab'.repeat(32) }, changed: ['metrics'], reason: 'review' };
  const h = await harness({ taskOver: { pending_decision: decision } });
  const card = h.nodes().find(n => n.type === 'ApprovalCard');
  assert.ok(card, 'approval card renders for a pending protocol decision');
  assert.equal(card.props.approval.id, 'dec-1');
  const resume = h.button('继续');
  assert.equal(resume.props.disabled, true);
  assert.equal(resume.props.title, '请先处理上方决策');
  card.props.onApprove(); await h.settle();
  assert.deepEqual(h.calls.decide[0], { id: 'ta', decisionId: 'dec-1', action: 'approve', note: undefined });
  assert.equal(h.calls.control.length, 0, 'approval uses the decision endpoint, not resume');
});

test('recovery_required gates resume behind key re-injection, then unlocks it', async () => {
  const profile = { id: 'main', model: 'm', base_url: 'https://example.test', api: 'openai-completions' };
  const config = { ...helpers.defaultConfig('A'), model: { provider_profile: 'main', credential_ref: 'profile:main' } };
  const events = [
    { sequence: 1, type: 'task_created', payload: {}, created_at: '' },
    { sequence: 2, type: 'recovery_required', payload: {}, created_at: '' },
    { sequence: 3, type: 'application_exit', payload: { background_jobs_retained: ['job-1'] }, created_at: '' },
  ];
  const h = await harness({ events, profiles: [profile], taskOver: { config_snapshot: config } });
  assert.ok(h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'), 'recovery card renders');
  const resume = h.button('继续');
  assert.equal(resume.props.disabled, true);
  assert.equal(resume.props.title, '请先重新输入模型密钥');

  const keyInput = h.nodes().find(n => n.type === 'input' && n.props.type === 'password');
  keyInput.props.onChange({ target: { value: 'secret-key' } }); h.render();
  const inject = h.button('注入密钥');
  assert.equal(inject.props.disabled, false);
  inject.props.onClick(); await h.settle();
  assert.equal(h.calls.saveProfile.length, 1);
  assert.equal(h.calls.saveProfile[0].id, 'main');
  assert.equal(h.calls.saveProfile[0].api_key, 'secret-key');

  assert.ok(!h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'), 'recovery card clears after injection');
  assert.equal(h.button('继续').props.disabled, false);
});

const frozenProfile = { id: 'main', model: 'm', base_url: 'https://example.test', api: 'openai-completions', auth_mode: 'api-key', secret_configured: true };
const frozenConfig = { ...helpers.defaultConfig('A'), model: { provider_profile: 'main', credential_ref: 'profile:main' }, model_profile: frozenProfile };

test('after a restart a paused task recovers from the frozen profile and re-reads profiles', async () => {
  // Paused before the restart: no recovery_required event, and the live profile store is empty.
  const options = { profiles: [], taskOver: { config_snapshot: frozenConfig } };
  const h = await harness(options);
  assert.ok(h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'), 'recovery card renders without an event');
  const keyInput = h.nodes().find(n => n.type === 'input' && n.props.type === 'password');
  assert.equal(keyInput.props.disabled, false, 'the frozen profile makes the key field usable');
  keyInput.props.onChange({ target: { value: 'sk-again' } }); h.render();
  let reads = 0;
  h.api.profiles = async () => { reads += 1; return [{ ...frozenProfile }]; };
  h.button('注入密钥').props.onClick(); await h.settle(); await h.settle();
  const saved = h.calls.saveProfile[0];
  assert.deepEqual([saved.id, saved.base_url, saved.model, saved.api, saved.api_key], ['main', 'https://example.test', 'm', 'openai-completions', 'sk-again']);
  assert.ok(reads >= 1, 'profiles are re-read after injection');
  assert.ok(!h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'));
  assert.equal(h.button('继续').props.disabled, false);
});

test('a CREDENTIAL_REQUIRED resume error opens the recovery card with guidance', async () => {
  const h = await harness({ profiles: [{ ...frozenProfile }], taskOver: { config_snapshot: frozenConfig }, controlError: new ApiError('reconfigure', 409, 'CREDENTIAL_REQUIRED', 'req-c') });
  assert.ok(!h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'));
  h.button('继续').props.onClick(); await h.settle();
  assert.ok(h.nodes().some(n => n.type === 'Card' && n.props.className === 'auto-recovery'));
  assert.match(JSON.stringify(h.nodes().find(n => n.type === 'div' && n.props.role === 'alert').children), /需要恢复.*CREDENTIAL_REQUIRED · req-c/);
});

test('a late poll response cannot overwrite the task after switching tasks', async () => {
  const h = await harness();
  let release;
  h.api.task = id => id === 'ta'
    ? new Promise(resolve => { release = resolve; })
    : Promise.resolve(h.makeTask(id, { state: 'running' }));
  h.poll(); // this round of ta polling hangs on api.task
  await h.switchTask('tb');
  release(h.makeTask('ta', { state: 'stopped' }));
  await h.settle();
  const workbench = h.nodes().find(n => n.type === 'Workbench');
  assert.equal(workbench.props.title, 'goal tb', 'the late ta response must not replace the tb view');
  assert.equal(workbench.props.status.text, '运行中');
});

test('a task with an outcome is finished: resume is disabled with the reason and pause/stop copy is gone', async () => {
  for (const state of ['paused', 'stopped']) {
    const h = await harness({ taskOver: { state, outcome: 'completed_with_issues', repair_round: 3 } });
    const workbench = h.nodes().find(n => n.type === 'Workbench');
    assert.equal(workbench.props.status.text, '完成，有未解决问题', state);
    assert.equal(workbench.props.status.tone, 'warn', state);
    const resume = h.button('继续');
    assert.equal(resume.props.disabled, true, state);
    assert.equal(resume.props.title, '任务已有结局，不能继续', state);
    const text = h.text();
    assert.doesNotMatch(text, /后台训练作业可能仍在运行|暂停不会停止远端计费|停止任务不会停止远端实例计费/, state);
    assert.match(text, /已暂停|已停止/, 'the lifecycle state stays visible as secondary text');
    assert.ok(h.nodes().some(n => n.type === 'CandidateCard'), 'the candidate review card is on the page');
  }
});

test('safety termination shows the bad callout with its code and keeps export available', async () => {
  const events = [{ sequence: 1, type: 'safety_blocked', payload: { code: 'ISOLATION_PROBE_FAILED', message: 'probe', outcome_snapshot_sha256: null }, created_at: '' }];
  const h = await harness({ events, taskOver: { state: 'stopped', outcome: 'blocked_by_safety' } });
  const callout = h.nodes().find(n => n.type === 'div' && /tone-bad/.test(n.props.className ?? '') && JSON.stringify(n.children).includes('容器隔离校验失败，任务已安全终止，不能恢复。'));
  assert.ok(callout, 'safety callout renders');
  assert.match(JSON.stringify(callout.children), /ISOLATION_PROBE_FAILED/);
  assert.equal(h.button('导出 ZIP').props.disabled, false);
  // A safety_blocked event alone (outcome not yet persisted) also shows it.
  const early = await harness({ events, taskOver: { state: 'stopping', outcome: null } });
  assert.match(early.text(), /容器隔离校验失败，任务已安全终止，不能恢复。/);
});

test('LEGACY_TASK and TASK_FINISHED errors get a specific explanation and keep code and request_id', async () => {
  const h = await harness({ controlError: new ApiError('raw', 409, 'LEGACY_TASK', 'req-9') });
  h.button('继续').props.onClick(); await h.settle();
  const alert = h.nodes().find(n => n.type === 'div' && n.props.role === 'alert');
  const text = JSON.stringify(alert.children);
  assert.match(text, /该任务创建于新版 harness 之前，只能导出/);
  assert.match(text, /LEGACY_TASK/);
  assert.match(text, /req-9/);
});

const pendingDecision = { id: 'dec-1', kind: 'protocol_approval', proposed: { protocol: { metrics: ['f1'] }, sha256: 'cd'.repeat(32) }, changed: ['metrics'], reason: 'switch metric' };

test('a stopped or finished task never shows 需要你决定, even with a leftover decision', async () => {
  for (const over of [{ state: 'stopped' }, { state: 'cancelled' }, { state: 'paused', outcome: 'completed_with_issues' }]) {
    const h = await harness({ taskOver: { ...over, pending_decision: pendingDecision } });
    assert.ok(!h.nodes().some(n => n.type === 'ApprovalCard'), JSON.stringify(over));
    if (!over.outcome) assert.equal(h.nodes().find(n => n.type === 'Workbench').props.status.tone, 'idle', JSON.stringify(over));
  }
});

test('the approval card has no dead 先暂停 action; pause stays on the main action bar', async () => {
  const h = await harness({ taskOver: { pending_decision: pendingDecision } });
  assert.equal('onPause' in h.nodes().find(n => n.type === 'ApprovalCard').props, false);
  const card = await readFile(new URL('../src/features/autonomous/ApprovalCard.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(card, /先暂停|onPause/);
  assert.ok(h.button('暂停'), 'the main action bar keeps 暂停');
});

test('暂停 is available for queued and running tasks, as control.py allows', async () => {
  for (const state of ['queued', 'running']) {
    const h = await harness({ taskOver: { state } });
    const pause = h.button('暂停');
    assert.equal(pause.props.disabled, false, state);
    pause.props.onClick(); await h.settle();
    assert.deepEqual(h.calls.control.at(-1), { id: 'ta', action: 'pause', text: undefined }, state);
  }
  for (const state of ['paused', 'stopping', 'stopped']) {
    const h = await harness({ taskOver: { state } });
    assert.equal(h.button('暂停').props.disabled, true, state);
    if (state !== 'stopped') assert.match(h.button('暂停').props.title, /排队中或运行中/, state);
  }
});

test('approve and reject go through the explicit decision endpoint, never through resume', async () => {
  const h = await harness({ taskOver: { pending_decision: pendingDecision } });
  const card = h.nodes().find(n => n.type === 'ApprovalCard');
  card.props.onReject('keep accuracy'); await h.settle();
  assert.deepEqual(h.calls.decide[0], { id: 'ta', decisionId: 'dec-1', action: 'reject', note: 'keep accuracy' });
  assert.ok(!h.nodes().some(n => n.type === 'ApprovalCard'), 'the returned task has no pending decision, so the card goes away');
  assert.match(h.text(), /已拒绝方案变更/);
  const approve = await harness({ taskOver: { pending_decision: pendingDecision } });
  approve.nodes().find(n => n.type === 'ApprovalCard').props.onApprove(); await approve.settle();
  assert.deepEqual(approve.calls.decide[0], { id: 'ta', decisionId: 'dec-1', action: 'approve', note: undefined });
  assert.equal(h.calls.control.length + approve.calls.control.length, 0, 'no approve:<id> resume message is sent');
});

test('a decision that was already handled or expired refreshes the task instead of showing an error', async () => {
  for (const code of ['INVALID_STATE', 'DECISION_MISMATCH']) {
    const h = await harness({ taskOver: { pending_decision: pendingDecision }, decideError: new ApiError('stale', 409, code, 'req-r') });
    const before = h.calls.task;
    h.nodes().find(n => n.type === 'ApprovalCard').props.onApprove(); await h.settle();
    assert.ok(h.calls.task > before, `${code}: the task is re-read`);
    assert.ok(!h.nodes().some(n => n.type === 'div' && n.props.role === 'alert'), `${code}: no error callout`);
    assert.match(h.text(), /该决定已被处理或已过期，已刷新任务/, code);
  }
  const other = await harness({ taskOver: { pending_decision: pendingDecision }, decideError: new ApiError('boom', 500, 'INTERNAL', 'req-x') });
  other.nodes().find(n => n.type === 'ApprovalCard').props.onApprove(); await other.settle();
  assert.match(JSON.stringify(other.nodes().find(n => n.type === 'div' && n.props.role === 'alert').children), /INTERNAL/);
});

test('stop stays available on a paused task with an outcome and says what it does', async () => {
  const h = await harness({ taskOver: { state: 'paused', outcome: 'completed_with_issues' } });
  const stop = h.button('停止');
  assert.equal(stop.props.disabled, false);
  assert.equal(stop.props.title, '结局已定；停止会终止仍保留的后台作业');
  assert.ok(h.nodes().some(n => n.type === 'span' && n.props.className === 'ui-help' && (n.children ?? []).flat(Infinity).includes('结局已定；停止会终止仍保留的后台作业')));
  const running = await harness({ taskOver: { state: 'running' } });
  assert.equal(running.button('停止').props.title, '停止任务（不会停止远端实例计费）');
});

const note = summary => ({ sequence: 1, type: 'progress_noted', created_at: '2026-11-05T00:00:00Z', payload: { phase: 'execute', summary } });
const flat = node => (node?.children ?? []).flat(Infinity).map(x => typeof x === 'string' ? x : x && typeof x === 'object' ? flat(x) : '').join('');

test('the status card says 运行中 and 正在 only while the task is running without an outcome', async () => {
  const cases = [
    [{ state: 'running' }, '运行中', '正在：跑基线'],
    [{ state: 'queued' }, helpers.stateLabel('queued'), '最近进度：跑基线'],
    [{ state: 'paused' }, '已暂停', '最近进度：跑基线'],
    [{ state: 'running', outcome: 'completed_with_issues' }, '完成，有未解决问题', '最近进度：跑基线'],
  ];
  for (const [over, head, line] of cases) {
    const h = await harness({ taskOver: over, events: [note('跑基线')] });
    const status = h.nodes().find(n => n.type === 'div' && n.props.className === 'auto-live-status');
    const b = h.nodes().find(n => n.type === 'b');
    assert.equal(flat(b), head, over.state);
    assert.ok(flat(status).includes(line), `${over.state}/${over.outcome}: ${line} in ${flat(status)}`);
    if (head !== '运行中') assert.ok(!flat(status).replace(/（生命周期：[^）]*）/, '').includes('运行中'), 'no 运行中 claim outside the running state (the lifecycle suffix may name it)');
  }
});

test('the 连接模型 step names the connection, falling back to the profile id', async () => {
  const config = { ...helpers.defaultConfig('A'), model: { provider_profile: 'model-3f2a', credential_ref: 'profile:model-3f2a' } };
  const profile = { id: 'model-3f2a', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-max', api: 'openai-completions', secret_configured: true };
  const named = await harness({ taskOver: { config_snapshot: config }, profiles: [profile] });
  const stagesOf = h => h.nodes().find(n => n.type === 'Workbench').props.stages;
  assert.equal(stagesOf(named).find(st => st.id === 'model').summary, 'dashscope.aliyuncs.com · qwen-max');
  const unknown = await harness({ taskOver: { config_snapshot: config }, profiles: [] });
  assert.equal(stagesOf(unknown).find(st => st.id === 'model').summary, 'model-3f2a');
});

test('the task page header names the paper like the setup steps, and the objective moves into the summary', async () => {
  const h = await harness({ projectName: 'Gaussian Error Linear Units (GELUs)' });
  const workbench = h.nodes().find(n => n.type === 'Workbench');
  assert.equal(workbench.props.title, 'Gaussian Error Linear Units (GELUs)');
  assert.equal(workbench.props.bareLive, true, 'no progress tab or download button in the centre column');
  const frame = h.nodes().find(n => n.type === 'StepFrame');
  assert.ok(frame, 'the stage is shown in the shared step frame');
  assert.match(frame.props.guide, /不是程序判定/);
  assert.ok(h.button('继续') && h.button('导出 ZIP'), 'controls sit in the pinned footer');
  assert.match(h.text(), /goal ta/, 'the objective is still on the page');
});

test('the centre column leads with conclusions and figures; process and raw logs stay folded', async () => {
  const report = { status: 'not_comparable' };
  const error = { code: 'BUDGET_EXHAUSTED', message: 'cumulative task budget exhausted' };
  const h = await harness({ taskOver: { state: 'stopped', outcome: 'not_comparable', report, error } });
  const all = h.nodes();
  const at = type => all.findIndex(n => n.type === type);
  const details = all.find(n => n.type === 'Disclosure' && /详细记录/.test(n.props.title));
  assert.ok(details, 'a folded 详细记录 section exists');
  const inside = [];
  const visit = n => { if (Array.isArray(n)) return n.forEach(visit); if (n && typeof n === 'object') { inside.push(n); visit(n.children); } };
  visit(details.children);
  assert.ok(at('ReportSummary') >= 0 && at('ReportSummary') < all.indexOf(details), 'outcome and conclusion come first');
  assert.ok(at('FiguresView') >= 0 && at('FiguresView') < all.indexOf(details), 'figures are on the page, not folded');
  assert.ok(inside.some(n => n.type === 'ReportDetails'), 'gate, observations and narrative are folded');
  assert.ok(!all.some(n => n.type === 'ReportDetails' && !inside.includes(n)), 'report details appear only inside the fold');
  assert.match(h.text(), /任务预算/, 'a known error code reads as a plain sentence');
  const rawError = all.find(n => n.type === 'Disclosure' && n.props.title === '原始错误');
  assert.ok(rawError, 'the raw error JSON is folded');
  assert.equal(h.button('发送指令'), undefined, 'a finished task has no instruction composer');
});
