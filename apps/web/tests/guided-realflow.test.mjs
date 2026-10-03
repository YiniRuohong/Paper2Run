import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { RealFlowController } from '../src/features/guided/realflow.ts';
import {
  applyTrainingToSpec,
  candidateBlockedReason,
  candidateKey,
  canRealViewStep,
  createRealFlowState,
  findCandidate,
  parseTrainingForm,
  realFlowReducer,
} from '../src/features/guided/realflow-state.ts';

// Real article flow: state gates, candidate selection, training merge, and the
// controller against a mocked GuidedApi. The controller has no storage option
// at all — the provider api_key must never reach any persistence.

const PAPER_SHA = 'b'.repeat(64);
const project = { id: 'prj', name: '真实文章复现', role: 'owner', version: 1, created_at: 't' };
const paperInfo = {
  paper_id: 'paper', project_id: 'prj', title: null, status: 'uploaded', sha256: PAPER_SHA,
  page_count: 0, content_available: true, current_extraction_id: null, active_operation_id: null,
  error: null, created_at: 't',
};
const paperAccepted = { paper_id: 'paper', operation_id: 'op', status: 'uploaded' };
const sessionSummary = {
  provider_session_id: 'ps_1', endpoint_alias: 'api.example.com', model_id: 'gpt-x',
  base_url: 'https://api.example.com/v1', secret_configured: true, secret_last4: '••••k3y',
};
const candidates = [
  { id: 'c-real', label: 'Real CPU baseline', recipe_id: 'real_cpu_baseline', recipe_version: '1', support_status: 'supported', execution_support: 'supported', unsupported_reasons: [], parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {}, estimated_runtime: null, source_refs: [], provenance: 'article_extraction' },
  { id: 'c-review', label: 'Needs review', recipe_id: 'other', recipe_version: '2', support_status: 'review_required', execution_support: 'requires_review', unsupported_reasons: ['support_status: review_required'], parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {}, estimated_runtime: null, source_refs: [], provenance: 'article_extraction' },
  { id: 'c-no', label: 'Impossible', recipe_id: 'gpu-only', recipe_version: '9', support_status: 'unsupported', execution_support: 'unsupported', unsupported_reasons: ['support_status: unsupported'], parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {}, estimated_runtime: null, source_refs: [], provenance: 'article_extraction' },
];
const extractionPayload = {
  extraction_id: 'x1', paper_id: 'paper', schema_version: '1.0',
  fields: { title: { value: 'A paper', evidence_status: 'supported', source_refs: [] } },
  methods: [], formulas: [], candidate_experiments: candidates,
  source_refs: [{ paper_id: 'paper', pdf_sha256: PAPER_SHA, page: 1, section: null, table: null, row: null, quote: 'quote', bbox: null }],
  warnings: [], provider: { model_id: 'gpt-x' },
};
const experiment = { experiment_id: 'e', project_id: 'prj', paper_id: 'paper', status: 'draft', head_revision_id: null, active_run_id: null, latest_run_id: null, repairs_used: 0, repairs_limit: 0, created_at: 't' };
const draft = { experiment_id: 'e', base_revision_id: null, policy_hash: 'ph', spec: { schema_version: '1.0', training: { seed: null, batch_size: null, max_steps: null, max_epochs: null, optimizer: null, learning_rate: null, weight_decay: null } }, editable_fields: [], issues: [], comparison_precheck: { status: 'unresolved', reasons: [] } };
const revision = { id: 'v', experiment_id: 'e', revision_no: 1, spec_hash: 's', policy_hash: 'h', spec: draft.spec, validation: { issues: [] }, comparison_precheck: null, budget_estimate: null, confirmation: null, is_current: true, created_at: 't' };
const confirmation = { confirmation_id: 'c', revision_id: 'v', spec_hash: 's', confirmed_by: 'demo-user', confirmed_at: 't' };
const envCheck = {
  environment_check_id: 'env1', project_id: 'prj', experiment_id: 'e', revision_id: 'v',
  decision: 'ready', status: 'ready', checker_version: 'environment-checker.v1',
  requirements: {}, repair_suggestions: [],
  items: [{ key: 'python', status: 'pass', required: false, observed: '3.12.0', required_value: null, suggestion: '' }],
  created_at: 't',
};
const blockedCheck = { ...envCheck, environment_check_id: 'env2', decision: 'blocked', status: 'blocked', items: [{ key: 'cpu', status: 'fail', required: true, observed: 2, required_value: 8, suggestion: '增加可用 CPU 核心数' }] };
const sshVerification = {
  ssh_verification_id: 'ssh1', environment_check_id: 'env2', project_id: 'prj', experiment_id: 'e',
  revision_id: 'v', status: 'awaiting_confirmation',
  target: { host: 'h', port: 22, user: 'u', workdir: '/w', auth_type: 'agent' },
  summary: { connected: true }, created_at: 't', updated_at: 't',
};
const runAccepted = { run_id: 'r', status: 'queued', events_url: '/api/v1/runs/r/events', last_event_seq: 0 };
const run = { run_id: 'r', project_id: 'prj', experiment_id: 'e', revision_id: 'v', status: 'running', last_event_seq: 1, created_at: 't', updated_at: 't', error: null };
const reportStatus = { run_id: 'r', status: null, build_id: null, operation_id: null, report_hash: null, error: null };
const exportAccepted = { export_id: 'ex', operation_id: 'opx', status: 'queued' };

