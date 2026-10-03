import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { GuidedController } from '../src/features/guided/controller.ts';
import {
  pollExports,
  pollRunAndReport,
  readExtraction,
  readReport,
  requireRecord,
} from '../src/features/guided/readback.ts';

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// ---------------------------------------------------------------------------
// Page wiring contract (regression): an explicit read-only refresh must restart
// every failed read-only readback (report / export / run+report polling) even
// when no resource id changed, while keeping resource-switch isolation and
// aborting superseded reads. Since the offline teaching entry was removed the
// wiring lives in RealFlowPanel, the product surface.
// ---------------------------------------------------------------------------

const pageSource = await readFile(new URL('../src/features/guided/RealFlowPanel.tsx', import.meta.url), 'utf8');
const helperSource = await readFile(new URL('../src/features/guided/readback.ts', import.meta.url), 'utf8');

test('explicit refresh bumps a read counter that every read-only effect depends on', () => {
  assert.match(pageSource, /const \[readRev, setReadRev\] = useState\(0\);/);
  assert.match(pageSource, /void call\(async \(\) => \{ try \{ await controller\.pollRun\(\); await controller\.pollReport\(\); \} finally \{ setReadRev\(x => x \+ 1\); \} \}\)/, 'the 刷新（只读） button must restart readbacks even when polling fails');
  assert.match(pageSource, /import \{ pollExports, pollRunAndReport, readReport \} from '\.\/readback';/);
  assert.match(pageSource, /return pollRunAndReport\(\{ controller, onError/);
  assert.match(pageSource, /return readReport\(\{ api, runId: state\.runId/);
  assert.match(pageSource, /return pollExports\(\{ api, runId: state\.runId, exportId: state\.exportId/);
  assert.match(pageSource, /\}, \[controller, state\?\.runId, state\?\.report\?\.build_id, readRev\]\);/);
  assert.match(pageSource, /\}, \[api, state\?\.runId, state\?\.reportStatus, readRev\]\);/);
  assert.match(pageSource, /\}, \[api, state\?\.runId, state\?\.exportId, readRev\]\);/);
});

test('step changes remount the animated card while preserving focus and reduced motion', async () => {
  assert.match(pageSource, /<section key=\{state\.step\} ref=\{card\}/, 'each step needs a fresh animation target');
  assert.match(pageSource, /card\.current\?\.focus\(\)/);
  const css = await readFile(new URL('../src/features/guided/guided.css', import.meta.url), 'utf8');
  assert.match(css, /animation: guided-enter 600ms cubic-bezier\(0\.16, 1, 0\.3, 1\) both/);
  assert.match(css, /prefers-reduced-motion: reduce[^]*animation: none/);
});

test('resource switches reset readback state instead of leaking prior results', () => {
  assert.match(pageSource, /setReport\(null\); setExports\(\[\]\);/);
  assert.match(pageSource, /\}, \[state\?\.runId, state\?\.selectedCandidateId\]\);/, 'switching run or candidate must clear stale readback state');
  assert.doesNotMatch(pageSource, /readRev\+\+|setReadRev\([^)]*\)\s*\+\+/);
  assert.doesNotMatch(helperSource, /\.write\(|replayPending|startExample|startRun\(|retryReport|createExports|api\.download/, 'readback helpers must stay read-only');
});

// ---------------------------------------------------------------------------
// Helper behavior: restart after failure, abort of superseded reads, GET-only.
// ---------------------------------------------------------------------------

const extraction = { paper_id: 'paper', extraction_id: 'x', source_refs: [] };
const report = { run_id: 'r', status: 'ready' };
const exportShape = (id, status) => ({
  id,
  run_id: 'r',
  status,
  operation_id: 'op',
  report_hash: null,
  items: [],
  created_at: 't',
});

