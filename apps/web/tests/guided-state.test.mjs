import test from 'node:test';
import assert from 'node:assert/strict';
import { createGuidedState, guidedReducer, canViewStep } from '../src/features/guided/state.ts';

const bootstrap = { project_id: 'p', paper_id: 'paper', extraction_id: 'x' };
function reviewed() {
  let s = guidedReducer(createGuidedState(), { type: 'bootstrap', epoch: 0, value: bootstrap });
  return guidedReducer(s, { type: 'review', extractionId: 'x' });
}
const run = { run_id: 'r', project_id: 'p', experiment_id: 'e', revision_id: 'v', status: 'completed', last_event_seq: 2, created_at: 't', updated_at: 't', error: null };

test('six-step gates require verified extraction and confirmation', () => {
  let s = createGuidedState();
  assert.equal(s.viewStep, 'paper');
  assert.equal(canViewStep(s, 'paper'), true);
  assert.equal(canViewStep(s, 'extraction'), false);
  s = guidedReducer(s, { type: 'bootstrap', epoch: 0, value: bootstrap });
  assert.equal(canViewStep(s, 'extraction'), true);
  assert.equal(canViewStep(s, 'experiment'), false);
  assert.equal(guidedReducer(s, { type: 'review', extractionId: 'wrong' }), s);
  s = guidedReducer(s, { type: 'review', extractionId: 'x' });
  assert.equal(canViewStep(s, 'experiment'), true);
  assert.equal(canViewStep(s, 'run'), false);
  assert.notEqual(guidedReducer(s, { type: 'view', step: 'run' }).error, null);
});

test('failed and cancelled runs do not unlock reports; ready reports do not navigate', () => {
  for (const status of ['failed', 'cancelled', 'awaiting_revision']) {
    const s = { ...reviewed(), run: { ...run, status } };
    assert.equal(canViewStep(s, 'report'), false);
  }
  let s = { ...reviewed(), run, viewStep: 'report' };
  s = guidedReducer(s, { type: 'report', epoch: 0, value: { run_id: 'r', status: 'ready', build_id: 'b', operation_id: 'o', report_hash: 'hash', error: null } });
  assert.equal(s.viewStep, 'report');
  assert.equal(canViewStep(s, 'export'), true);
});

test('viewing backwards preserves refs; editing archives downstream and advances epoch', () => {
  const s = { ...reviewed(), run, runId: 'r', revisionId: 'v', confirmationId: 'c', exportIds: ['z'], viewStep: 'run' };
  const viewed = guidedReducer(s, { type: 'view', step: 'paper' });
  assert.equal(viewed.run, run);
  assert.equal(viewed.epoch, 0);
  const edited = guidedReducer(s, { type: 'edit' });
  assert.equal(edited.epoch, 1);
  assert.equal(edited.runId, null);
  assert.equal(edited.confirmationId, null);
  assert.equal(edited.run, null);
  assert.deepEqual(edited.history, [{ revisionId: 'v', runId: 'r', exportIds: ['z'] }]);
  assert.equal(guidedReducer(edited, { type: 'run', epoch: 0, value: run }), edited);
});

test('events are run-scoped, ordered and deduplicated', () => {
  const event = (id, seq) => ({ schema_version: '1.0', run_id: id, seq, type: 'run.status', occurred_at: 't', payload: {} });
  let s = { ...reviewed(), runId: 'r', run };
  s = guidedReducer(s, { type: 'events', epoch: 0, value: [event('r', 2), event('r', 0), event('other', 1)] });
  s = guidedReducer(s, { type: 'events', epoch: 0, value: [event('r', 2), event('r', 1)] });
  assert.deepEqual(s.events.map(x => x.seq), [0, 1, 2]);
});

test('unrelated run and report responses cannot replace current resources', () => {
  const s = { ...reviewed(), runId: 'r', experimentId: 'e', revisionId: 'v', run };
  assert.equal(guidedReducer(s, { type: 'run', epoch: 0, value: { ...run, run_id: 'other' } }), s);
  assert.equal(guidedReducer(s, { type: 'report', epoch: 0, value: { run_id: 'other', status: 'ready' } }), s);
});