function setup() {
  const calls = [];
  const state = { envDecision: 'ready', failProjectOnce: false, runStatus: 'running', reportStatus: null };
  const api = {
    async createProject(name, key) { calls.push(['POST', '/projects', { name }, key]); if (state.failProjectOnce) { state.failProjectOnce = false; throw Object.assign(new Error('forbidden'), { status: 403 }); } return project; },
    async write(intent, validate) {
      calls.push(['POST', intent.path, JSON.parse(intent.body), intent.key]);
      const body = JSON.parse(intent.body);
      if (intent.path === '/demo/bootstrap') return validate({ project_id: 'fixture-project', paper_id: 'fixture-paper', extraction_id: 'fixture-extraction' });
      if (intent.path === '/projects/prj/experiments') return validate(experiment);
      if (intent.path === '/projects/prj/spec-revisions') return validate(revision);
      if (intent.path === '/spec-revisions/v/confirm') return validate(confirmation);
      if (intent.path === '/projects/prj/runs') return validate(runAccepted);
      if (intent.path === '/runs/r/report-builds') { state.reportStatus = 'pending'; return validate({ build_id: 'b', operation_id: 'ob', run_id: 'r', status: 'pending' }); }
      if (intent.path === '/runs/r/exports') return validate(exportAccepted);
      throw new Error(`unexpected write ${intent.path}`);
    },
    async read(path, validate) {
      calls.push(['GET', path]);
      if (path === '/experiments/e/draft') return validate(draft);
      if (path === '/spec-revisions/v') return validate(revision);
      if (path === '/runs/r') return validate({ ...run, status: state.runStatus });
      if (path === '/runs/r/report-status') return validate({ ...reportStatus, status: state.reportStatus });
      throw new Error(`unexpected read ${path}`);
    },
    async readArray() { throw new Error('unexpected readArray'); },
    async receipt() { throw new Error('unexpected receipt'); },
    async download() { throw new Error('unexpected download'); },
    async runEvents() { return []; },
    async createTextPaper(projectId, input) { calls.push(['POST', `/projects/${projectId}/papers/text`, { text: input.text, titleHint: input.titleHint }, input.key]); return paperAccepted; },
    async uploadPaperPdf(projectId, file, options) { calls.push(['POST', `/projects/${projectId}/papers`, { file: file.name }, options.key]); return paperAccepted; },
    async getPaper(paperId) { calls.push(['GET', `/papers/${paperId}`]); return paperInfo; },
    async createProviderSession(input) { calls.push(['POST', '/provider-sessions', { base_url: input.base_url, model_id: input.model_id, endpoint_alias: input.endpoint_alias }, input.key]); return sessionSummary; },
    async deleteProviderSession(id, key) { calls.push(['DELETE', `/provider-sessions/${id}`, key]); return { provider_session_id: id, secret_configured: false }; },
    async createExtraction(paperId, input) { calls.push(['POST', `/papers/${paperId}/extractions`, { reason: input.reason, provider_session_id: input.provider_session_id }, input.key]); return { paper_id: paperId, operation_id: 'op', status: 'ready', extraction_id: 'x1' }; },
    async getExtraction(paperId) { calls.push(['GET', `/papers/${paperId}/extraction`]); return extractionPayload; },
    async createEnvironmentCheck(projectId, experimentId, input) { calls.push(['POST', `/projects/${projectId}/experiments/${experimentId}/environment-checks`, { revision_id: input.revision_id, requirements: input.requirements }, input.key]); return validateEnv(state.envDecision === 'blocked' ? blockedCheck : envCheck); },
    async createSshVerification(checkId, input) { calls.push(['POST', `/environment-checks/${checkId}/ssh-verifications`, { target: { host: input.host, port: input.port, user: input.user, workdir: input.workdir, auth: input.auth }, user_selected: true }, input.key]); return sshVerification; },
  };
  return { api, calls, state, create: () => new RealFlowController({ api }) };
}