function mockApi({ readSequence = {}, readArraySequence = {} } = {}) {
  const calls = [];
  const readIndex = new Map();
  const arrayIndex = new Map();
  const take = (indexMap, path, sequence) => {
    const seen = indexMap.get(path) ?? 0;
    indexMap.set(path, seen + 1);
    return sequence[Math.min(seen, sequence.length - 1)] ?? {};
  };
  const hangUntilAborted = (signal) => new Promise((resolve, reject) => {
    const fail = () => reject(new DOMException('This operation was aborted', 'AbortError'));
    if (signal?.aborted) return fail();
    signal?.addEventListener('abort', fail, { once: true });
  });
  const api = {
    async read(path, validate, signal) {
      calls.push({ kind: 'GET', path, signal });
      if (!readSequence[path]) throw new Error(`unexpected read ${path}`);
      const step = take(readIndex, path, readSequence[path]);
      if (step.abortWait) await hangUntilAborted(signal);
      if (step.delayMs) await sleep(step.delayMs);
      if (step.fail) throw Object.assign(new Error('read failed'), { status: 503, code: 'RUNNER_UNAVAILABLE' });
      return validate(step.value);
    },
    async readArray(path, validate, signal) {
      calls.push({ kind: 'GET', path, signal });
      if (!readArraySequence[path]) throw new Error(`unexpected readArray ${path}`);
      const step = take(arrayIndex, path, readArraySequence[path]);
      if (step.abortWait) await hangUntilAborted(signal);
      if (step.delayMs) await sleep(step.delayMs);
      if (step.fail) throw Object.assign(new Error('read failed'), { status: 503 });
      return { items: (step.items ?? []).map(validate), has_more: false, next_cursor: null };
    },
    async write() { calls.push({ kind: 'POST' }); throw new Error('readback helpers must never write'); },
    async receipt() { throw new Error('unexpected receipt'); },
    async download() { throw new Error('unexpected download'); },
    async runEvents() { throw new Error('unexpected runEvents'); },
  };
  return { api, calls };
}

test('failed extraction readback recovers on explicit restart with a fresh read, GET-only', async () => {
  const { api, calls } = mockApi({ readSequence: { '/papers/paper/extraction': [{ fail: 1 }, { value: extraction }] } });
  const seen = [];
  const errors = [];
  const restart = () => readExtraction({ api, paperId: 'paper', extractionId: 'x', onValue: v => seen.push(v), onError: () => errors.push('extraction') });
  let stop = restart();
  await sleep(5);
  stop();
  assert.deepEqual(seen, []);
  assert.equal(errors.length, 1, 'the first failure must surface the page error');
  stop = restart(); // 刷新（只读）：same ids, new read generation
  await sleep(5);
  stop();
  assert.deepEqual(seen, [extraction]);
  assert.equal(errors.length, 1, 'the recovered readback must not raise a second error');
  assert.deepEqual(calls.map(c => c.path), ['/papers/paper/extraction', '/papers/paper/extraction']);
  assert.ok(calls.every(c => c.kind === 'GET'), 'restart issues reads only');
});

test('restart aborts the in-flight extraction readback and suppresses its late error', async () => {
  const { api, calls } = mockApi({ readSequence: { '/papers/paper/extraction': [{ abortWait: true }, { value: extraction }] } });
  const seen = [];
  const errors = [];
  const stop1 = readExtraction({ api, paperId: 'paper', extractionId: 'x', onValue: v => seen.push(v), onError: () => errors.push('extraction') });
  await sleep(2);
  const stop2 = readExtraction({ api, paperId: 'paper', extractionId: 'x', onValue: v => seen.push(v), onError: () => errors.push('extraction') });
  await sleep(5);
  stop1();
  stop2();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].signal.aborted, true, 'the superseded read must be aborted');
  assert.deepEqual(seen, [extraction]);
  assert.equal(errors.length, 0, 'an aborted read must not surface an error');
});

test('cleanup stops a late successful extraction response from being committed', async () => {
  const { api, calls } = mockApi({ readSequence: { '/papers/paper/extraction': [{ delayMs: 15, value: extraction }] } });
  const seen = [];
  const errors = [];
  const stop = readExtraction({ api, paperId: 'paper', extractionId: 'x', onValue: v => seen.push(v), onError: () => errors.push('extraction') });
  await sleep(3);
  stop(); // cleanup aborts the generation; the in-flight success must not commit afterwards
  await sleep(30);
  assert.equal(calls.length, 1);
  assert.deepEqual(seen, [], 'a response that resolves after cleanup must not be delivered');
  assert.equal(errors.length, 0, 'an aborted read must not surface an error either');
});

