import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  PHASES, artifactPath, artifactRound, buildStages, currentProtocolSha, flattenItems, isKeyRun, progressNotes, railItems, specialistCalls, toolLabel, toWbFiles,
} from '../src/features/autonomous/stages.ts';

// The timeline groups the real event journal into phases. It is presentation
// only: Pi's own progress notes and specialist advice are labelled as model
// narrative, and nothing here turns them into a program judgement.

const ev = (sequence, type, payload = {}) => ({ sequence, type, payload, created_at: '2026-11-05T12:00:00Z' });
const tool = (sequence, id, name, args = {}) => ev(sequence, 'tool_started', { id, name, arguments: args });
const done = (sequence, id, name, extra = {}) => ev(sequence, 'tool_completed', { id, name, result: {}, is_error: false, ...extra });
const byId = stages => Object.fromEntries(stages.map(s => [s.id, s]));
const sha = c => c.repeat(64);

test('a 候选与复核 phase sits between evidence and report', () => {
  assert.deepEqual(PHASES.map(p => p.id), ['setup', 'plan', 'execute', 'evidence', 'review', 'report']);
  assert.equal(PHASES.find(p => p.id === 'review').title, '候选与复核');
});

test('register_artifact, submit_candidate and the review events land in their phases', () => {
  const events = [
    ev(1, 'task_created'),
    tool(2, 'a', 'register_artifact', { name: 'train.py', kind: 'code' }), done(3, 'a', 'register_artifact'),
    tool(4, 'b', 'submit_candidate', { summary: 's' }), done(5, 'b', 'submit_candidate'),
    ev(6, 'candidate_frozen', { snapshot_sha256: sha('a'), round: 0 }),
    ev(7, 'review_failed', { snapshot_sha256: sha('a'), attempt: 1, reason: 'timeout' }),
    ev(8, 'review_recorded', { snapshot_sha256: sha('a'), verdict: 'issues', findings: [{ description: 'x', evidence: ['y'] }], attempt: 2 }),
  ];
  const { stages, active } = buildStages({ id: 't', state: 'running' }, events);
  const s = byId(stages);
  assert.equal(active, 'review');
  assert.equal(s.evidence.items[0].label, toolLabel('register_artifact', { name: 'train.py' }));
  const review = s.review.items;
  assert.equal(review[0].label, toolLabel('submit_candidate', {}));
  assert.equal(review[1].label, '候选已冻结');
  assert.equal(review[1].tone, 'ok');
  assert.equal(review[2].tone, 'bad');
  assert.match(review[2].label, /第 1 次复核未完成/);
  assert.equal(review[3].tone, 'warn');
  assert.match(review[3].label, /复核发现 1 个问题/);
  const pass = buildStages({ id: 't', state: 'running' }, [ev(1, 'review_recorded', { verdict: 'pass', findings: [] })]);
  assert.equal(byId(pass.stages).review.items[0].tone, 'ok');
});

test('a rejected protocol change sits with the approval request in the plan phase as a user action', () => {
  const { stages } = buildStages({ id: 't', state: 'queued' }, [
    ev(1, 'protocol_approval_required', { reason: 'r' }), ev(2, 'protocol_rejected', { decision_id: 'd', note: 'keep metric' }),
  ]);
  const item = byId(stages).plan.items[1];
  assert.equal(item.label, '方案变更已拒绝');
  assert.equal(item.tone, 'accent');
  assert.match(item.detail, /keep metric/);
});

// Real bridge shapes (apps/harness/bridge.mjs research_delegate): started, then
// research_delegation_completed only on success, and always a delegated
// message_end in `finally` whose message.stopReason is error/aborted on failure.
const delegationBase = { specialist_id: 'advisor', task_type: 'method_design', model: 'm', reason: 'check design', evidence_status: 'unverified_advisory' };
const dStart = seq => ev(seq, 'sdk_event', { type: 'research_delegation_started', ...delegationBase });
const dTurn = seq => ev(seq, 'sdk_event', { type: 'turn_start', delegated: true, specialist_id: 'advisor', model: 'm' });
const dDone = seq => ev(seq, 'sdk_event', { type: 'research_delegation_completed', ...delegationBase, text: 'use k-fold' });
const dEnd = (seq, stopReason) => ev(seq, 'sdk_event', { type: 'message_end', delegated: true, specialist_id: 'advisor', message: { role: 'assistant', model: 'm', content: [{ type: 'text', text: '' }], usage: {}, stopReason } });