function validateEnv(value) { return value; }

async function reachSpec(c, f) {
  await c.uploadArticle({ consent: true, text: 'A real paper text.' });
  await c.createSession({ baseUrl: 'https://api.example.com/v1', modelId: 'gpt-x', apiKey: 'sk-secret-value', endpointAlias: '' });
  await c.runExtraction();
  c.selectCandidate('c-real');
  await c.confirmSpec(true);
  return c.getSnapshot();
}

// ---------------------------------------------------------------------------
// state: gates, candidate selection, training merge
// ---------------------------------------------------------------------------

test('realflow steps stay gated until each verified milestone exists', () => {
  let s = createRealFlowState();
  assert.equal(canRealViewStep(s, 'input'), true);
  assert.equal(canRealViewStep(s, 'provider'), false);
  assert.equal(canRealViewStep(s, 'extract'), false);
  assert.equal(canRealViewStep(s, 'spec'), false);
  assert.equal(canRealViewStep(s, 'environment'), false);
  assert.equal(canRealViewStep(s, 'execute'), false);
  s = realFlowReducer(s, { type: 'paper', value: paperInfo });
  assert.equal(canRealViewStep(s, 'provider'), true);
  assert.equal(canRealViewStep(s, 'spec'), false);
  s = realFlowReducer(s, { type: 'extraction', value: extractionPayload });
  assert.equal(canRealViewStep(s, 'spec'), true);
  assert.equal(canRealViewStep(s, 'environment'), false);
  s = realFlowReducer(s, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  assert.equal(canRealViewStep(s, 'environment'), true);
  assert.equal(canRealViewStep(s, 'execute'), false, 'execute needs a ready check or ssh verification');
  s = realFlowReducer(s, { type: 'env', value: blockedCheck });
  assert.equal(canRealViewStep(s, 'execute'), false, 'blocked alone does not unlock execution');
  s = realFlowReducer(s, { type: 'ssh', value: sshVerification });
  assert.equal(canRealViewStep(s, 'execute'), true);
});

test('paper from another project never replaces the current one', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'project', projectId: 'prj' });
  s = realFlowReducer(s, { type: 'paper', value: paperInfo });
  const impostor = { ...paperInfo, paper_id: 'other', project_id: 'other-project' };
  assert.equal(realFlowReducer(s, { type: 'paper', value: impostor }), s);
});

test('candidate gating: supported selectable, review_required/unsupported refused', () => {
  const s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  assert.equal(candidateBlockedReason(candidates[0]), null);
  assert.equal(candidateBlockedReason(candidates[1]), 'REVIEW_REQUIRED_CANDIDATE');
  assert.equal(candidateBlockedReason(candidates[2]), 'UNSUPPORTED_CANDIDATE');
  assert.equal(findCandidate(s.extraction, 'nope'), null);
  let picked = realFlowReducer(s, { type: 'select', candidateId: 'c-review' });
  assert.equal(picked.selectedCandidateId, null);
  assert.equal(picked.error, 'REVIEW_REQUIRED_CANDIDATE');
  picked = realFlowReducer(s, { type: 'select', candidateId: 'c-no' });
  assert.equal(picked.selectedCandidateId, null);
  picked = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  assert.equal(picked.selectedCandidateId, 'c-real');
  assert.equal(picked.error, null);
});