test('cleanup stops a late successful report response from being committed', async () => {
  const { api, calls } = mockApi({ readSequence: { '/runs/r/report': [{ delayMs: 15, value: report }] } });
  const seen = [];
  const errors = [];
  const stop = readReport({ api, runId: 'r', onValue: v => seen.push(v), onError: () => errors.push('report') });
  await sleep(3);
  stop();
  await sleep(30);
  assert.equal(calls.length, 1);
  assert.deepEqual(seen, [], 'a response that resolves after cleanup must not be delivered');
  assert.equal(errors.length, 0);
});

function controllerFixture() {
  const data = new Map();
  const calls = [];
  const storage = { getItem: k => data.get(k) ?? null, setItem: (k, v) => data.set(k, v) };
  const bootstrap = { project_id: 'p', paper_id: 'paper', extraction_id: 'x' };
  const experiment = { experiment_id: 'e', project_id: 'p', paper_id: 'paper', status: 'draft', head_revision_id: null, active_run_id: null, latest_run_id: null, repairs_used: 0, repairs_limit: 0, created_at: 't' };
  const revision = { id: 'v', experiment_id: 'e', revision_no: 1, spec_hash: 's', policy_hash: 'h', spec: {}, validation: { issues: [] }, confirmation: null, is_current: true, created_at: 't' };
  const confirmation = { confirmation_id: 'c', revision_id: 'v', spec_hash: 's', confirmed_by: 'demo-user', confirmed_at: 't' };
  const run = { run_id: 'r', project_id: 'p', experiment_id: 'e', revision_id: 'v', status: 'running', last_event_seq: 0, created_at: 't', updated_at: 't', error: null };
  const reportStatus = { run_id: 'r', status: 'pending', build_id: null, operation_id: null, report_hash: null, error: null };
  const fixture = { failNextRunRead: false, run, reportStatus };
  fixture.api = {
    async read(path, validate) {
      calls.push({ method: 'GET', path });
      if (path === '/runs/r') {
        if (fixture.failNextRunRead) { fixture.failNextRunRead = false; throw Object.assign(new Error('read failed'), { status: 503 }); }
        return validate(run);
      }
      const values = {
        '/demo': { actor_id: 'demo-user' },
        '/experiments/e/draft': { experiment_id: 'e', base_revision_id: null, spec: {}, issues: [], editable_fields: [] },
        '/spec-revisions/v': revision,
        '/runs/r/report-status': reportStatus,
      };
      return validate(values[path] ?? bootstrap);
    },
    async readArray() { throw new Error('unexpected readArray'); },
    async write(intent, validate) {
      calls.push({ method: 'POST', path: intent.path });
      if (intent.path === '/runs/r/report-builds') {
        Object.assign(reportStatus, { build_id: 'build-new', operation_id: 'op-new', status: 'pending' });
        return validate(reportStatus);
      }
      return validate(
        intent.path === '/demo/bootstrap' ? bootstrap
          : intent.path.endsWith('/experiments') ? experiment
            : intent.path.endsWith('/spec-revisions') ? revision
              : intent.path.endsWith('/runs') ? { run_id: 'r', status: 'queued', events_url: '/api/v1/runs/r/events', last_event_seq: 0 }
                : confirmation,
      );
    },
    async receipt() { throw Object.assign(new Error('missing'), { status: 404 }); },
    async download() { throw new Error('unexpected download'); },
    async runEvents(runId, after) { calls.push({ method: 'GET', path: `/runs/${runId}/events?after=${after}` }); return []; },
  };
  fixture.storage = storage;
  fixture.calls = calls;
  fixture.data = data;
  fixture.create = () => new GuidedController({ api: fixture.api, storage, actor: 'demo-user' });
  return fixture;
}