test('specialist delegations are model-narrative sub-steps and never green', () => {
  const events = [tool(1, 'a', 'read', { path: 'p.md' }), dStart(2), dTurn(3), dDone(4), dEnd(5, 'stop'), dStart(6), dTurn(7), dEnd(8, 'error'), dStart(9), dEnd(10, 'aborted'), dStart(11)];
  const { stages } = buildStages({ id: 't', state: 'running' }, events);
  const items = byId(stages).plan.items.filter(i => i.label === '专家建议（模型叙述）');
  assert.equal(items.length, 4, 'one sub-step per delegation; completion and message_end update it');
  for (const item of items) assert.notEqual(item.tone, 'ok');
  assert.match(items[0].detail, /advisor · 方法设计，已返回/);
  assert.equal(items[0].tone, 'idle');
  for (const failed of [items[1], items[2]]) { assert.equal(failed.tone, 'bad'); assert.match(failed.detail, /调用失败/); }
  assert.match(items[3].detail, /进行中/);
  const calls = specialistCalls(events);
  assert.deepEqual(calls.map(c => c.status), ['done', 'failed', 'failed', 'running']);
  assert.equal(calls[0].text, 'use k-fold');
  assert.equal(calls[1].text, null, 'a failed call has no advisory text');
  assert.equal(calls[0].taskTypeLabel, '方法设计');
  assert.equal(calls[0].reason, 'check design');
  assert.equal(calls[0].model, 'm');
});

test('a delegation still open when the task is finished or has an outcome shows 未返回', () => {
  const events = [dStart(1), dTurn(2)];
  for (const task of [{ id: 't', state: 'stopped' }, { id: 't', state: 'paused', outcome: 'completed_with_issues' }]) {
    assert.deepEqual(specialistCalls(events, task).map(c => c.status), ['missing'], JSON.stringify(task));
    const item = buildStages(task, events).stages.flatMap(s => s.items).find(i => i.label === '专家建议（模型叙述）');
    assert.match(item.detail, /未返回/);
    assert.equal(item.tone, 'idle');
  }
  assert.deepEqual(specialistCalls(events, { id: 't', state: 'running' }).map(c => c.status), ['running']);
  assert.deepEqual(specialistCalls(events).map(c => c.status), ['running'], 'without a task the call is still running');
});