test('changing the candidate resets everything downstream', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  s = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  s = realFlowReducer(s, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  s = realFlowReducer(s, { type: 'env', value: envCheck });
  s = realFlowReducer(s, { type: 'run', value: run });
  const next = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  assert.equal(next, s, 're-selecting the same candidate is a no-op that keeps downstream refs');
  const other = { ...extractionPayload, candidate_experiments: [...candidates, { ...candidates[0], id: 'c-real-2' }] };
  let s2 = realFlowReducer(createRealFlowState(), { type: 'extraction', value: other });
  s2 = realFlowReducer(s2, { type: 'select', candidateId: 'c-real' });
  s2 = realFlowReducer(s2, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  s2 = realFlowReducer(s2, { type: 'run', value: run });
  const switched = realFlowReducer(s2, { type: 'select', candidateId: 'c-real-2' });
  assert.equal(switched.selectedCandidateId, 'c-real-2');
  assert.equal(switched.experimentId, null);
  assert.equal(switched.revisionId, null);
  assert.equal(switched.confirmationId, null);
  assert.equal(switched.envCheck, null);
  assert.equal(switched.runId, null);
});

test('a genuinely re-run extraction invalidates the previous experiment chain', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  s = realFlowReducer(s, { type: 'select', candidateId: 'c-real' });
  s = realFlowReducer(s, { type: 'refs', value: { experimentId: 'e', revisionId: 'v', confirmationId: 'c' } });
  s = realFlowReducer(s, { type: 'env', value: envCheck });
  s = realFlowReducer(s, { type: 'run', value: run });
  const restarted = realFlowReducer(s, { type: 'extraction-phase', phase: 'loading' });
  assert.equal(restarted.extraction, null);
  assert.equal(restarted.selectedCandidateId, null);
  assert.equal(restarted.confirmationId, null);
  assert.equal(restarted.envCheck, null);
  assert.equal(restarted.runId, null);
});

test('training form: empty keeps nulls, values merge, invalid input refused, spec not mutated', () => {
  const spec = { training: { seed: null, learning_rate: null } };
  const untouched = applyTrainingToSpec(spec, { seed: '', max_epochs: '', learning_rate: '', batch_size: '' });
  assert.deepEqual(untouched.training, { seed: null, learning_rate: null, max_epochs: null, batch_size: null });
  assert.equal(spec.training.max_epochs, undefined, 'the draft spec object is never mutated');
  const merged = applyTrainingToSpec(spec, { seed: '7', max_epochs: '', learning_rate: '0.15', batch_size: '' });
  assert.deepEqual(merged.training, { seed: 7, learning_rate: 0.15, max_epochs: null, batch_size: null });
  assert.throws(() => parseTrainingForm({ seed: 'abc', max_epochs: '', learning_rate: '', batch_size: '' }), /REALFLOW_TRAINING_INVALID/);
  assert.throws(() => parseTrainingForm({ seed: '', max_epochs: '1.5', learning_rate: '', batch_size: '' }), /REALFLOW_TRAINING_INVALID/, 'integer fields reject fractions');
  assert.throws(() => parseTrainingForm({ seed: '', max_epochs: '', learning_rate: '', batch_size: '-8' }), /REALFLOW_TRAINING_INVALID/);
  assert.deepEqual(parseTrainingForm({ seed: '0', max_epochs: '', learning_rate: '', batch_size: '' }).seed, 0);
});

test('run/report/events reducers stay run-scoped', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'extraction', value: extractionPayload });
  s = realFlowReducer(s, { type: 'run', value: run });
  assert.equal(s.runId, 'r');
  assert.equal(realFlowReducer(s, { type: 'run', value: { ...run, run_id: 'other' } }), s);
  assert.equal(realFlowReducer(s, { type: 'report', value: { run_id: 'other', status: 'ready', build_id: null, operation_id: null, report_hash: null, error: null } }), s);
  const event = (id, seq) => ({ schema_version: '1.0', run_id: id, seq, type: 'run.status', occurred_at: 't', payload: {} });
  s = realFlowReducer(s, { type: 'events', value: [event('r', 1), event('other', 0)] });
  assert.deepEqual(s.events.map(e => e.seq), [1]);
});

