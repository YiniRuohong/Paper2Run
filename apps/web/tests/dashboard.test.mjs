import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { GuidedApiError } from '../src/api-client/guided.ts';
import { describeError, loadSnapshot } from '../src/features/dashboard/data.ts';

const dashboard = await readFile(new URL('../src/features/dashboard/Dashboard.tsx', import.meta.url), 'utf8');
const data = await readFile(new URL('../src/features/dashboard/data.ts', import.meta.url), 'utf8');
const page = await readFile(new URL('../src/features/guided/GuidedPage.tsx', import.meta.url), 'utf8');

// Fake GuidedApi: `routes` maps a path to rows, or to an Error to throw.
const fakeApi = routes => ({
  async readArray(path, validator) {
    const hit = routes[path];
    if (hit instanceof Error) throw hit;
    if (!hit) throw new GuidedApiError('not found', 404, 'NOT_FOUND');
    return { items: hit.map(validator), has_more: false, next_cursor: null };
  },
});
const signal = new AbortController().signal;

test('dashboard is read-only: GET readers only, no write or persistence calls', () => {
  for (const source of [dashboard, data]) {
    assert.doesNotMatch(source, /api\.write\(|createProject|uploadPaper|createTextPaper|createExtraction|createCodegen|download\(/);
    assert.doesNotMatch(source, /localStorage\.|sessionStorage\./);
  }
  assert.match(data, /api\.readArray\(/);
});

test('shell routes between overview and the guided new-task flow', () => {
  assert.match(page, /<Dashboard api=\{api\} onNew=/);
  assert.match(page, /<RealFlowPanel api=\{api\} \/>/);
  assert.match(page, /#\/new/);
  assert.doesNotMatch(page, /离线教学/);
});

test('loadSnapshot links papers, experiments and runs and drops malformed rows', async () => {
  const api = fakeApi({
    '/projects': [{ id: 'p1' }, { nope: true }],
    '/projects/p1/papers': [{ paper_id: 'pap_a', title: 'A', status: 'ready', created_at: '2026-10-03T00:00:00Z' }, 42],
    '/projects/p1/experiments': [{ experiment_id: 'exp_a', paper_id: 'pap_a' }],
    '/projects/p1/runs': [{ run_id: 'run_a', experiment_id: 'exp_a', status: 'completed', verdict: 'matched', mode: 'local-cpu', created_at: '2026-10-03T01:00:00Z' }],
  });
  const { snapshot, issues } = await loadSnapshot(api, signal);
  assert.equal(snapshot.papers.length, 1);
  assert.equal(snapshot.runs[0].verdict, 'matched');
  assert.equal(snapshot.experiments[0].paperId, 'pap_a');
  assert.ok(issues.some(i => /格式异常/.test(i.reason)), 'a malformed row is reported, not fatal');
});

test('a failing resource degrades to a warning; the rest of the page still loads', async () => {
  const api = fakeApi({
    '/projects': [{ id: 'p1' }],
    '/projects/p1/papers': [{ paper_id: 'pap_a', status: 'ready' }],
    '/projects/p1/experiments': new GuidedApiError('boom', 500, 'INTERNAL'),
    '/projects/p1/runs': [],
  });
  const { snapshot, issues } = await loadSnapshot(api, signal);
  assert.equal(snapshot.papers.length, 1);
  assert.equal(issues.length, 1);
});

test('an uninitialised user (403 on /projects) is an empty state, not an outage', async () => {
  const api = fakeApi({ '/projects': new GuidedApiError('forbidden', 403, 'FORBIDDEN') });
  const { snapshot } = await loadSnapshot(api, signal);
  assert.deepEqual(snapshot, { papers: [], experiments: [], runs: [] });
});

test('a dead backend surfaces as a classified, actionable error', async () => {
  const api = fakeApi({ '/projects': new GuidedApiError('网络连接失败', 0, 'NETWORK_ERROR') });
  await assert.rejects(loadSnapshot(api, signal), error => {
    const info = describeError(error);
    assert.equal(info.kind, 'offline');
    assert.ok(info.steps.length >= 2);
    assert.match(info.detail, /NETWORK_ERROR/);
    return true;
  });
});

test('describeError classifies auth, server and unknown failures', () => {
  assert.equal(describeError(new GuidedApiError('x', 401, 'UNAUTHENTICATED')).kind, 'auth');
  assert.equal(describeError(new GuidedApiError('x', 502, 'BAD_GATEWAY')).kind, 'offline');
  assert.equal(describeError(new GuidedApiError('x', 500, 'INTERNAL')).kind, 'server');
  // dev proxy with a dead target: HTTP 500, empty body, client fallback code
  assert.equal(describeError(new GuidedApiError('请求失败 (HTTP 500)', 500, 'UNKNOWN_ERROR')).kind, 'offline');
  assert.equal(describeError(new Error('weird')).kind, 'unknown');
});

const detail = await readFile(new URL('../src/features/dashboard/TaskDetail.tsx', import.meta.url), 'utf8');

test('task detail is wired by hash route and stays read-only', () => {
  assert.match(page, /#\/task\//);
  assert.match(page, /TaskDetail/);
  assert.doesNotMatch(detail, /api\.write\(|localStorage\.|sessionStorage\./);
  assert.match(dashboard, /#\/task\//);
});

test('loadReport maps a report and treats 409 as not generated', async () => {
  const { loadReport } = await import('../src/features/dashboard/data.ts');
  const report = {
    recipe: { id: 'r', version: '1' }, metrics: { accuracy: 0.5, total: 4 }, evidence_class: 'sandboxed_model_code',
    verdict: 'not_comparable', evaluator_version: 'e1', manifest: { manifest_hash: 'abc' },
    evidence: [{ sha256: 'f'.repeat(64), bytes: 3, media_type: 'text/plain' }], limitations: ['x'],
  };
  const ok = { async read() { return report; } };
  const view = await loadReport(ok, 'run_1', signal);
  assert.equal(view.recipe, 'r/1');
  assert.deepEqual(view.metrics, [['accuracy', '0.5000'], ['total', '4']]);
  assert.equal(view.evidence.length, 1);
  const pending = { async read() { throw new GuidedApiError('n', 409, 'REPORT_NOT_READY'); } };
  assert.equal(await loadReport(pending, 'run_1', signal), null);
});
