import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createGuidedApi } from '../src/api-client/guided.ts';
import { RealFlowController } from '../src/features/guided/realflow.ts';
import { canRealViewStep, createRealFlowState, realFlowReducer, realFlowSteps } from '../src/features/guided/realflow-state.ts';

// Sandbox flow (recipe sandbox_python): 生成复现代码 → 沙箱执行. Everything here
// runs against a mocked fetch through the real createGuidedApi wrapper, so the
// wire contract (X-Actor-ID, Idempotency-Key, exact bodies) is asserted at the
// transport level, not at a friendly stub. The controller never gains a
// persistence layer; the provider api_key stays in memory only.

const CODE_0 = 'import json\n\npredictions = [{"id": "a", "prediction": 1}]\nwith open("predictions.json", "w") as f:\n    json.dump(predictions, f)\n';
const CODE_1 = 'import json\n\nlabels = {"a": 1}\nwith open("labels.json", "w") as f:\n    json.dump(labels, f)\n';
const NOTES_0 = 'stdlib only; deterministic seed 7';
const SHA_A = 'a'.repeat(64);
const SHA_B = 'b'.repeat(64);
const PAPER_SHA = 'c'.repeat(64);

const codegenAccepted = (index, code, sha, notes) => ({ attempt_id: `att${index}`, attempt_index: index, code, code_sha256: sha, notes });

const project = { id: 'prj', name: '真实文章复现', role: 'owner', version: 1, created_at: 't' };
const paperAccepted = { paper_id: 'paper', operation_id: 'op', status: 'uploaded' };
const paperInfo = {
  paper_id: 'paper', project_id: 'prj', title: null, status: 'uploaded', sha256: PAPER_SHA,
  page_count: 0, content_available: true, current_extraction_id: null, active_operation_id: null,
  error: null, created_at: 't',
};
const sessionSummary = {
  provider_session_id: 'ps_1', endpoint_alias: 'api.example.com', model_id: 'gpt-x',
  base_url: 'https://api.example.com/v1', secret_configured: true, secret_last4: '••••k3y',
};
const cpuCandidate = {
  id: 'c-real', label: 'Real CPU baseline', recipe_id: 'real_cpu_baseline', recipe_version: '1',
  support_status: 'supported', execution_support: 'supported', unsupported_reasons: [],
  parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {},
  estimated_runtime: null, source_refs: [], provenance: 'article_extraction',
};
const sandboxCandidate = {
  id: 'c-sb', label: 'Sandbox python repro', recipe_id: 'sandbox_python', recipe_version: '1',
  support_status: 'supported', execution_support: 'supported', unsupported_reasons: [],
  parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {},
  estimated_runtime: null, source_refs: [], provenance: 'article_extraction',
};
const extractionPayload = {
  extraction_id: 'x1', paper_id: 'paper', schema_version: '1.0',
  fields: { title: { value: 'A paper', evidence_status: 'supported', source_refs: [] } },
  methods: [], formulas: [], candidate_experiments: [cpuCandidate, sandboxCandidate],
  source_refs: [], warnings: [], provider: { model_id: 'gpt-x' },
};
const experiment = { experiment_id: 'e', project_id: 'prj', paper_id: 'paper', status: 'draft', head_revision_id: null, active_run_id: null, latest_run_id: null, repairs_used: 0, repairs_limit: 0, created_at: 't' };
const draft = { experiment_id: 'e', base_revision_id: null, policy_hash: 'ph', spec: { schema_version: '1.0', training: { seed: null, batch_size: null, max_epochs: null, learning_rate: null } }, editable_fields: [], issues: [], comparison_precheck: { status: 'unresolved', reasons: [] } };
const revision = { id: 'v', experiment_id: 'e', revision_no: 1, spec_hash: 's', policy_hash: 'h', spec: draft.spec, validation: { issues: [] }, comparison_precheck: null, budget_estimate: null, confirmation: null, is_current: true, created_at: 't' };
const confirmation = { confirmation_id: 'c', revision_id: 'v', spec_hash: 's', confirmed_by: 'demo-user', confirmed_at: 't' };
const envCheck = {
  environment_check_id: 'env1', project_id: 'prj', experiment_id: 'e', revision_id: 'v',
  decision: 'ready', status: 'ready', checker_version: 'environment-checker.v1',
  requirements: {}, repair_suggestions: [],
  items: [{ key: 'python', status: 'pass', required: false, observed: '3.12.0', required_value: null, suggestion: '' }],
  created_at: 't',
};
const runAccepted = { run_id: 'r', status: 'queued', events_url: '/api/v1/runs/r/events', last_event_seq: 0 };
const run = { run_id: 'r', project_id: 'prj', experiment_id: 'e', revision_id: 'v', status: 'running', last_event_seq: 1, created_at: 't', updated_at: 't', error: null };