// ---------------------------------------------------------------------------
// controller: article input, provider session, extraction
// ---------------------------------------------------------------------------

test('upload requires consent and exactly one source, then shows paper_id and sha256', async () => {
  const f = setup(), c = f.create();
  await assert.rejects(c.uploadArticle({ consent: false, text: 'x' }), /CONSENT/);
  await assert.rejects(c.uploadArticle({ consent: true }), /ONE_SOURCE/);
  await assert.rejects(c.uploadArticle({ consent: true, text: 'x', file: { name: 'a.txt', size: 3, text: async () => 'x' } }), /ONE_SOURCE/);
  await assert.rejects(c.uploadArticle({ consent: true, file: { name: 'a.docx', size: 3 } }), /UNSUPPORTED_FILE_TYPE/);
  await assert.rejects(c.uploadArticle({ consent: true, text: ' '.repeat(10) }), /.*/);
  await c.uploadArticle({ consent: true, text: 'A real paper text.', titleHint: 'hint' });
  const s = c.getSnapshot();
  assert.equal(s.projectId, 'prj');
  assert.equal(s.paper.paper_id, 'paper');
  assert.equal(s.paper.sha256, PAPER_SHA);
  assert.deepEqual(f.calls.map(x => `${x[0]} ${x[1]}`), ['POST /projects', 'POST /projects/prj/papers/text', 'GET /papers/paper']);
  assert.equal(f.calls[1][2].text, 'A real paper text.');
  assert.equal(f.calls[1][2].titleHint, 'hint');
});

test('pdf files go through the multipart endpoint, txt/json through the text endpoint', async () => {
  const f = setup(), c = f.create();
  await c.uploadArticle({ consent: true, file: new File(['%PDF-fake'], 'paper.pdf', { type: 'application/pdf' }) });
  assert.equal(f.calls[1][1], '/projects/prj/papers');
  const f2 = setup(), c2 = f2.create();
  await c2.uploadArticle({ consent: true, file: new File(['hello world'], 'notes.txt', { type: 'text/plain' }) });
  assert.equal(f2.calls[1][1], '/projects/prj/papers/text');
  assert.equal(f2.calls[1][2].text, 'hello world');
});

test('project creation provisions the demo user once on 403, with a fresh key for the retry', async () => {
  const f = setup(), c = f.create();
  f.state.failProjectOnce = true;
  await c.uploadArticle({ consent: true, text: 'text' });
  assert.deepEqual(f.calls.map(x => `${x[0]} ${x[1]}`), ['POST /projects', 'POST /demo/bootstrap', 'POST /projects', 'POST /projects/prj/papers/text', 'GET /papers/paper']);
  assert.notEqual(f.calls[0][3], f.calls[2][3], 'the rejected key must not be reused for the retried body');
  assert.ok(c.getSnapshot().note?.includes('bootstrap'), 'the provisioning side effect is surfaced');
});

test('provider session stores only masked info; api_key never appears in state', async () => {
  const f = setup(), c = f.create();
  await assert.rejects(c.createSession({ baseUrl: '', modelId: 'm', apiKey: 'k' }), /FORM_INCOMPLETE/);
  await c.createSession({ baseUrl: 'https://api.example.com/v1', modelId: 'gpt-x', apiKey: 'sk-live-SUPERSECRET', endpointAlias: '' });
  const s = c.getSnapshot();
  assert.equal(s.provider.providerSessionId, 'ps_1');
  assert.equal(s.provider.secretLast4, '••••k3y');
  assert.equal(JSON.stringify(s).includes('SUPERSECRET'), false, 'no api_key material may live in controller state');
  await c.clearSession();
  assert.equal(c.getSnapshot().provider, null);
  assert.equal(f.calls.find(x => x[0] === 'DELETE')?.[1], '/provider-sessions/ps_1');
});

