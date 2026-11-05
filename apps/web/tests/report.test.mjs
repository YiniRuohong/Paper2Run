import test from 'node:test';
import assert from 'node:assert/strict';
import { parseReport } from '../src/features/autonomous/report.ts';

// parseReport is the type guard behind the report view (P0-6): it accepts only
// the exact structure the backend evidence.py report() emits. Anything else
// returns null so the page falls back to raw JSON instead of guessing.

const valid = () => ({
  status: 'completed',
  verdict: 'not_comparable',
  completion_gate: { entrypoint: 'train.py', frozen_code_sha256: 'a'.repeat(64), rule: 'exit 0 on the approved entrypoint' },
  successful_execution_observed: true,
  execution_observations: [
    { sequence: 3, tool: 'execute', command: 'python train.py', result: { exit_code: 0 }, matches_protocol_entrypoint: true },
    { sequence: 5, tool: 'read', command: '', result: {}, matches_protocol_entrypoint: false },
  ],
  evaluation: {
    verdict: 'not_comparable', comparison_eligible: false,
    reasons: ['no reference values'],
    metrics: { accuracy: 0.9, note: 'ignored' },
    confidence_intervals: { accuracy_95: { lower: 0.8, upper: 0.95 } },
    within_tolerance: null,
  },
  verification: null,
  artifacts: [{ name: 'metrics.json', kind: 'result', sha256: 'b'.repeat(64), bytes: 12, protocol_sha256: 'c'.repeat(64) }],
  missing_artifacts: [],
  missing_evidence_kinds: ['data'],
  model_narrative: 'I trained the model.',
  limitations: ['small dataset'],
  sha256: 'd'.repeat(64),
});

test('a backend-shaped report parses into the typed structure', () => {
  const report = parseReport(valid());
  assert.ok(report);
  assert.equal(report.verdict, 'not_comparable');
  assert.equal(report.completionGate.entrypoint, 'train.py');
  assert.equal(report.executionObservations.length, 2);
  assert.equal(report.executionObservations[0].exitCode, 0);
  assert.equal(report.executionObservations[1].command, '');
  assert.equal(report.executionObservations[1].matchesEntrypoint, false);
  assert.deepEqual(report.evaluation.metrics, { accuracy: 0.9 }, 'non-numeric metric entries are dropped');
  assert.deepEqual(report.evaluation.confidenceIntervals.accuracy_95, { lower: 0.8, upper: 0.95 });
  assert.equal(report.verification, null);
  assert.equal(report.modelNarrative, 'I trained the model.');
  assert.equal(report.sha256, 'd'.repeat(64));
});

test('verification comparisons parse with scope fallback and nullable numbers', () => {
  const source = valid();
  source.verification = {
    scope: 'paper-text', verdict: 'outside_tolerance', limitations: ['reference from text'],
    comparisons: [
      { metric: 'accuracy', value: 0.95, tolerance: 0.02, observed_value: 0.9, absolute_difference: 0.05, within_tolerance: false, verdict: 'outside_tolerance', source_url: 'https://example.test' },
      { metric: 'f1', value: null, tolerance: null, observed_value: null, absolute_difference: null, within_tolerance: null, verdict: 'not_comparable' },
    ],
  };
  const report = parseReport(source);
  assert.equal(report.verification.scope, 'paper-text');
  assert.equal(report.verification.comparisons.length, 2);
  assert.equal(report.verification.comparisons[0].referenceValue, 0.95);
  assert.equal(report.verification.comparisons[1].scope, 'paper-text', 'row scope falls back to the verification scope');
  assert.equal(report.verification.comparisons[1].referenceValue, null);
});

test('any structural mismatch returns null so the page shows raw JSON', () => {
  const bad = [
    null, [], 'report',
    { ...valid(), status: 7 },
    { ...valid(), verdict: undefined },
    { ...valid(), completion_gate: { entrypoint: 'x' } },
    { ...valid(), successful_execution_observed: 'yes' },
    { ...valid(), execution_observations: [{}] },
    { ...valid(), execution_observations: 'none' },
    { ...valid(), evaluation: { verdict: 'ok' } },
    { ...valid(), artifacts: [{ name: 'x' }] },
    { ...valid(), missing_artifacts: 'none' },
    { ...valid(), missing_evidence_kinds: [1] },
    { ...valid(), limitations: null },
    { ...valid(), model_narrative: undefined },
    { ...valid(), sha256: '' },
    { ...valid(), verification: { scope: 's' } },
    { ...valid(), verification: { scope: 's', verdict: 'v', limitations: [], comparisons: [{}] } },
  ];
  for (const value of bad) assert.equal(parseReport(value), null, JSON.stringify(value)?.slice(0, 80));
});

test('model narrative stays a plain string field, never promoted into evidence', () => {
  const source = valid();
  source.model_narrative = '';
  const report = parseReport(source);
  assert.equal(report.modelNarrative, '', 'empty narrative is still valid');
  assert.ok(!('narrative' in report.executionObservations[0]));
});
