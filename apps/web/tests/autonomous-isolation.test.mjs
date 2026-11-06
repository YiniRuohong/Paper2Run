import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import vm from 'node:vm';
import * as helpers from '../src/features/autonomous/config.ts';
import * as stages from '../src/features/autonomous/stages.ts';
import * as errors from '../src/features/autonomous/errors.ts';
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
    '../../api-client/autonomous': { createAutonomousApi: () => api, ApiError },
    '../../ui/workbench/Workbench': { Workbench: box('Workbench') },
    '../../ui/Card': { Badge: box('Badge'), Card: box('Card'), CardHeader: box('CardHeader') },
    '../../ui/Disclosure': { Disclosure: box('Disclosure') },
    '../../ui/Icon': { Icon: () => null },
    './ApprovalCard': { ApprovalCard: box('ApprovalCard') },
    './CandidateCard': { CandidateCard: box('CandidateCard') },
    './ActivityViews': { ProgressNotes: box('ProgressNotes'), SpecialistCalls: box('SpecialistCalls') },
    './ReportView': { ReportView: box('ReportView') },
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