test('extraction is driven by the provider session and displays returned candidates as-is', async () => {
  const f = setup(), c = f.create();
  await assert.rejects(c.runExtraction(), /PAPER_REQUIRED/);
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  assert.equal(f.calls.find(x => x[1] === '/papers/paper/extractions')[2].provider_session_id, null, 'no session yet');
  await c.createSession({ baseUrl: 'https://api.example.com/v1', modelId: 'gpt-x', apiKey: 'k', endpointAlias: 'alias' });
  await c.runExtraction('my-reason');
  const extractionCalls = f.calls.filter(x => x[1] === '/papers/paper/extractions');
  assert.equal(extractionCalls[1][2].reason, 'my-reason');
  assert.equal(extractionCalls[1][2].provider_session_id, 'ps_1');
  const s = c.getSnapshot();
  assert.equal(s.extractionPhase, 'ready');
  assert.equal(s.extraction.extraction_id, 'x1');
  assert.deepEqual(s.extraction.candidate_experiments.map(x => candidateKey(x)), ['c-real', 'c-review', 'c-no']);
  assert.ok(s.extraction.candidate_experiments.some(x => x.recipe_id === 'real_cpu_baseline'), 'candidates come from the payload, not a hardcoded offline_fixture');
});

// ---------------------------------------------------------------------------
// controller: spec confirmation, environment, ssh, run
// ---------------------------------------------------------------------------

test('confirmSpec refuses missing gates and non-supported candidates, then posts experiment→revision→confirm', async () => {
  const f = setup(), c = f.create();
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  await assert.rejects(c.confirmSpec(true), /CANDIDATE_SELECTION_REQUIRED/);
  c.selectCandidate('c-review');
  assert.equal(c.getSnapshot().error, 'REVIEW_REQUIRED_CANDIDATE', 'selecting a review_required candidate is refused with a reason');
  assert.equal(c.getSnapshot().selectedCandidateId, null);
  await assert.rejects(c.confirmSpec(true), /CANDIDATE_SELECTION_REQUIRED/);
  c.selectCandidate('c-no');
  assert.equal(c.getSnapshot().error, 'UNSUPPORTED_CANDIDATE');
  c.selectCandidate('c-real');
  await assert.rejects(c.confirmSpec(false), /BUDGET/);
  c.setTraining({ seed: '7', learning_rate: '0.15' });
  c.setTraining({ seed: 'abc' });
  await assert.rejects(c.confirmSpec(true), /REALFLOW_TRAINING_INVALID/);
  assert.equal(f.calls.filter(x => x[0] === 'POST' && x[1].endsWith('/experiments')).length, 0, 'invalid numbers must fail before any write');
  c.setTraining({ seed: '7' });
  await c.confirmSpec(true);
  const posts = f.calls.filter(x => x[0] === 'POST').map(x => x[1]);
  assert.deepEqual(posts, ['/projects', '/projects/prj/papers/text', '/papers/paper/extractions', '/projects/prj/experiments', '/projects/prj/spec-revisions', '/spec-revisions/v/confirm']);
  const revisionBody = f.calls.find(x => x[1] === '/projects/prj/spec-revisions')[2];
  assert.equal(revisionBody.experiment_id, 'e');
  assert.equal(revisionBody.base_revision_id, null);
  assert.equal(revisionBody.spec.training.seed, 7, 'only filled training fields are overridden');
  assert.equal(revisionBody.spec.training.learning_rate, 0.15, 'the form keeps earlier filled fields');
  assert.equal(revisionBody.spec.training.batch_size, null);
  const experimentBody = f.calls.find(x => x[1] === '/projects/prj/experiments')[2];
  assert.deepEqual({ ...experimentBody }, { paper_id: 'paper', extraction_id: 'x1', candidate_experiment_id: 'c-real', recipe_id: 'real_cpu_baseline', recipe_version: '1' });
  const s = c.getSnapshot();
  assert.equal(s.experimentId, 'e');
  assert.equal(s.revisionId, 'v');
  assert.equal(s.confirmationId, 'c');
  assert.equal(s.step, 'environment');
});

