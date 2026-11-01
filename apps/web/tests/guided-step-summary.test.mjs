import test from 'node:test';
import assert from 'node:assert/strict';
import { createRealFlowState } from '../src/features/guided/realflow-state.ts';
import { nextViewableStep, stepRows } from '../src/features/guided/realflow-summary.ts';

const row = (state, step) => stepRows(state).find(r => r.step === step);
const paper = { paper_id: 'pap_1', project_id: 'p', title: 'T', status: 'ready', sha256: 'a'.repeat(64), page_count: 1, content_available: true, current_extraction_id: null, active_operation_id: null, error: null, created_at: '' };
const confirmed = { ...createRealFlowState(), step: 'environment', paper, extractionPhase: 'ready', confirmationId: 'c', experimentId: 'e', revisionId: 'r' };

test('a fresh flow: only the first step is current, the rest are locked', () => {
  const marks = stepRows(createRealFlowState()).map(r => r.mark);
  assert.deepEqual(marks, ['current', 'locked', 'locked', 'locked', 'locked', 'locked', 'locked']);
  assert.equal(nextViewableStep(createRealFlowState()), null);
});

test('marks reuse the existing view gate: viewable but unfinished steps stay reachable', () => {
  const s = { ...createRealFlowState(), step: 'provider', paper };
  assert.equal(row(s, 'input').mark, 'done');
  assert.equal(row(s, 'extract').mark, 'open', 'skipping the provider must stay possible');
  assert.equal(row(s, 'spec').mark, 'locked');
  assert.equal(nextViewableStep(s), 'extract');
});

test('a blocked environment or a fake SSH verification never gets a success check', () => {
  const blocked = { ...confirmed, step: 'spec', envCheck: { decision: 'blocked' } };
  assert.equal(row(blocked, 'environment').mark, 'attention');
  const viaSsh = { ...blocked, ssh: { status: 'awaiting_confirmation' } };
  assert.equal(row(viaSsh, 'environment').mark, 'attention');
  assert.match(row(viaSsh, 'environment').summary, /fake/);
  const ready = { ...confirmed, step: 'spec', envCheck: { decision: 'ready' } };
  assert.equal(row(ready, 'environment').mark, 'done');
});

test('codegen summary distinguishes required, optional and generated', () => {
  const candidate = recipe => ({ id: 'c1', candidate_id: null, label: 'L', recipe_id: recipe, execution_support: 'supported' });
  const withRecipe = recipe => ({ ...confirmed, extraction: { candidate_experiments: [candidate(recipe)] }, selectedCandidateId: 'c1' });
  assert.equal(row(withRecipe('sandbox_python'), 'codegen').mark, 'attention');
  assert.match(row(withRecipe('other'), 'codegen').summary, /可选/);
  const generated = { ...withRecipe('sandbox_python'), codegen: { attemptId: 'a', attemptIndex: 2, code: 'x', codeSha256: 'f'.repeat(64), notes: null } };
  assert.equal(row(generated, 'codegen').mark, 'done');
});

test('only a completed run reads as done; an SSH-recorded run says nothing was executed', () => {
  const base = { ...confirmed, step: 'environment', envCheck: { decision: 'ready' }, runId: 'run' };
  assert.equal(row({ ...base, run: { status: 'completed' } }, 'execute').mark, 'done');
  assert.equal(row({ ...base, run: { status: 'failed' } }, 'execute').mark, 'attention');
  const created = row({ ...base, run: { status: 'created' } }, 'execute');
  assert.equal(created.mark, 'attention');
  assert.match(created.summary, /没有任何执行/);
  assert.equal(row({ ...base, run: { status: 'running' } }, 'execute').mark, 'open');
});