function envelope(data, status = 200) {
  return new Response(JSON.stringify({ data, meta: { request_id: 'req-ok' } }), { status, headers: { 'Content-Type': 'application/json' } });
}
function errorEnvelope(status, code, message) {
  return new Response(JSON.stringify({ error: { code, message }, meta: { request_id: 'req-err' } }), { status, headers: { 'Content-Type': 'application/json' } });
}

function harness() {
  const h = { calls: [], codegenQueue: [], overrides: new Map() };
  h.fetcher = async (path, init = {}) => {
    const method = init.method ?? 'GET';
    const call = { path, method, headers: new Headers(init.headers), body: init.body };
    h.calls.push(call);
    const route = `${method} ${path.replace('/api/v1', '').split('?')[0]}`;
    if (h.overrides.has(route)) return h.overrides.get(route)(init);
    if (route === 'POST /experiments/e/codegen') {
      const respond = h.codegenQueue.length > 0 ? h.codegenQueue.shift() : () => envelope(codegenAccepted(0, CODE_0, SHA_A, NOTES_0));
      return respond(init);
    }
    switch (route) {
      case 'POST /projects': return envelope(project, 201);
      case 'POST /projects/prj/papers/text': return envelope(paperAccepted, 202);
      case 'GET /papers/paper': return envelope(paperInfo);
      case 'POST /provider-sessions': return envelope(sessionSummary, 201);
      case 'POST /papers/paper/extractions': return envelope({ paper_id: 'paper', operation_id: 'op', status: 'ready', extraction_id: 'x1' }, 202);
      case 'GET /papers/paper/extraction': return envelope(extractionPayload);
      case 'POST /projects/prj/experiments': return envelope(experiment, 201);
      case 'GET /experiments/e/draft': return envelope(draft);
      case 'POST /projects/prj/spec-revisions': return envelope(revision, 201);
      case 'GET /spec-revisions/v': return envelope(revision);
      case 'POST /spec-revisions/v/confirm': return envelope(confirmation, 201);
      case 'POST /projects/prj/experiments/e/environment-checks': return envelope(envCheck, 201);
      case 'GET /experiments/e/attempts/0/code': return envelope({ code: CODE_0, code_sha256: SHA_A, notes: NOTES_0 });
      case 'GET /experiments/e/attempts/1/code': return envelope({ code: CODE_1, code_sha256: SHA_B, notes: null });
      case 'POST /projects/prj/runs': return envelope(runAccepted, 202);
      case 'GET /runs/r': return envelope(run);
      case 'GET /runs/r/events': return new Response('', { headers: { 'Content-Type': 'text/event-stream' } });
      default: throw new Error(`unexpected fetch ${route}`);
    }
  };
  h.api = createGuidedApi('demo-user', h.fetcher);
  h.create = () => new RealFlowController({ api: h.api });
  return h;
}

async function reachConfirmed(c, candidateId) {
  await c.uploadArticle({ consent: true, text: 'A real paper about a method.' });
  await c.createSession({ baseUrl: 'https://api.example.com/v1', modelId: 'gpt-x', apiKey: 'sk-live-SECRET', endpointAlias: '' });
  await c.runExtraction();
  c.selectCandidate(candidateId);
  await c.confirmSpec(true);
}

// ---------------------------------------------------------------------------
// step rail: the 复现代码 step is kept for every recipe (codegen globally open)
// ---------------------------------------------------------------------------

test('the step rail keeps the 复现代码 step for every recipe; viewing it still needs the confirmed chain', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  assert.deepEqual(realFlowSteps(s), ['input', 'provider', 'extract', 'spec', 'environment', 'codegen', 'execute'], 'codegen is open regardless of the selected recipe');
  s = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  assert.equal(realFlowSteps(s).includes('codegen'), true);
  s = realFlowReducer(s, { type: 'select', candidateId: 'c-sb' });
  assert.deepEqual(realFlowSteps(s), ['input', 'provider', 'extract', 'spec', 'environment', 'codegen', 'execute']);
  assert.equal(canRealViewStep(s, 'codegen'), false, 'codegen needs the confirmed experiment (the write targets /experiments/{eid}/codegen)');
  s = realFlowReducer(s, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  assert.equal(canRealViewStep(s, 'codegen'), true);
});

// ---------------------------------------------------------------------------
// codegen three states + wire contract
// ---------------------------------------------------------------------------