test('environment check stores the decision; ssh verification only from a blocked check', async () => {
  const f = setup(), c = f.create();
  await reachSpec(c, f);
  f.state.envDecision = 'blocked';
  await assert.rejects(c.startRun('local-cpu'), /ENVIRONMENT_BLOCKED/);
  await c.checkEnvironment();
  const checkCall = f.calls.find(x => x[1] === '/projects/prj/experiments/e/environment-checks');
  assert.deepEqual({ ...checkCall[2] }, { revision_id: 'v', requirements: {} });
  assert.equal(c.getSnapshot().envCheck.decision, 'blocked');
  await assert.rejects(c.verifySsh({ host: 'h', port: '70000', user: 'u', workdir: '/w', authType: 'agent', agentId: 'a' }), /SSH_FORM_INVALID/);
  await assert.rejects(c.verifySsh({ host: 'h', port: '22', user: 'u', workdir: '/w', authType: 'key_reference', keyReference: '' }), /SSH_FORM_INVALID/);
  await c.verifySsh({ host: 'h', port: '', user: 'u', workdir: '/w', authType: 'agent', agentId: 'agent-1' });
  const sshCall = f.calls.find(x => x[1] === '/environment-checks/env2/ssh-verifications');
  assert.deepEqual(sshCall[2], { target: { host: 'h', port: 22, user: 'u', workdir: '/w', auth: { type: 'agent', agent_id: 'agent-1' } }, user_selected: true });
  assert.equal(c.getSnapshot().ssh.status, 'awaiting_confirmation');
});

test('ssh verification is refused when the check is not blocked', async () => {
  const f = setup(), c = f.create();
  await reachSpec(c, f);
  await c.checkEnvironment();
  assert.equal(c.getSnapshot().envCheck.decision, 'ready');
  await assert.rejects(c.verifySsh({ host: 'h', port: '22', user: 'u', workdir: '/w', authType: 'agent', agentId: 'a' }), /SSH_NOT_AVAILABLE/);
});

test('local run requires a ready check; ssh run requires a verification and binds it explicitly', async () => {
  const f = setup(), c = f.create();
  await reachSpec(c, f);
  await assert.rejects(c.startRun('ssh'), /SSH_VERIFICATION_REQUIRED/);
  await c.checkEnvironment();
  await c.startRun('local-cpu');
  const runCall = f.calls.find(x => x[1] === '/projects/prj/runs');
  assert.deepEqual(runCall[2], { spec_revision_id: 'v', confirmation_id: 'c' });
  assert.equal(c.getSnapshot().runId, 'r');

  const f2 = setup(), c2 = f2.create();
  f2.state.envDecision = 'blocked';
  await reachSpec(c2, f2);
  await c2.checkEnvironment();
  await assert.rejects(c2.startRun('local-cpu'), /ENVIRONMENT_BLOCKED/);
  await c2.verifySsh({ host: 'h', port: '22', user: 'u', workdir: '/w', authType: 'agent', agentId: 'a' });
  await c2.startRun('ssh');
  const sshRun = f2.calls.filter(x => x[1] === '/projects/prj/runs').at(-1);
  assert.equal(sshRun[2].mode, 'ssh');
  assert.deepEqual(sshRun[2].execution, { kind: 'ssh', ssh_verification_id: 'ssh1', confirmed: true });
});

test('report build waits for a completed run; exports wait for a ready report', async () => {
  const f = setup(), c = f.create();
  await reachSpec(c, f);
  await c.checkEnvironment();
  await c.startRun('local-cpu');
  await assert.rejects(c.buildReport(), /COMPLETED_RUN_REQUIRED/);
  await assert.rejects(c.createExports(), /REPORT_NOT_READY/);
  f.state.runStatus = 'completed';
  await c.pollRun();
  await c.buildReport();
  assert.equal(c.getSnapshot().report.build_id, 'b');
  const builds = () => f.calls.filter(x => x[0] === 'POST' && x[1] === '/runs/r/report-builds').length;
  const before = builds();
  await c.buildReport(); // report already pending: no duplicate build request
  assert.equal(builds(), before);
  await assert.rejects(c.createExports(), /REPORT_NOT_READY/, 'a pending report does not unlock exports');
});

