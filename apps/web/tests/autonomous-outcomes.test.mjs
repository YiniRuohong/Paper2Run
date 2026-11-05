import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import {
  defaultConfig, validateConfig, taskDisplay, taskBucket, outcomeLabel, outcomeTone, errorExplanation, maxRepairRounds, stateLabel, terminalState,
} from '../src/features/autonomous/config.ts';
import { parseReport, parseCandidate, parseReviews } from '../src/features/autonomous/report.ts';

// Outcomes are program-decided (driver.finish). The UI shows them as the primary
// status word; the lifecycle state stays visible as secondary text.

test('each outcome maps to its own word and tone; verified carries the reproduction caveat', () => {
  assert.deepEqual([outcomeLabel('verified'), outcomeTone('verified')], ['已复核', 'ok']);
  assert.deepEqual([outcomeLabel('completed_with_issues'), outcomeTone('completed_with_issues')], ['完成·有未解决问题', 'warn']);
  assert.deepEqual([outcomeLabel('review_unavailable'), outcomeTone('review_unavailable')], ['复核不可用', 'warn']);
  assert.deepEqual([outcomeLabel('blocked_by_safety'), outcomeTone('blocked_by_safety')], ['安全终止', 'bad']);
  assert.equal(outcomeLabel(null), null);
  assert.equal(taskDisplay({ state: 'completed', outcome: 'verified' }).caption, '复核通过不等于复现成立；可比性见报告');
  assert.equal(taskDisplay({ state: 'completed', outcome: 'completed_with_issues' }).caption, null);
});

test('a task with an outcome is finished even when paused or stopped, with no paused notice', () => {
  for (const state of ['paused', 'stopped', 'stopping', 'completed']) {
    const shown = taskDisplay({ state, outcome: 'completed_with_issues', pending_decision: null });
    assert.equal(shown.text, '完成·有未解决问题', state);
    assert.equal(shown.tone, 'warn', state);
    assert.equal(shown.finished, true, state);
    assert.equal(shown.pausedNotice, false, state);
    assert.equal(shown.lifecycle, stateLabel(state), 'lifecycle state stays as secondary text');
  }
  const safety = taskDisplay({ state: 'stopped', outcome: 'blocked_by_safety' });
  assert.deepEqual([safety.text, safety.tone, safety.lifecycle], ['安全终止', 'bad', '已停止']);
  assert.equal(taskBucket({ state: 'paused', outcome: 'verified' }), 'done');
  assert.equal(taskBucket({ state: 'stopped', outcome: 'blocked_by_safety' }), 'failed');
});

test('without an outcome the lifecycle state is the status; paused keeps its notice', () => {
  const paused = taskDisplay({ state: 'paused', outcome: null });
  assert.deepEqual([paused.text, paused.tone, paused.finished, paused.pausedNotice, paused.lifecycle], ['已暂停', 'warn', false, true, null]);
  const running = taskDisplay({ state: 'running' });
  assert.deepEqual([running.text, running.tone, running.finished], ['运行中', 'run', false]);
  assert.equal(taskDisplay({ state: 'failed', outcome: null }).tone, 'bad');
  assert.equal(taskDisplay({ state: 'running', pending_decision: { id: 'd' } }).tone, 'warn', 'a pending decision needs a person');
  assert.equal(taskBucket({ state: 'paused' }), 'active');
  // The backend ends a no-progress task as `incomplete` (driver.after_turn).
  assert.equal(stateLabel('incomplete'), '未完成');
  assert.equal(terminalState('incomplete'), true);
  assert.equal(taskDisplay({ state: 'incomplete' }).finished, true);
});

test('an unknown outcome is shown as unknown, never guessed into a known one', () => {
  const shown = taskDisplay({ state: 'completed', outcome: 'something_new' });
  assert.equal(shown.tone, 'idle');
  assert.match(shown.text, /未知结局/);
  assert.match(shown.text, /something_new/);
  assert.equal(shown.finished, true);
});

test('repair round budget defaults to 3, allows 0..10 and keeps the other limits positive', () => {
  const config = defaultConfig('p'); config.model = { provider_profile: 'm', credential_ref: 'profile:m' }; config.experiment.objective = 'test';
  assert.equal(config.limits.max_repair_rounds, 3);
  for (const value of [0, 10]) assert.doesNotThrow(() => validateConfig({ ...config, limits: { ...config.limits, max_repair_rounds: value } }), String(value));
  for (const value of [11, -1, 1.5, NaN, Infinity]) assert.throws(() => validateConfig({ ...config, limits: { ...config.limits, max_repair_rounds: value } }), /修复轮次/, String(value));
  assert.throws(() => validateConfig({ ...config, limits: { ...config.limits, max_model_calls: 0 } }), /正整数/, 'zero is allowed for repair rounds only');
  const { max_repair_rounds: _omit, ...legacyLimits } = config.limits;
  assert.doesNotThrow(() => validateConfig({ ...config, limits: legacyLimits }), 'configs saved before the key existed stay valid');
  assert.equal(maxRepairRounds({ limits: legacyLimits }), 3, 'an absent key means the backend default of 3');
  assert.equal(maxRepairRounds({ limits: { ...legacyLimits, max_repair_rounds: 0 } }), 0);
  assert.equal(maxRepairRounds(undefined), 3);
});