test('failed run/report polling stops and is restarted by explicit refresh without any POST', async () => {
  const f = controllerFixture();
  const c = f.create();
  await c.startExample(true);
  c.reviewExtraction('x');
  await c.confirmExperiment(true);
  await c.startRun();
  assert.deepEqual(
    f.calls.filter(x => x.method === 'POST').map(x => x.path),
    ['/demo/bootstrap', '/projects/p/experiments', '/projects/p/spec-revisions', '/spec-revisions/v/confirm', '/projects/p/runs'],
  );
  f.calls.length = 0;
  const errors = [];
  f.failNextRunRead = true;
  let stop = pollRunAndReport({ controller: c, onError: () => errors.push('poll'), initialDelayMs: 1, intervalMs: 5 });
  await sleep(10);
  stop();
  assert.equal(errors.length, 1, 'the failed poll must surface the page error once');
  assert.deepEqual(f.calls, [{ method: 'GET', path: '/runs/r' }], 'a failed poll stops; it never retries by itself');
  stop = pollRunAndReport({ controller: c, onError: () => errors.push('poll'), initialDelayMs: 1, intervalMs: 5 });
  await sleep(10);
  stop();
  assert.equal(errors.length, 1, 'the restarted polling must not raise a second error');
  assert.equal(f.calls.filter(x => x.method === 'POST').length, 0, 'explicit refresh restarts polling without any write');
  assert.ok(f.calls.some(x => x.method === 'GET' && x.path === '/runs/r'), 'restarted polling re-reads the run');
  assert.ok(f.calls.some(x => x.method === 'GET' && x.path === '/runs/r/report-status'), 'restarted polling re-reads report status');
  c.dispose();
});

for (const [runStatus, reportStatus] of [['completed', 'ready'], ['completed', null], ['completed', 'failed'], ['failed', null], ['cancelled', null], ['awaiting_revision', null]]) {
  test(`polling stops after ${runStatus}/${reportStatus} readback`, async () => {
    const f = controllerFixture();
    const c = f.create();
    await c.startExample(true);
    c.reviewExtraction('x');
    await c.confirmExperiment(true);
    await c.startRun();
    f.run.status = runStatus;
    f.reportStatus.status = reportStatus;
    f.calls.length = 0;
    const errors = [];
    const stop = pollRunAndReport({ controller: c, onError: () => errors.push('poll'), initialDelayMs: 0, intervalMs: 2 });
    try {
      await sleep(30);
      assert.deepEqual(errors, []);
      assert.equal(f.calls.filter(x => x.path === '/runs/r').length, 1, 'terminal readback must not schedule another poll');
      assert.equal(f.calls.filter(x => x.path === '/runs/r/report-status').length, 1);
      assert.ok(f.calls.every(x => x.method === 'GET'));
    } finally { stop(); c.dispose(); }
  });
}

test('a new accepted report build restarts the page polling effect', () => {
  assert.match(pageSource, /\}, \[controller, state\?\.runId, state\?\.report\?\.build_id, readRev\]\);/);
});

test('explicit report retry changes build identity and restarted polling reaches ready without extra writes', async () => {
  const f = controllerFixture();
  const c = f.create();
  await c.startExample(true);
  c.reviewExtraction('x');
  await c.confirmExperiment(true);
  await c.startRun();
  f.run.status = 'completed';
  Object.assign(f.reportStatus, { status: 'failed', build_id: 'build-old' });
  await c.pollRun();
  await c.pollReport();
  f.calls.length = 0;
  await c.retryReport();
  assert.equal(c.getSnapshot().report.build_id, 'build-new');
  assert.equal(c.getSnapshot().reportStatus, 'pending');
  const errors = [];
  const stop = pollRunAndReport({ controller: c, onError: () => errors.push('poll'), initialDelayMs: 0, intervalMs: 2 });
  try {
    await sleep(15);
    assert.ok(f.calls.filter(x => x.path === '/runs/r/report-status').length > 2);
    f.reportStatus.status = 'ready';
    await sleep(15);
    assert.equal(c.getSnapshot().reportStatus, 'ready');
    const count = f.calls.length;
    await sleep(15);
    assert.equal(f.calls.length, count);
    assert.deepEqual(f.calls.filter(x => x.method === 'POST').map(x => x.path), ['/runs/r/report-builds']);
    assert.deepEqual(errors, []);
  } finally { stop(); c.dispose(); }
});

