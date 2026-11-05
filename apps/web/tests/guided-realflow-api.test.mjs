import test from 'node:test';
import assert from 'node:assert/strict';
import { createGuidedApi, GuidedApiError, validateRun } from '../src/api-client/guided.ts';

// Api-client tests for the real article flow endpoints, against a mock fetch.
// Focus: Idempotency-Key + X-Actor-ID headers, exact wire bodies (nothing but
// allow-listed fields may ride along), envelope error mapping, and the
// extraction readback's pending/ready distinction.

const SHA = 'c'.repeat(64);

function envelope(data, meta = { request_id: 'req-ok' }, status = 200) {
  return new Response(JSON.stringify({ data, meta, ...{} }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-ok' },
  });
}

function errorEnvelope(status, code, message = 'failed') {
  return new Response(JSON.stringify({ error: { code, message, retryable: status >= 500, details: {} }, meta: { request_id: 'req-error' } }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-error' },
  });
}

function capture() {
  const calls = [];
  const transport = { calls, respond: null, fetcher: async (path, init = {}) => {
    calls.push({ path, init, headers: new Headers(init.headers) });
    return transport.respond ? transport.respond(path, init, calls.length) : envelope({ ok: true });
  } };
  return transport;
}

test('provider session create posts the exact fields with actor and idempotency headers', async () => {
  const t = capture();
  t.respond = async () => envelope({
    provider_session_id: 'ps_1', endpoint_alias: 'alias', model_id: 'm',
    base_url: 'https://api.example.com/v1', secret_configured: true, secret_last4: '••••abcd',
  }, { request_id: 'r1' }, 201);
  const api = createGuidedApi('demo-user', t.fetcher);
  const summary = await api.createProviderSession({ base_url: 'https://api.example.com/v1', model_id: 'm', api_key: 'sk-x', endpoint_alias: 'alias', key: 'stable-key' });
  assert.equal(summary.provider_session_id, 'ps_1');
  assert.equal(summary.secret_last4, '••••abcd');
  assert.equal(t.calls.length, 1);
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/provider-sessions');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
  assert.equal(call.headers.get('Idempotency-Key'), 'stable-key');
  assert.equal(call.headers.get('Content-Type'), 'application/json');
  const body = JSON.parse(call.init.body);
  assert.deepEqual(Object.keys(body).sort(), ['api_key', 'base_url', 'endpoint_alias', 'model_id', 'revision']);
  assert.equal(body.api_key, 'sk-x');
});

test('provider session opts into loopback http only for local model servers', async () => {
  const t = capture();
  t.respond = async () => envelope({
    provider_session_id: 'ps_2', endpoint_alias: 'local', model_id: 'm',
    base_url: 'http://127.0.0.1:3425/v1', secret_configured: true, secret_last4: '••••abcd',
  }, { request_id: 'r2' }, 201);
  const api = createGuidedApi('demo-user', t.fetcher);
  for (const base_url of ['http://127.0.0.1:3425/v1', 'http://localhost:8000/v1']) {
    await api.createProviderSession({ base_url, model_id: 'm', api_key: 'sk-x', endpoint_alias: 'local' });
  }
  for (const base_url of ['http://example.com/v1', 'https://127.0.0.1/v1', 'http://127.example.com/v1']) {
    await api.createProviderSession({ base_url, model_id: 'm', api_key: 'sk-x', endpoint_alias: 'remote' });
  }
  const flags = t.calls.map(call => JSON.parse(call.init.body).allow_local_http);
  assert.deepEqual(flags, [true, true, undefined, undefined, undefined]);
});

test('provider session delete uses DELETE with an idempotency key', async () => {
  const t = capture();
  t.respond = async () => envelope({ provider_session_id: 'ps_1', secret_configured: false });
  const api = createGuidedApi('demo-user', t.fetcher);
  const out = await api.deleteProviderSession('ps_1', 'clear-key');
  assert.deepEqual(out, { provider_session_id: 'ps_1', secret_configured: false });
  assert.equal(t.calls[0].path, '/api/v1/provider-sessions/ps_1');
  assert.equal(t.calls[0].init.method, 'DELETE');
  assert.equal(t.calls[0].headers.get('Idempotency-Key'), 'clear-key');
  assert.equal(t.calls[0].headers.get('X-Actor-ID'), 'demo-user');
});