test('backend error codes get a specific Chinese explanation', () => {
  assert.equal(errorExplanation('LEGACY_TASK'), '该任务创建于新版 harness 之前，只能导出');
  assert.match(errorExplanation('TASK_FINISHED'), /已有结局/);
  assert.match(errorExplanation('DECISION_PENDING'), /批准或拒绝/);
  assert.match(errorExplanation('DECISION_MISMATCH'), /已被处理或已过期/);
  assert.match(errorExplanation('CANDIDATE_UNDER_REVIEW'), /复核/);
  assert.match(errorExplanation('CANDIDATE_INCOMPLETE'), /候选/);
  assert.equal(errorExplanation('SOMETHING_ELSE'), null);
});

const base = () => ({
  status: 'not_comparable', verdict: 'not_comparable',
  completion_gate: { entrypoint: 'train.py', frozen_code_sha256: 'a'.repeat(64), rule: 'rule' },
  successful_execution_observed: true, execution_observations: [],
  evaluation: { verdict: 'not_comparable', comparison_eligible: false, reasons: [], metrics: {} },
  verification: null, artifacts: [], missing_artifacts: [], missing_evidence_kinds: [],
  model_narrative: '', limitations: [], sha256: 'd'.repeat(64),
});
const candidate = () => ({
  snapshot_sha256: 's'.repeat(64), round: 1, protocol_sha256: 'p'.repeat(64), protocol: { protocol: {}, sha256: 'p'.repeat(64) },
  artifacts: [{ name: 'train.py', kind: 'code', sha256: 'c'.repeat(64), bytes: 10, source_path: '/work/train.py', round: 1, sequence: 4, protocol_sha256: 'p'.repeat(64) }],
  run: { job_id: 'abc123', exit_code: 0, sequence: 6 }, summary: 'done', review_status: 'issues', attempts: 1,
});
const review = (over = {}) => ({ snapshot_sha256: 's'.repeat(64), round: 1, attempt: 1, verdict: 'issues', findings: [{ description: 'leak', evidence: ['metrics.json:3'] }], ...over });

test('parseReport reads outcome, reason, deciding snapshot, candidate and reviews', () => {
  const report = parseReport({ ...base(), outcome: 'completed_with_issues', outcome_reason: 'budget_exhausted', outcome_snapshot_sha256: 's'.repeat(64), candidate: candidate(), reviews: [review()] });
  assert.ok(report);
  assert.equal(report.outcome, 'completed_with_issues');
  assert.equal(report.outcomeReason, 'budget_exhausted');
  assert.equal(report.outcomeSnapshotSha256, 's'.repeat(64));
  assert.equal(report.candidate.snapshotSha256, 's'.repeat(64));
  assert.equal(report.candidate.run.jobId, 'abc123');
  assert.equal(report.candidate.artifacts[0].round, 1);
  assert.equal(report.reviews[0].findings[0].evidence[0], 'metrics.json:3');
  assert.equal(report.verdict, 'not_comparable', 'the verdict stays a separate program field');
});

test('old reports without the new fields still parse; malformed new fields do not', () => {
  const old = parseReport(base());
  assert.ok(old, 'a pre-candidate report is accepted');
  assert.deepEqual([old.outcome, old.outcomeReason, old.outcomeSnapshotSha256, old.candidate, old.reviews], [null, null, null, null, []]);
  const nulls = parseReport({ ...base(), outcome: null, outcome_reason: null, outcome_snapshot_sha256: null, candidate: null, reviews: [] });
  assert.ok(nulls);
  for (const bad of [{ outcome: 7 }, { outcome_reason: [] }, { candidate: { round: 1 } }, { reviews: 'none' }, { reviews: [{ verdict: 'pass' }] }]) {
    assert.equal(parseReport({ ...base(), ...bad }), null, JSON.stringify(bad));
  }
});

test('candidate and review guards accept the task DTO shape and tolerate optional parts', () => {
  const parsed = parseCandidate(candidate());
  assert.equal(parsed.protocolSha256, 'p'.repeat(64));
  assert.equal(parsed.reviewStatus, 'issues');
  assert.equal(parsed.summary, 'done');
  const { run: _run, summary: _summary, ...partial } = candidate();
  const lean = parseCandidate(partial);
  assert.ok(lean, 'run and summary are optional');
  assert.equal(lean.run, null);
  assert.equal(parseCandidate(null), null);
  assert.equal(parseCandidate({ ...candidate(), artifacts: [{ name: 'x' }] }), null);
  assert.deepEqual(parseReviews([review(), review({ round: 2, attempt: 1, verdict: 'pass', findings: [] })]).map(r => r.verdict), ['issues', 'pass']);
  assert.equal(parseReviews([{ ...review(), findings: [{ description: 'x', evidence: [1] }] }]), null);
  assert.deepEqual(parseReviews(undefined), []);
});

test('the report view shows the outcome block before the verdict block and never greens not_comparable', async () => {
  const view = await readFile(new URL('../src/features/autonomous/ReportView.tsx', import.meta.url), 'utf8');
  const outcomeAt = view.indexOf('<OutcomeCard');
  const conclusionAt = view.indexOf('<ConclusionCard');
  assert.ok(outcomeAt > 0 && outcomeAt < conclusionAt, 'outcome block comes first');
  assert.match(view, /not_comparable' \? 'warn'/);
});