test('completed run keeps polling while its report is generating, then stops at ready', async () => {
  const f = controllerFixture();
  const c = f.create();
  await c.startExample(true);
  c.reviewExtraction('x');
  await c.confirmExperiment(true);
  await c.startRun();
  f.run.status = 'completed';
  f.reportStatus.status = 'generating';
  f.calls.length = 0;
  const errors = [];
  const stop = pollRunAndReport({ controller: c, onError: () => errors.push('poll'), initialDelayMs: 0, intervalMs: 2 });
  try {
    await sleep(15);
    assert.ok(f.calls.filter(x => x.path === '/runs/r/report-status').length > 1);
    f.reportStatus.status = 'ready';
    await sleep(15);
    const count = f.calls.length;
    await sleep(15);
    assert.equal(f.calls.length, count, 'ready report ends polling');
    assert.deepEqual(errors, []);
  } finally { stop(); c.dispose(); }
});

test('failed report readback recovers on explicit restart, GET-only', async () => {
  const { api, calls } = mockApi({ readSequence: { '/runs/r/report': [{ fail: 1 }, { value: report }] } });
  const seen = [];
  const errors = [];
  const restart = () => readReport({ api, runId: 'r', onValue: v => seen.push(v), onError: () => errors.push('report') });
  let stop = restart();
  await sleep(5);
  stop();
  assert.equal(errors.length, 1);
  assert.deepEqual(seen, []);
  stop = restart();
  await sleep(5);
  stop();
  assert.deepEqual(seen, [report]);
  assert.equal(errors.length, 1);
  assert.deepEqual(calls.map(c => c.path), ['/runs/r/report', '/runs/r/report']);
  assert.ok(calls.every(c => c.kind === 'GET'));
});

test('failed export polling stops and restarts on explicit refresh; results stay export-scoped', async () => {
  const queued = exportShape('ex1', 'queued');
  const other = exportShape('ex2', 'queued');
  const succeeded = exportShape('ex1', 'succeeded');
  const { api, calls } = mockApi({ readArraySequence: { '/runs/r/exports': [{ fail: 1 }, { items: [other, queued] }, { items: [succeeded] }] } });
  const seen = [];
  const errors = [];
  const restart = () => pollExports({ api, runId: 'r', exportId: 'ex1', onValue: v => seen.push(v), onError: () => errors.push('exports'), intervalMs: 5 });
  let stop = restart();
  await sleep(20);
  stop();
  assert.equal(errors.length, 1, 'the failed export poll must surface the page error once');
  assert.equal(calls.length, 1, 'a failed export poll stops; it never retries by itself');
  stop = restart(); // 刷新（只读）：same runId/exportId, fresh poll
  await sleep(20);
  stop();
  assert.equal(errors.length, 1, 'the restarted export poll must not raise a second error');
  assert.deepEqual(seen, [[queued], [succeeded]], 'exports are filtered to the selected export of this run');
  assert.equal(calls.length, 3, 'a succeeded export ends the poll loop without further reads');
  assert.ok(calls.every(c => c.kind === 'GET'));
});

test('restart aborts the in-flight export poll without surfacing its error', async () => {
  const queued = exportShape('ex1', 'queued');
  const { api, calls } = mockApi({ readArraySequence: { '/runs/r/exports': [{ abortWait: true }, { items: [queued] }] } });
  const seen = [];
  const errors = [];
  const stop1 = pollExports({ api, runId: 'r', exportId: 'ex1', onValue: v => seen.push(v), onError: () => errors.push('exports') });
  await sleep(2);
  const stop2 = pollExports({ api, runId: 'r', exportId: 'ex1', onValue: v => seen.push(v), onError: () => errors.push('exports') });
  await sleep(5);
  stop1();
  stop2();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].signal.aborted, true, 'the superseded export poll must be aborted');
  assert.deepEqual(seen, [[queued]]);
  assert.equal(errors.length, 0, 'an aborted export poll must not surface an error');
});

test('readback DTO guard rejects non-object payloads', () => {
  assert.throws(() => requireRecord(null), /INVALID_DTO/);
  assert.throws(() => requireRecord('x'), /INVALID_DTO/);
  assert.throws(() => requireRecord([1]), /INVALID_DTO/);
  assert.deepEqual(requireRecord({ a: 1 }), { a: 1 });
});