test('codegen: idle → loading while in flight → ready, with the exact wire body and headers', async () => {
  const h = harness(), c = h.create();
  await reachConfirmed(c, 'c-sb');
  assert.equal(c.getSnapshot().codegenPhase, 'idle');
  let release;
  h.codegenQueue.push(() => new Promise(resolve => { release = resolve; }));
  const pending = c.generateCode();
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(c.getSnapshot().codegenPhase, 'loading', 'the request is genuinely in flight');
  assert.equal(c.getSnapshot().codegen, null);
  release(envelope(codegenAccepted(0, CODE_0, SHA_A, NOTES_0)));
  await pending;
  const s = c.getSnapshot();
  assert.equal(s.codegenPhase, 'ready');
  assert.equal(s.codegen.attemptId, 'att0');
  assert.equal(s.codegen.attemptIndex, 0);
  assert.equal(s.codegen.code, CODE_0, 'the code is kept verbatim, newlines included');
  assert.equal(s.codegen.codeSha256, SHA_A);
  assert.equal(s.codegen.notes, NOTES_0);
  const call = h.calls.find(x => x.method === 'POST' && x.path === '/api/v1/experiments/e/codegen');
  assert.ok(call, 'the codegen write hit the contract endpoint');
  assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
  assert.ok(call.headers.get('Idempotency-Key'), 'the write carries an Idempotency-Key');
  assert.equal(call.headers.get('Content-Type'), 'application/json');
  assert.deepEqual(JSON.parse(call.body), { provider_session_id: 'ps_1', reason: 'guided-real-flow-codegen' });
  assert.equal(JSON.stringify(s).includes('sk-live-SECRET'), false, 'no api_key material may leak into flow state');
});

test('codegen without a provider session omits the field instead of sending null', async () => {
  const h = harness(), c = h.create();
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  c.selectCandidate('c-sb');
  await c.confirmSpec(true);
  await c.generateCode();
  const call = h.calls.find(x => x.method === 'POST' && x.path === '/api/v1/experiments/e/codegen');
  assert.deepEqual(Object.keys(JSON.parse(call.body)), ['reason']);
});

test('codegen rejection lands in the error state with the server code and keeps the run gate closed', async () => {
  const h = harness(), c = h.create();
  await reachConfirmed(c, 'c-sb');
  await c.checkEnvironment();
  h.codegenQueue.push(() => errorEnvelope(422, 'CODEGEN_PROVIDER_REQUIRED', 'provider session has no secret'));
  await assert.rejects(c.generateCode(), (error) => {
    assert.equal(error.code, 'CODEGEN_PROVIDER_REQUIRED');
    assert.equal(error.status, 422);
    return true;
  });
  const s = c.getSnapshot();
  assert.equal(s.codegenPhase, 'error');
  assert.equal(s.codegen, null);
  assert.ok(s.error.includes('CODEGEN_PROVIDER_REQUIRED'), 'the honest server code is surfaced');
  await assert.rejects(c.startRun('local-cpu'), /SANDBOX_CODE_REQUIRED/);
  assert.equal(h.calls.some(x => x.method === 'POST' && x.path === '/api/v1/projects/prj/runs'), false, 'no Run write may leave without generated code');
});

// ---------------------------------------------------------------------------
// run gate: sandbox needs code; other recipes unchanged
// ---------------------------------------------------------------------------

test('run gate: sandbox_python refuses Runs until code exists; other recipes are untouched', async () => {
  const h = harness(), c = h.create();
  await reachConfirmed(c, 'c-sb');
  await c.checkEnvironment();
  await assert.rejects(c.startRun('local-cpu'), /SANDBOX_CODE_REQUIRED/);
  assert.equal(h.calls.some(x => x.method === 'POST' && x.path === '/api/v1/projects/prj/runs'), false);
  await c.generateCode();
  await c.startRun('local-cpu');
  const runCall = h.calls.find(x => x.method === 'POST' && x.path === '/api/v1/projects/prj/runs');
  assert.deepEqual(JSON.parse(runCall.body), { spec_revision_id: 'v', confirmation_id: 'c' }, 'the sandbox gate does not alter the run body');

  const h2 = harness(), c2 = h2.create();
  await reachConfirmed(c2, 'c-real');
  await c2.checkEnvironment();
  await c2.startRun('local-cpu');
  assert.ok(h2.calls.some(x => x.method === 'POST' && x.path === '/api/v1/projects/prj/runs'), 'real_cpu_baseline runs with no codegen at all');
  // codegen is globally open now: non-sandbox recipes may generate code too,
  // and the same contract endpoint serves them.
  await c2.generateCode();
  const codegenCall = h2.calls.find(x => x.method === 'POST' && x.path === '/api/v1/experiments/e/codegen');
  assert.ok(codegenCall, 'codegen is allowed for non-sandbox recipes');
  assert.deepEqual(JSON.parse(codegenCall.body), { provider_session_id: 'ps_1', reason: 'guided-real-flow-codegen' });
  assert.equal(c2.getSnapshot().codegenPhase, 'ready');
});