test('provider endpoint rejections map to the envelope error code', async () => {
  const t = capture();
  t.respond = async () => errorEnvelope(422, 'PROVIDER_ENDPOINT_REJECTED', 'provider endpoint was rejected');
  const api = createGuidedApi('demo-user', t.fetcher);
  await assert.rejects(api.createProviderSession({ base_url: 'http://127.0.0.1:9/v1', model_id: 'm', api_key: 'k', endpoint_alias: 'a' }), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.status, 422);
    assert.equal(error.code, 'PROVIDER_ENDPOINT_REJECTED');
    assert.equal(error.unknown, false, 'an explicit 4xx is a proven rejection, not an unknown outcome');
    return true;
  });
});

test('text paper upload carries consent and only allow-listed fields', async () => {
  const t = capture();
  t.respond = async () => envelope({ paper_id: 'paper', operation_id: 'op', status: 'uploaded' }, { request_id: 'r2' }, 202);
  const api = createGuidedApi('demo-user', t.fetcher);
  const accepted = await api.createTextPaper('prj 1', { text: 'hello', titleHint: 'T', key: 'k1' });
  assert.equal(accepted.paper_id, 'paper');
  assert.equal(t.calls[0].path, '/api/v1/projects/prj%201/papers/text');
  const body = JSON.parse(t.calls[0].init.body);
  assert.deepEqual(Object.keys(body).sort(), ['source_processing_consent', 'text', 'title_hint']);
  assert.equal(body.source_processing_consent, true);
  assert.equal(t.calls[0].headers.get('Idempotency-Key'), 'k1');
});

test('pdf upload posts multipart form data without a preset content-type', async () => {
  const t = capture();
  t.respond = async () => envelope({ paper_id: 'paper', operation_id: 'op', status: 'uploaded' }, { request_id: 'r3' }, 202);
  const api = createGuidedApi('demo-user', t.fetcher);
  const file = new File(['%PDF-1.4 fake'], 'paper.pdf', { type: 'application/pdf' });
  await api.uploadPaperPdf('prj', file, { key: 'k2' });
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/projects/prj/papers');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.headers.get('Idempotency-Key'), 'k2');
  assert.equal(call.headers.get('Content-Type'), null, 'the multipart boundary must come from the transport');
  assert.ok(call.init.body instanceof FormData);
  assert.equal(call.init.body.get('source_processing_consent'), 'true');
  assert.equal(call.init.body.get('file') instanceof File, true);
});

test('extraction create body only ever contains reason and provider_session_id', async () => {
  const t = capture();
  t.respond = async () => envelope({ paper_id: 'paper', operation_id: 'op', status: 'ready', extraction_id: 'x1' }, { request_id: 'r4' }, 202);
  const api = createGuidedApi('demo-user', t.fetcher);
  const out = await api.createExtraction('paper', { reason: 'review', provider_session_id: 'ps_1', key: 'k3' });
  assert.equal(out.extraction_id, 'x1');
  const body = JSON.parse(t.calls[0].init.body);
  assert.deepEqual(Object.keys(body), ['reason', 'provider_session_id']);
  await api.createExtraction('paper', { reason: 'review', provider_session_id: null });
  assert.deepEqual(Object.keys(JSON.parse(t.calls[1].init.body)), ['reason'], 'a null session is omitted, not sent as null');
});

test('extraction readback distinguishes pending 202 from a ready payload and maps candidates', async () => {
  const t = capture();
  const payload = {
    extraction_id: 'x1', paper_id: 'paper', schema_version: '1.0',
    fields: { task: { value: 'train', evidence_status: 'supported', source_refs: [] } },
    methods: [], formulas: [],
    candidate_experiments: [
      { id: 'c1', label: 'Real', recipe_id: 'real_cpu_baseline', recipe_version: '1', support_status: 'supported', execution_support: 'supported', unsupported_reasons: [], parameters: { seed: 7 }, required_dependencies: [], required_datasets: [], resource_requirements: {}, estimated_runtime: null, source_refs: [], provenance: 'article_extraction' },
      { id: 'c2', label: 'Review', recipe_id: 'x', recipe_version: '1', support_status: 'review_required', execution_support: 'requires_review', unsupported_reasons: ['support_status: review_required'], parameters: {}, required_dependencies: [], required_datasets: [], resource_requirements: {}, estimated_runtime: null, source_refs: [], provenance: null },
    ],
    source_refs: [{ paper_id: 'paper', pdf_sha256: SHA, page: 2, section: '3', table: null, row: null, quote: 'q', bbox: null }],
    warnings: ['w1'], provider: { model_id: 'm' },
  };
  t.respond = async (path, init, count) => (count === 1
    ? envelope({ paper_id: 'paper', operation_id: null, status: 'uploaded' }, { request_id: 'p' }, 202)
    : envelope(payload));
  const api = createGuidedApi('demo-user', t.fetcher);
  assert.equal(await api.getExtraction('paper'), null, 'an uploaded paper without extraction is not rendered as ready');
  const extraction = await api.getExtraction('paper');
  assert.equal(extraction.extraction_id, 'x1');
  assert.equal(extraction.fields.task.evidence_status, 'supported');
  assert.deepEqual(extraction.candidate_experiments.map(c => c.execution_support), ['supported', 'requires_review']);
  assert.deepEqual(extraction.candidate_experiments[0].parameters, { seed: 7 });
  assert.equal(extraction.source_refs[0].page, 2);
  assert.deepEqual(extraction.warnings, ['w1']);
});

