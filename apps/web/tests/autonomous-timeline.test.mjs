import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  PHASES, artifactPath, artifactRound, buildStages, currentProtocolSha, progressNotes, specialistCalls, toolLabel, toWbFiles,
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

test('specialist delegations are model-narrative sub-steps and never green', () => {
  const start = (seq, extra = {}) => ev(seq, 'sdk_event', { type: 'research_delegation_started', specialist_id: 'advisor', task_type: 'method_design', model: 'm', reason: 'check design', evidence_status: 'unverified_advisory', ...extra });
  const end = (seq, extra = {}) => ev(seq, 'sdk_event', { type: 'research_delegation_completed', specialist_id: 'advisor', task_type: 'method_design', model: 'm', reason: 'check design', evidence_status: 'unverified_advisory', text: 'use k-fold', ...extra });
  const { stages } = buildStages({ id: 't', state: 'running' }, [tool(1, 'a', 'read', { path: 'p.md' }), start(2), end(3), start(4), end(5, { is_error: true, text: '' })]);
  const items = byId(stages).plan.items.filter(i => i.label === '专家建议（模型叙述）');
  assert.equal(items.length, 2, 'one sub-step per delegation, the completion updates it');
  for (const item of items) assert.notEqual(item.tone, 'ok');
  assert.match(items[0].detail, /advisor · 方法设计/);
  assert.match(items[0].detail, /已返回/);
  assert.equal(items[1].tone, 'bad');
  const calls = specialistCalls([start(2), end(3), start(4)]);
  assert.deepEqual(calls.map(c => c.status), ['done', 'running']);
  assert.equal(calls[0].text, 'use k-fold');
  assert.equal(calls[0].taskTypeLabel, '方法设计');
  assert.equal(calls[0].reason, 'check design');
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
});

test('Pi progress notes become the active stage detail without duplicating the tool step', () => {
  const events = [
    tool(1, 'a', 'execute', { command: 'python train.py' }), done(2, 'a', 'execute'),
    tool(3, 'n', 'note_progress', { phase: '训练基线', summary: '第一轮训练完成，loss 下降' }),
    ev(4, 'progress_noted', { phase: '训练基线', summary: '第一轮训练完成，loss 下降' }), done(5, 'n', 'note_progress'),
  ];
  const { stages, active } = buildStages({ id: 't', state: 'running' }, events);
  const execute = byId(stages).execute;
  assert.equal(active, 'execute', 'note_progress does not move the phase');
  assert.equal(execute.items.length, 2);
  assert.match(execute.items[1].label, /Pi 自报进度/);
  assert.notEqual(execute.items[1].tone, 'ok', 'a progress note is narrative, not a passed check');
  assert.match(execute.summary, /Pi 自报 · 训练基线：第一轮训练完成/);
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
  assert.match(page, /阶段由 Pi 自报进度（note_progress）与事件归纳得出，不是程序判定/);
  assert.doesNotMatch(page, /契约 C1/);
  assert.match(page, /<ProgressNotes\b/);
  assert.match(page, /<SpecialistCalls\b/);
  const views = await readFile(new URL('../src/features/autonomous/ActivityViews.tsx', import.meta.url), 'utf8');
  assert.match(views, /Pi 自报进度（模型叙述，不是程序判定）/);
  assert.match(views, /科研专家调用/);
  assert.match(views, /建议，不是执行证据/);
});