test('the activity view has no dead is_error branch and labels every call status', async () => {
  const source = await readFile(new URL('../src/features/autonomous/stages.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /is_error \|\| !!p\.error|delegationFailed/);
  const views = await readFile(new URL('../src/features/autonomous/ActivityViews.tsx', import.meta.url), 'utf8');
  assert.match(views, /missing: \{ text: '未返回', tone: 'idle' \}/);
  assert.match(views, /failed: \{ text: '调用失败', tone: 'bad' \}/);
});

// The review phase is a program result, so its mark follows the outcome and the
// latest recorded verdict, never just "a later phase started".
const reviewMark = (task, events) => byId(buildStages({ id: 't', ...task }, events).stages).review.mark;
const submitted = [tool(1, 'b', 'submit_candidate'), done(2, 'b', 'submit_candidate'), ev(3, 'candidate_frozen', { snapshot_sha256: sha('a'), round: 0 })];
const verdict = (seq, v) => ev(seq, 'review_recorded', { snapshot_sha256: sha('a'), verdict: v, findings: [], attempt: 1 });
const finishedWith = (seq, outcome) => [ev(seq, 'report_created', { outcome }), ev(seq + 1, 'task_finished', { state: 'completed', outcome })];

test('review phase is done only for verified or a passing latest verdict', () => {
  assert.equal(reviewMark({ state: 'completed', outcome: 'verified' }, [...submitted, verdict(4, 'pass'), ...finishedWith(5, 'verified')]), 'done');
  assert.equal(reviewMark({ state: 'stopped', outcome: null }, [...submitted, verdict(4, 'issues'), verdict(5, 'pass'), ev(6, 'task_finished', { state: 'stopped' })]), 'done');
});

test('review phase needs attention for issues, exhausted rounds and unavailable review', () => {
  assert.equal(reviewMark({ state: 'completed', outcome: 'completed_with_issues' }, [...submitted, verdict(4, 'issues'), ...finishedWith(5, 'completed_with_issues')]), 'attention');
  assert.equal(reviewMark({ state: 'stopped', outcome: null }, [...submitted, verdict(4, 'pass'), verdict(5, 'issues'), ev(6, 'task_finished', { state: 'stopped' })]), 'attention', 'the latest verdict decides');
  assert.equal(reviewMark({ state: 'completed', outcome: 'review_unavailable' }, [...submitted, ev(4, 'review_failed', { attempt: 2, reason: 'x' }), ...finishedWith(5, 'review_unavailable')]), 'attention');
  assert.equal(reviewMark({ state: 'stopped', outcome: null }, [...submitted, ev(4, 'review_failed', { attempt: 1, reason: 'x' }), ev(5, 'task_finished', { state: 'stopped' })]), 'attention', 'failed attempts and no recorded review');
  // A repair round: execution is active again and the last review found issues.
  assert.equal(reviewMark({ state: 'running', repair_round: 1 }, [...submitted, verdict(4, 'issues'), tool(5, 'c', 'write', { path: 'train.py' })]), 'attention');
});

test('review phase fails on a safety block; it keeps running while a review is live', () => {
  assert.equal(reviewMark({ state: 'stopped', outcome: 'blocked_by_safety' }, [...submitted, ev(4, 'safety_blocked', { code: 'X' }), ev(5, 'task_finished', { state: 'stopped', outcome: 'blocked_by_safety' })]), 'failed');
  assert.equal(reviewMark({ state: 'running' }, submitted), 'running');
});

test('a phase after the active one that was started in an earlier round shows open, not done', () => {
  const events = [tool(1, 'a', 'register_artifact', { name: 'x' }), done(2, 'a', 'register_artifact'), ev(3, 'continuation_sent', { reason: 'review_issues', repair_round: 1 }), tool(4, 'c', 'write', { path: 'x.py' })];
  const s = byId(buildStages({ id: 't', state: 'running', repair_round: 1 }, events).stages);
  assert.equal(s.execute.mark, 'running');
  assert.equal(s.evidence.mark, 'open');
  assert.equal(s.report.mark, 'locked');
});

test('backend strings never hit prototype keys in the lookup tables', () => {
  const events = [tool(1, 'a', 'constructor'), done(2, 'a', 'constructor'), ev(3, 'toString'), ev(4, 'artifact_frozen', { name: 'm', kind: '__proto__', protocol_sha256: null })];
  const { stages, active } = buildStages({ id: 't', state: 'running' }, events);
  assert.equal(typeof active, 'string');
  assert.ok(stages.every(s => typeof s.id === 'string'));
  const files = toWbFiles([{ path: 'artifacts/unversioned/m', source: 'artifact', bytes: 1, media_type: 'text/plain', sha256: 'h', kind: 'constructor', protocol_sha256: null, modified_at: null }]);
  assert.equal(files[0].tags[0].text, 'constructor');
});

test('safety_blocked is a bad sub-step that ends in the report phase', () => {
  const { stages, active } = buildStages({ id: 't', state: 'stopped', outcome: 'blocked_by_safety' }, [
    tool(1, 'a', 'execute', { command: 'python x.py' }), ev(2, 'safety_blocked', { code: 'ISOLATION_PROBE_FAILED', message: 'm' }),
  ]);
  const s = byId(stages);
  assert.equal(active, 'report');
  assert.equal(s.report.items[0].label, '安全终止');
  assert.equal(s.report.items[0].tone, 'bad');
  assert.equal(s.report.items[0].detail, 'ISOLATION_PROBE_FAILED');
  assert.equal(s.report.mark, 'failed');
  assert.equal(s.review.mark, 'open', 'the review never started, so it is neither green nor failed');
});

test('agent progress notes become the active stage detail without duplicating the tool step', () => {
  const events = [
    tool(1, 'a', 'execute', { command: 'python train.py' }), done(2, 'a', 'execute'),
    tool(3, 'n', 'note_progress', { phase: '训练基线', summary: '第一轮训练完成，loss 下降' }),
    ev(4, 'progress_noted', { phase: '训练基线', summary: '第一轮训练完成，loss 下降' }), done(5, 'n', 'note_progress'),
  ];
  const { stages, active } = buildStages({ id: 't', state: 'running' }, events);
  const execute = byId(stages).execute;
  assert.equal(active, 'execute', 'note_progress does not move the phase');
  assert.equal(execute.items.length, 2);
  assert.equal(execute.items[1].label, '第一轮训练完成，loss 下降', 'the rail draws the note as its own words');
  assert.equal(execute.items[1].variant, 'note');
  assert.doesNotMatch(JSON.stringify(stages), /\bPi\b/, 'the agent is never named');
  assert.notEqual(execute.items[1].tone, 'ok', 'a progress note is narrative, not a passed check');
  assert.match(execute.summary, /智能体 · 训练基线：第一轮训练完成/);
  const notes = progressNotes([...events, ev(9, 'progress_noted', { phase: '评估', summary: '准备提交' })]);
  assert.deepEqual(notes.map(n => n.phase), ['评估', '训练基线'], 'newest first');
});

test('a repair round moves the active phase back to execution; a worker exit does not jump to report', () => {
  const events = [
    tool(1, 'b', 'submit_candidate'), done(2, 'b', 'submit_candidate'),
    ev(3, 'review_recorded', { verdict: 'issues', findings: [] }), ev(4, 'continuation_sent', { reason: 'review_issues', repair_round: 1 }),
    tool(5, 'c', 'write', { path: 'train.py' }),
  ];
  const { stages, active } = buildStages({ id: 't', state: 'running', repair_round: 1 }, events);
  assert.equal(active, 'execute');
  assert.equal(byId(stages).execute.mark, 'running');
  assert.equal(byId(stages).review.mark, 'attention', 'the last review found issues');
  assert.match(byId(stages).review.items.at(-1).label, /进入第 1 轮修复/);
  const paused = buildStages({ id: 't', state: 'paused' }, [tool(1, 'c', 'execute', { command: 'x' }), ev(2, 'task_paused'), ev(3, 'worker_exited', { state: 'paused' })]);
  assert.equal(paused.active, 'execute');
  assert.equal(byId(paused.stages).execute.mark, 'attention');
  assert.equal(byId(paused.stages).report.mark, 'locked');
});

test('a report with not_comparable never shows a green sub-step', () => {
  const { stages } = buildStages({ id: 't', state: 'completed', outcome: 'verified' }, [
    ev(1, 'report_created', { verdict: 'not_comparable', status: 'not_comparable', outcome: 'verified' }),
    ev(2, 'task_finished', { state: 'completed', outcome: 'verified', verdict: 'not_comparable' }),
  ]);
  const [report, finished] = byId(stages).report.items;
  assert.notEqual(report.tone, 'ok');
  assert.doesNotMatch(`${report.detail}`, /not_comparable/);
  assert.equal(finished.detail, '已复核');
});

test('artifact paths carry the repair round; legacy paths have none', () => {
  assert.equal(artifactPath('out/m.json', 'abcdef0123456789', 2), 'artifacts/abcdef012345/r2/out/m.json');
  assert.equal(artifactPath('x', null), 'artifacts/unversioned/x');
  assert.equal(artifactRound('artifacts/abcdef012345/r2/out/m.json'), 2);
  assert.equal(artifactRound('artifacts/abcdef012345/m.json'), null);
  assert.equal(artifactRound('workspace/r2/m.json'), null);
  const { stages } = buildStages({ id: 't', state: 'running' }, [ev(1, 'artifact_frozen', { name: 'm.json', kind: 'result', protocol_sha256: 'abcdef0123456789', round: 1 })]);
  assert.equal(byId(stages).evidence.items[0].openPath, 'artifacts/abcdef012345/r1/m.json');
});

test('evidence files show kind and round, and flag old protocol versions and earlier rounds', () => {
  const current = sha('c');
  const file = (path, protocol_sha256, kind = 'result') => ({ path, source: 'artifact', bytes: 1, media_type: 'application/json', sha256: sha('f'), kind, protocol_sha256, modified_at: null });
  const files = toWbFiles([
    file(`artifacts/${current.slice(0, 12)}/r2/m.json`, current),
    file(`artifacts/${current.slice(0, 12)}/r1/m.json`, current, 'code'),
    file(`artifacts/${sha('o').slice(0, 12)}/r2/m.json`, sha('o')),
    file(`artifacts/${sha('o').slice(0, 12)}/legacy.json`, sha('o')),
    { path: 'workspace/train.py', source: 'workspace', bytes: 1, media_type: 'text/plain', sha256: null, kind: null, protocol_sha256: null, modified_at: null },
  ], { round: 2, protocolSha256: current });
  const tags = f => (f.tags ?? []).map(t => t.text);
  assert.deepEqual(tags(files[0]), ['结果', '第 2 轮']);
  assert.deepEqual(tags(files[1]), ['代码', '第 1 轮', '早期轮次']);
  assert.deepEqual(tags(files[2]), ['结果', '第 2 轮', '旧方案版本']);
  assert.deepEqual(tags(files[3]), ['结果', '旧方案版本'], 'a legacy path has no round');
  assert.deepEqual(tags(files[4]), []);
  assert.equal(files[0].badge, '证据');
  assert.equal(files[0].sha256, sha('f'), 'the full hash is kept for copying');
  assert.equal(files[2].tags.find(t => t.text === '旧方案版本').tone, 'warn');
  assert.deepEqual(tags(toWbFiles([file(`artifacts/${current.slice(0, 12)}/r1/m.json`, sha('o'))])[0]), ['结果', '第 1 轮'], 'without context nothing is flagged');
});

test('the current protocol comes from the latest protocol event, then the candidate', () => {
  const events = [
    ev(1, 'task_created', { protocol_sha256: sha('1') }),
    ev(2, 'protocol_approved', { decision_id: 'd', protocol: { protocol: {}, sha256: sha('2') } }),
    ev(3, 'protocol_version', { protocol: { protocol: {}, sha256: sha('3') }, reason: 'r', changed: [] }),
  ];
  assert.equal(currentProtocolSha({ candidate: { protocol_sha256: sha('9') } }, events), sha('3'));
  assert.equal(currentProtocolSha({ candidate: { protocol_sha256: sha('9') } }, []), sha('9'));
  assert.equal(currentProtocolSha(null, []), null);
});

test('the timeline badge explains it is derived, not a program judgement', async () => {
  const page = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /阶段由智能体自报进度与事件归纳得出，不是程序判定/);
  assert.doesNotMatch(page, /契约 C1/);
  assert.match(page, /<ProgressNotes\b/);
  assert.match(page, /<SpecialistCalls\b/);
  const views = await readFile(new URL('../src/features/autonomous/ActivityViews.tsx', import.meta.url), 'utf8');
  assert.match(views, /智能体自报进度（模型叙述，不是程序判定）/);
  assert.match(views, /科研专家调用/);
  assert.match(views, /建议，不是执行证据/);
});

// Re-review probe cases: a phase that never started is never green.
test('probe: a safety block during execute leaves the unstarted review and evidence phases open', () => {
  const s = byId(buildStages({ id: 't', state: 'stopped', outcome: 'blocked_by_safety' }, [
    ev(1, 'task_created'), tool(2, 'a', 'execute', { command: 'python x.py' }), ev(3, 'safety_blocked', { code: 'X' }),
    ev(4, 'task_finished', { state: 'stopped', outcome: 'blocked_by_safety' }),
  ]).stages);
  assert.equal(s.review.mark, 'open');
  assert.equal(s.evidence.mark, 'open');
  assert.equal(s.execute.mark, 'done', 'a phase that really ran before the active one stays done');
  assert.equal(s.report.mark, 'failed');
});

test('probe: a failed task with an error in execute never greens the later phases', () => {
  const s = byId(buildStages({ id: 't', state: 'failed' }, [
    ev(1, 'task_created'), tool(2, 'a', 'execute', { command: 'python x.py' }), ev(3, 'task_error', { message: 'boom' }),
    ev(4, 'task_finished', { state: 'failed' }),
  ]).stages);
  assert.equal(s.review.mark, 'open');
  assert.equal(s.evidence.mark, 'open');
  assert.equal(s.plan.mark, 'open', 'the task never read or planned');
  assert.equal(s.execute.mark, 'failed');
});

test('probe: a task stopped during execute without an outcome never greens the later phases', () => {
  const s = byId(buildStages({ id: 't', state: 'stopped', outcome: null }, [
    ev(1, 'task_created'), tool(2, 'a', 'read', { path: 'p.md' }), done(3, 'a', 'read'), tool(4, 'b', 'execute', { command: 'x' }),
    ev(5, 'stop_requested'), ev(6, 'task_finished', { state: 'stopped' }),
  ]).stages);
  assert.equal(s.review.mark, 'open');
  assert.equal(s.evidence.mark, 'open');
  assert.equal(s.plan.mark, 'done');
  assert.equal(s.execute.mark, 'done');
});

test('a stopped or cancelled task_finished item is idle, not green; a withdrawn decision stays in 阅读与方案', () => {
  for (const state of ['stopped', 'cancelled']) {
    const s = byId(buildStages({ id: 't', state, outcome: null }, [
      ev(1, 'task_created'), ev(2, 'protocol_approval_required', { reason: 'r' }), ev(3, 'stop_requested'),
      ev(4, 'decision_withdrawn', { decision_id: 'd', reason: 'task_stopped' }), ev(5, 'task_finished', { state }),
    ]).stages);
    const finished = s.report.items.find(item => item.label === '任务结束');
    assert.equal(finished.tone, 'idle', state);
    const withdrawn = s.plan.items.find(item => item.label === '待决定已撤回');
    assert.deepEqual([withdrawn.detail, withdrawn.tone], ['任务已停止', 'idle']);
  }
});

test('冻结证据 is not done when it has no records', () => {
  const s = byId(buildStages({ id: 't', state: 'completed', outcome: null }, [
    ev(1, 'task_created'), tool(2, 'b', 'execute', { command: 'x' }), done(3, 'b', 'execute'), ev(4, 'task_finished', { state: 'completed' }),
  ]).stages);
  assert.equal(s.evidence.items.length, 0);
  assert.notEqual(s.evidence.mark, 'done');
  assert.equal(s.evidence.mark, 'open');
});

test('an unknown outcome needs attention on the review phase even after a passing verdict', () => {
  const events = [...submitted, verdict(4, 'pass'), ev(5, 'task_finished', { state: 'completed', outcome: 'something_new' })];
  assert.equal(reviewMark({ state: 'completed', outcome: 'something_new' }, events), 'attention');
  // verified and a passing verdict keep their green, as the re-review confirmed.
  assert.equal(reviewMark({ state: 'completed', outcome: 'verified' }, [...submitted, verdict(4, 'pass'), ...finishedWith(5, 'verified')]), 'done');
});

test('a safety block before the review phase started does not mark it failed', () => {
  assert.equal(reviewMark({ state: 'stopped', outcome: 'blocked_by_safety' }, [...submitted, ev(4, 'safety_blocked', { code: 'X' })]), 'failed', 'started: failed');
  const s = byId(buildStages({ id: 't', state: 'stopped', outcome: 'blocked_by_safety' }, [tool(1, 'a', 'write', { path: 'x' }), ev(2, 'safety_blocked', { code: 'X' })]).stages);
  assert.equal(s.review.mark, 'open', 'never started: open');
});

// Re-review probe: delegations are serialised, so an ending belongs to the most recent open call.
test('probe: an ending and a completion pair with the most recent open call, not the oldest', () => {
  const events = [dStart(1), dStart(5), dEnd(6, 'error')];
  assert.deepEqual(specialistCalls(events, { state: 'running' }).map(c => [c.sequence, c.status]), [[1, 'running'], [5, 'failed']]);
  assert.deepEqual(specialistCalls(events, { state: 'stopped' }).map(c => [c.sequence, c.status]), [[1, 'missing'], [5, 'failed']]);
  const items = buildStages({ id: 't', state: 'stopped' }, events).stages.flatMap(st => st.items).filter(i => i.label === '专家建议（模型叙述）');
  assert.deepEqual(items.map(i => /未返回/.test(i.detail)), [true, false], '未返回 shows on call 1 once the task is finished');
  const replied = specialistCalls([dStart(1), dStart(5), dDone(6), dEnd(7, 'stop')], { state: 'running' });
  assert.deepEqual(replied.map(c => [c.sequence, c.status, c.text]), [[1, 'running', null], [5, 'done', 'use k-fold']], 'an orphaned earlier call never takes the later reply');
});

test('agent progress notes nest the following tool calls; earlier calls stay top-level', () => {
  const events = [
    tool(1, 'x', 'read', { path: 'before.md' }), done(2, 'x', 'read'),
    ev(3, 'progress_noted', { phase: '读代码', summary: '先核对模型配置' }),
    tool(4, 'a', 'read', { path: 'paper.md' }), done(5, 'a', 'read'),
    tool(6, 'b', 'execute', { command: 'ls' }),
  ];
  const { stages } = buildStages({ id: 't', state: 'running' }, events);
  const plan = byId(stages).plan;
  assert.equal(plan.items[0].label, toolLabel('read', { path: 'before.md' }), 'calls before any note stay top-level');
  assert.equal(plan.items[1].label, '先核对模型配置');
  assert.equal(plan.items[1].tone, 'idle');
  assert.match(plan.items[1].detail, /模型叙述/);
  assert.match(plan.items[1].children[0].label, /^读取/);
  assert.equal(plan.items[1].children[0].tone, 'ok', 'completion still colours the nested item');
  assert.equal(plan.items.length, 2);
  const execute = byId(stages).execute;
  assert.equal(execute.items.length, 1, 'the next phase starts a fresh top-level list');
  assert.equal(execute.items[0].tone, 'run');
});

test('a second note ends the previous group and a phase change ends the group', () => {
  const events = [
    ev(0, 'protocol_approved', { protocol_version: 'v1' }),
    ev(1, 'progress_noted', { phase: 'a', summary: 'one' }), tool(2, 'a', 'read', { path: 'p.md' }),
    ev(3, 'progress_noted', { phase: 'b', summary: 'two' }), tool(4, 'b', 'read', { path: 'q.md' }),
    tool(5, 'c', 'write', { path: 'x.py' }),
  ];
  const { stages } = buildStages({ id: 't', state: 'running' }, events);
  const plan = byId(stages).plan.items;
  assert.deepEqual(plan.map(i => i.label), ['方案已批准', 'one', 'two']);
  assert.equal(plan[1].children.length, 1);
  assert.equal(plan[2].children.length, 1);
  assert.equal(byId(stages).execute.items[0].children, undefined, 'execute is a new phase, not nested under the plan note');
});

test('the truncation note counts only capped top-level items, not nested children', () => {
  const reads = (from, n) => Array.from({ length: n }, (_, i) => tool(from + i, `r${from + i}`, 'read', { path: `f${i}.md` }));
  const leave = seq => tool(seq, 'w', 'write', { path: 'x.py' }); // move on so plan is no longer the active phase
  const nested = [ev(0, 'protocol_approved', { protocol_version: 'v1' }), ev(0, 'progress_noted', { phase: 'a', summary: 'one' }), ...reads(1, 20), leave(30)];
  const nestedPlan = byId(buildStages({ id: 't', state: 'running' }, nested).stages).plan;
  assert.equal(nestedPlan.items.length, 2);
  assert.equal(nestedPlan.items[1].children.length, 20);
  assert.match(nestedPlan.summary, /22 条记录/);
  assert.doesNotMatch(nestedPlan.summary, /显示最近 14 条/, 'nothing was cut, so no truncation claim');
  const flat = [...reads(1, 15), leave(30)];
  const flatPlan = byId(buildStages({ id: 't', state: 'running' }, flat).stages).plan;
  assert.equal(flatPlan.items.length, 14);
  assert.match(flatPlan.summary, /15 条记录/);
  assert.match(flatPlan.summary, /显示最近 14 条/);
});

test('timeline sub-steps open real artifacts only, never the whole raw event log', () => {
  const { stages } = buildStages({ id: 't', state: 'running' }, [
    tool(1, 'c1', 'bash', { command: 'python train.py' }), done(2, 'c1', 'bash'),
    ev(3, 'artifact_frozen', { name: 'm.json', kind: 'result', protocol_sha256: 'abcdef0123456789', round: 1 }),
  ]);
  const items = stages.flatMap(s => s.items ?? []);
  assert.ok(items.length >= 2);
  assert.ok(items.every(item => item.openPath !== 'logs/events.log'));
  assert.ok(items.some(item => item.openPath === 'artifacts/abcdef012345/r1/m.json'));
});

test('the rail keeps nodes only: notes, milestones and script runs; other tool calls stay for the drawer', () => {
  const events = [
    tool(1, 'a', 'execute', { command: 'pip install numpy' }), done(2, 'a', 'execute'),
    tool(3, 'n', 'note_progress', { phase: '训练', summary: '开始正式训练' }),
    ev(4, 'progress_noted', { phase: '训练', summary: '开始正式训练' }), done(5, 'n', 'note_progress'),
    tool(6, 'w', 'write', { path: 'train.py' }), done(7, 'w', 'write'),
    tool(8, 'r', 'execute', { command: '/work/.venv/bin/python run_experiment.py --epochs 60' }), done(9, 'r', 'execute'),
    tool(10, 'b', 'background', { command: 'bash run_all.sh' }), done(11, 'b', 'background'),
    tool(12, 'c', 'execute', { command: 'python3 -c "import numpy"' }), done(13, 'c', 'execute'),
    ev(14, 'artifact_frozen', { name: 'm.json', kind: 'result', protocol_sha256: 'abcdef0123456789', round: 0 }),
  ];
  const { stages, operations } = buildStages({ id: 't', state: 'running' }, events);
  const all = [...operations.values()].flat();
  const rail = railItems(all).map(item => item.id);
  assert.deepEqual(rail, ['t3', 't8', 't10', 'e14'], 'note, two script runs and the frozen artifact');
  assert.equal(flattenItems(all).length, 7, 'the drawer still lists every operation');
  assert.ok(isKeyRun('execute', { command: 'cd /work && python train.py' }));
  assert.ok(!isKeyRun('execute', { command: '/work/.venv/bin/python -m py_compile /work/run.py' }));
  assert.ok(!isKeyRun('execute', { command: 'ls -la' }));
  assert.ok(stages.length > 0);
});