// ---------------------------------------------------------------------------
// regenerate + readback
// ---------------------------------------------------------------------------

test('regenerating mints a fresh idempotency key and replaces the attempt; a mismatching readback fails visibly', async () => {
  const h = harness(), c = h.create();
  await reachConfirmed(c, 'c-sb');
  h.codegenQueue.push(() => envelope(codegenAccepted(0, CODE_0, SHA_A, NOTES_0)));
  h.codegenQueue.push(() => envelope(codegenAccepted(1, CODE_1, SHA_B, null)));
  await c.generateCode();
  await c.generateCode();
  const posts = h.calls.filter(x => x.method === 'POST' && x.path === '/api/v1/experiments/e/codegen');
  assert.equal(posts.length, 2);
  assert.notEqual(posts[0].headers.get('Idempotency-Key'), posts[1].headers.get('Idempotency-Key'), '重新生成 means a new idempotency key');
  assert.deepEqual(JSON.parse(posts[1].body), JSON.parse(posts[0].body), 'the body stays {provider_session_id, reason}');
  const s = c.getSnapshot();
  assert.equal(s.codegen.attemptIndex, 1);
  assert.equal(s.codegen.code, CODE_1);
  assert.equal(s.codegen.notes, null);

  await c.readCode();
  assert.ok(h.calls.some(x => x.method === 'GET' && x.path === '/api/v1/experiments/e/attempts/1/code'), 'readback targets the latest attempt index');
  h.overrides.set('GET /experiments/e/attempts/1/code', () => envelope({ code: 'print("tampered")', code_sha256: 'd'.repeat(64), notes: null }));
  await assert.rejects(c.readCode(), /CODE_READBACK_MISMATCH/);
  assert.equal(c.getSnapshot().codegen.code, CODE_1, 'a mismatching readback never silently swaps the displayed code');
});

test('the api client rejects a codegen readback whose sha256 is not a real hash', async () => {
  const h = harness();
  h.overrides.set('GET /experiments/e/attempts/0/code', () => envelope({ code: 'x', code_sha256: 'not-a-hash', notes: null }));
  await assert.rejects(h.api.readAttemptCode('e', 0), (error) => {
    assert.equal(error.code, 'INVALID_DTO');
    assert.match(error.message, /64-character SHA-256/);
    return true;
  });
});

// ---------------------------------------------------------------------------
// state hygiene + honest panel wording
// ---------------------------------------------------------------------------

test('switching candidates drops the generated code together with the experiment chain', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  s = realFlowReducer(s, { type: 'select', candidateId: 'c-sb' });
  s = realFlowReducer(s, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  s = realFlowReducer(s, { type: 'codegen', value: { attemptId: 'att0', attemptIndex: 0, code: CODE_0, codeSha256: SHA_A, notes: null } });
  assert.equal(s.codegenPhase, 'ready');
  const switched = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  assert.equal(switched.codegen, null);
  assert.equal(switched.codegenPhase, 'idle');
  assert.equal(switched.experimentId, null);
});

test('the panel renders the honest sandbox wording and gates the Run button with a reason', async () => {
  const panel = await readFile(new URL('../src/features/guided/RealFlowPanel.tsx', import.meta.url), 'utf8');
  assert.match(panel, /sandboxed_model_code/, 'the sandbox evidence class is mapped');
  assert.match(panel, /沙箱内执行模型生成代码（断网、限额、仅标准库）——近似复现，非论文原代码原数据/);
  assert.match(panel, /生成复现代码/, 'the codegen step is rendered');
  assert.match(panel, /sandboxCodeMissing/, 'code-missing gate disables the Run entry');
  assert.match(panel, /需先在「复现代码」步骤生成代码后才能创建 Run/, 'the disabled Run states the honest user-facing reason');
  assert.match(panel, /realFlowSteps/, 'the step rail is derived from the selected candidate');
  assert.match(panel, /code_sha256/, 'the hash is displayed next to the code');
  assert.doesNotMatch(panel, /localStorage\./, 'still no persistence for the real flow');
});