test('unknown write outcome keeps the exact key and body for an explicit replay', async () => {
  const f = setup(), c = f.create();
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  c.selectCandidate('c-real');
  const realWrite = f.api.write;
  let attempted;
  f.api.write = async (intent, validate) => {
    if (intent.path === '/projects/prj/experiments') {
      attempted = intent;
      f.calls.push(['POST', intent.path, JSON.parse(intent.body), intent.key]);
      throw Object.assign(new Error('network'), { unknown: true });
    }
    return realWrite(intent, validate);
  };
  await assert.rejects(c.confirmSpec(true));
  assert.equal(c.getSnapshot().error, 'WRITE_UNCONFIRMED');
  f.api.write = realWrite;
  await c.retryWrite();
  const posts = f.calls.filter(x => x[0] === 'POST' && x[1] === '/projects/prj/experiments');
  assert.equal(posts.length, 2);
  assert.equal(posts[1][3], attempted.key, 'the replay reuses the same Idempotency-Key');
  assert.deepEqual(posts[1][2], JSON.parse(attempted.body));
  const s = c.getSnapshot();
  assert.equal(s.confirmationId, 'c');
  assert.equal(c.hasRetry(), false);
  await assert.rejects(c.retryWrite(), /NO_UNCONFIRMED_WRITE/);
});

test('new commands are blocked while an unknown write is pending', async () => {
  const f = setup(), c = f.create();
  const realWrite = f.api.write;
  f.api.write = async (intent, validate) => {
    if (intent.path === '/projects/prj/experiments') throw Object.assign(new Error('network'), { unknown: true });
    return realWrite(intent, validate);
  };
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  c.selectCandidate('c-real');
  await assert.rejects(c.confirmSpec(true));
  await assert.rejects(c.uploadArticle({ consent: true, text: 'more' }), /WRITE_UNCONFIRMED_RECHECK_STATE/);
  await assert.rejects(c.runExtraction(), /WRITE_UNCONFIRMED_RECHECK_STATE/);
  f.api.write = realWrite;
  await c.retryWrite();
  assert.equal(c.getSnapshot().confirmationId, 'c');
  await c.checkEnvironment(); // unblocked after the explicit replay
  assert.equal(c.getSnapshot().envCheck.decision, 'ready');
});

test('rejected writes clear the retry slot and surface the server error code', async () => {
  const f = setup(), c = f.create();
  const realWrite = f.api.write;
  f.api.write = async (intent, validate) => {
    if (intent.path === '/projects/prj/experiments') {
      throw Object.assign(new Error('conflict'), { status: 409, code: 'CANDIDATE_INVALID', message: 'candidate does not match persisted extraction' });
    }
    return realWrite(intent, validate);
  };
  await c.uploadArticle({ consent: true, text: 'text' });
  await c.runExtraction();
  c.selectCandidate('c-real');
  await assert.rejects(c.confirmSpec(true));
  assert.equal(c.getSnapshot().error, 'CANDIDATE_INVALID: candidate does not match persisted extraction');
  assert.equal(c.hasRetry(), false);
});

test('the panel and controller never touch localStorage for the real flow', async () => {
  const panel = await readFile(new URL('../src/features/guided/RealFlowPanel.tsx', import.meta.url), 'utf8');
  const controllerSource = await readFile(new URL('../src/features/guided/realflow.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(panel, /localStorage\./, 'the api_key must only live in React state');
  assert.doesNotMatch(controllerSource, /localStorage\./, 'the real flow controller has no persistence layer');
  assert.match(panel, /type="password"/, 'the api_key input is masked');
});

test('the product surface is the real flow only: no offline teaching entry', async () => {
  const page = await readFile(new URL('../src/features/guided/GuidedPage.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(page, /离线教学/, 'the offline teaching entry is removed from the product surface');
  assert.doesNotMatch(page, /startExample|GuidedController/, 'no offline demo bootstrap wiring remains on the page');
  assert.doesNotMatch(page, /guided-mode-tabs|mode === 'example'/, 'the mode switch is gone');
  assert.match(page, /<RealFlowPanel api=\{api\} \/>/, 'the page renders the real flow panel directly');
});