test('environment check create validates the response belongs to the requested resources', async () => {
  const t = capture();
  const check = (experimentId, decision) => ({
    environment_check_id: 'env1', project_id: 'prj', experiment_id: experimentId, revision_id: 'v',
    decision, status: decision, checker_version: 'environment-checker.v1', requirements: {},
    items: [{ key: 'python', status: 'pass', required: false, observed: '3.12', required_value: null, suggestion: '' }],
    repair_suggestions: [], created_at: 't',
  });
  t.respond = async () => envelope(check('other', 'ready'), { request_id: 'r5' }, 201);
  const api = createGuidedApi('demo-user', t.fetcher);
  await assert.rejects(api.createEnvironmentCheck('prj', 'e', { revision_id: 'v', requirements: {}, key: 'k' }), /RESOURCE_MISMATCH/);
  t.respond = async () => envelope(check('e', 'blocked'), { request_id: 'r6' }, 201);
  const result = await api.createEnvironmentCheck('prj', 'e', { revision_id: 'v', requirements: {}, key: 'k' });
  assert.equal(result.decision, 'blocked');
  assert.deepEqual(result.items[0], { key: 'python', status: 'pass', required: false, observed: '3.12', required_value: null, suggestion: '' });
  const body = JSON.parse(t.calls[1].init.body);
  assert.deepEqual(body, { revision_id: 'v', requirements: {} });
});

test('ssh verification posts a structured target with user_selected true', async () => {
  const t = capture();
  t.respond = async () => envelope({
    ssh_verification_id: 'ssh1', environment_check_id: 'env1', project_id: 'prj', experiment_id: 'e',
    revision_id: 'v', status: 'awaiting_confirmation',
    target: { host: 'h', port: 22, user: 'u', workdir: '/w', auth_type: 'agent' },
    summary: { connected: true }, created_at: 't', updated_at: 't',
  }, { request_id: 'r7' }, 201);
  const api = createGuidedApi('demo-user', t.fetcher);
  const verification = await api.createSshVerification('env1', { host: 'h', port: 22, user: 'u', workdir: '/w', auth: { type: 'agent', agent_id: 'a' }, key: 'k' });
  assert.equal(verification.status, 'awaiting_confirmation');
  assert.equal(t.calls[0].path, '/api/v1/environment-checks/env1/ssh-verifications');
  const body = JSON.parse(t.calls[0].init.body);
  assert.deepEqual(body, {
    target: { host: 'h', port: 22, user: 'u', workdir: '/w', auth: { type: 'agent', agent_id: 'a' } },
    user_selected: true,
  });
  assert.equal(t.calls[0].headers.get('Idempotency-Key'), 'k');
});

test('paper info read maps the wire payload and rejects mismatched shapes', async () => {
  const t = capture();
  t.respond = async () => envelope({
    paper_id: 'paper', project_id: 'prj', title: null, status: 'uploaded', sha256: SHA,
    page_count: 3, content_available: true, current_extraction_id: null, active_operation_id: null,
    processing_consent: { accepted_at: 't', policy_version: '1' }, error: null, created_at: 't',
  });
  const api = createGuidedApi('demo-user', t.fetcher);
  const paper = await api.getPaper('paper');
  assert.equal(paper.sha256, SHA);
  assert.equal(paper.page_count, 3);
  t.respond = async () => envelope({ paper_id: 'paper' });
  await assert.rejects(api.getPaper('paper'), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.code, 'INVALID_DTO');
    return true;
  });
});

test('an ssh run readback keeps the honest created status instead of rejecting it', () => {
  const created = validateRun({
    run_id: 'r', project_id: 'prj', experiment_id: 'e', revision_id: 'v',
    status: 'created', last_event_seq: 0, created_at: 't', updated_at: 't', error: null,
  });
  assert.equal(created.status, 'created');
  assert.throws(() => validateRun({
    run_id: 'r', project_id: 'prj', experiment_id: 'e', revision_id: 'v',
    status: 'executing', last_event_seq: 0, created_at: 't', updated_at: 't', error: null,
  }), /unsupported/);
});
