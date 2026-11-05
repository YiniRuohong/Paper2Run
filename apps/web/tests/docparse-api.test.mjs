import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api-client/client.ts';
import { createPapersApi, validateDocParseStatus, validatePaperText } from '../src/api-client/papers.ts';

// Api-client tests for the MinerU PDF parsing endpoints (docparse), against a
// mock fetch. Focus: exact paths/methods, Idempotency-Key only where required,
// X-Actor-ID on every call, and validators that refuse unknown enum values or
// wrong types.

function envelope(data, status = 200) {
  return new Response(JSON.stringify({ data, meta: { request_id: 'req-ok' } }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-ok' },
  });
}

function errorEnvelope(status, code, message = 'failed') {
  return new Response(JSON.stringify({ error: { code, message, retryable: false, details: {} }, meta: { request_id: 'req-error' } }), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function capture(respond) {
  const calls = [];
  const fetcher = async (path, init = {}) => {
    calls.push({ path, init, headers: new Headers(init.headers) });
    return respond(path, init, calls.length);
  };
  return { calls, fetcher };
}

const statusDto = (over = {}) => ({
  mode: 'offline',
  offline: { state: 'installing', step: 'packages', progress: 0.5, tier: null, version: null, bytes_on_disk: 1024, error: null },
  platform: 'darwin-arm64',
  ...over,
});
const parsingText = {
  status: 'parsing', backend: null, page_count: 30, pages_parsed: 0,
  chunks: [{ status: 'done' }, { status: 'pending' }], warnings: [], error_code: null, preview: null,
};
const readyText = {
  status: 'ready', backend: 'offline_mineru', page_count: 30, pages_parsed: 30,
  chunks: [{ first_page: 1, last_page: 20, status: 'done' }, { first_page: 21, last_page: 30, status: 'missing' }],
  warnings: ['pages 21-30 missing'], error_code: null, preview: '<!-- p2r:pages 1-20 -->\n# Title',
};

test('getDocParseStatus is a plain GET with the actor header and no idempotency key', async () => {
  const t = capture(async () => envelope(statusDto()));
  const api = createPapersApi('demo-user', t.fetcher);
  const value = await api.getDocParseStatus();
  assert.deepEqual(value, statusDto());
  assert.equal(t.calls.length, 1);
  assert.equal(t.calls[0].path, '/api/v1/docparse/status');
  assert.equal(t.calls[0].init.method, 'GET');
  assert.equal(t.calls[0].headers.get('X-Actor-ID'), 'demo-user');
  assert.equal(t.calls[0].headers.get('Idempotency-Key'), null);
});

test('setDocParseMode PUTs exactly {mode} without an idempotency key', async () => {
  const t = capture(async () => envelope(statusDto({ mode: 'online' })));
  const api = createPapersApi('demo-user', t.fetcher);
  const value = await api.setDocParseMode('online');
  assert.equal(value.mode, 'online');
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/docparse/mode');
  assert.equal(call.init.method, 'PUT');
  assert.equal(call.headers.get('Content-Type'), 'application/json');
  assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
  assert.equal(call.headers.get('Idempotency-Key'), null);
  assert.deepEqual(JSON.parse(call.init.body), { mode: 'online' });
});

test('setDocParseMode refuses a mode outside online/offline before any request', async () => {
  const t = capture(async () => envelope(statusDto()));
  const api = createPapersApi('demo-user', t.fetcher);
  await assert.rejects(api.setDocParseMode('auto'), (error) => error instanceof ApiError);
  assert.equal(t.calls.length, 0);
});

test('offline install/cancel/uninstall hit their routes without idempotency keys or bodies', async () => {
  const t = capture(async (path) => envelope(statusDto({ offline: { ...statusDto().offline, state: path.endsWith('/install') ? 'installing' : 'not_installed' } }), path.endsWith('/install') ? 202 : 200));
  const api = createPapersApi('demo-user', t.fetcher);
  assert.equal((await api.startOfflineInstall()).offline.state, 'installing');
  assert.equal((await api.cancelOfflineInstall()).offline.state, 'not_installed');
  assert.equal((await api.uninstallOffline()).offline.state, 'not_installed');
  assert.deepEqual(t.calls.map(c => `${c.init.method} ${c.path}`), [
    'POST /api/v1/docparse/offline/install',
    'POST /api/v1/docparse/offline/cancel',
    'DELETE /api/v1/docparse/offline',
  ]);
  for (const call of t.calls) {
    assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
    assert.equal(call.headers.get('Idempotency-Key'), null);
    assert.equal(call.init.body, undefined);
  }
});

test('offline install on an unsupported platform surfaces DOCPARSE_OFFLINE_UNSUPPORTED as a proven rejection', async () => {
  const t = capture(async () => errorEnvelope(409, 'DOCPARSE_OFFLINE_UNSUPPORTED', 'offline parsing is not supported on this platform'));
  const api = createPapersApi('demo-user', t.fetcher);
  await assert.rejects(api.startOfflineInstall(), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, 'DOCPARSE_OFFLINE_UNSUPPORTED');
    assert.equal(error.unknown, false);
    return true;
  });
});

test('parsePaper posts an empty JSON body with the caller key', async () => {
  const t = capture(async () => envelope({ paper_id: 'paper_1', job_id: 'job_1', status: 'parsing' }, 202));
  const api = createPapersApi('demo-user', t.fetcher);
  const accepted = await api.parsePaper('paper_1', 'parse-key');
  assert.deepEqual(accepted, { paper_id: 'paper_1', job_id: 'job_1', status: 'parsing' });
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/papers/paper_1/parse');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.headers.get('Idempotency-Key'), 'parse-key');
  assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
  assert.equal(call.headers.get('Content-Type'), 'application/json');
  assert.deepEqual(JSON.parse(call.init.body), {});
});

test('parsePaper refuses an empty key without sending anything', async () => {
  const t = capture(async () => envelope({ paper_id: 'paper_1', job_id: 'job_1', status: 'parsing' }, 202));
  const api = createPapersApi('demo-user', t.fetcher);
  await assert.rejects(api.parsePaper('paper_1', ''), (error) => error instanceof ApiError);
  assert.equal(t.calls.length, 0);
});

test('parsePaper maps DOCPARSE_MODE_UNSET and rejects an accept for another paper', async () => {
  const unset = capture(async () => errorEnvelope(409, 'DOCPARSE_MODE_UNSET', 'choose online or offline parsing first'));
  await assert.rejects(createPapersApi('demo-user', unset.fetcher).parsePaper('paper_1', 'k'), (error) => {
    assert.equal(error.code, 'DOCPARSE_MODE_UNSET');
    assert.equal(error.unknown, false);
    return true;
  });
  const other = capture(async () => envelope({ paper_id: 'someone-else', job_id: 'job_1', status: 'parsing' }, 202));
  await assert.rejects(createPapersApi('demo-user', other.fetcher).parsePaper('paper_1', 'k'), (error) => {
    assert.ok(error instanceof ApiError, 'a 202 for another paper must not be treated as success');
    return true;
  });
});

test('getPaperText reads the paper text route and validates parsing and ready shapes', async () => {
  const t = capture(async (path) => envelope(path.includes('p1') ? parsingText : readyText));
  const api = createPapersApi('demo-user', t.fetcher);
  assert.deepEqual(await api.getPaperText('p1'), parsingText);
  assert.deepEqual(await api.getPaperText('p2'), readyText);
  assert.deepEqual(t.calls.map(c => `${c.init.method} ${c.path}`), ['GET /api/v1/papers/p1/text', 'GET /api/v1/papers/p2/text']);
  assert.equal(t.calls[0].headers.get('Idempotency-Key'), null);
  assert.equal(t.calls[0].headers.get('X-Actor-ID'), 'demo-user');
});

test('uploadPaperPdf posts multipart with consent, title hint and an idempotency key', async () => {
  const t = capture(async () => envelope({ paper_id: 'p1', operation_id: null, status: 'uploaded' }, 202));
  const api = createPapersApi('demo-user', t.fetcher);
  const file = new File(['%PDF-1.4'], 'paper.pdf', { type: 'application/pdf' });
  const accepted = await api.uploadPaperPdf('proj', file, { titleHint: 'My Paper', key: 'upload-key' });
  assert.equal(accepted.paper_id, 'p1');
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/projects/proj/papers');
  assert.equal(call.init.method, 'POST');
  assert.equal(call.headers.get('Idempotency-Key'), 'upload-key');
  assert.equal(call.headers.get('Content-Type'), null, 'multipart boundary is set by the runtime, not the client');
  const form = call.init.body;
  assert.equal(form.get('title_hint'), 'My Paper');
  assert.equal(form.get('source_processing_consent'), 'true');
  assert.equal(form.get('file').name, 'paper.pdf');
});

test('createTextPaper posts JSON with consent and a generated key when none is given', async () => {
  const t = capture(async () => envelope({ paper_id: 'p2', operation_id: null, status: 'uploaded' }, 202));
  const api = createPapersApi('demo-user', t.fetcher);
  await api.createTextPaper('proj', { text: '# Title\nbody', titleHint: 'T' });
  const call = t.calls[0];
  assert.equal(call.path, '/api/v1/projects/proj/papers/text');
  assert.ok(call.headers.get('Idempotency-Key'), 'a key is generated per call');
  assert.deepEqual(JSON.parse(call.init.body), { text: '# Title\nbody', title_hint: 'T', source_processing_consent: true });
});

test('validatePaperText refuses unknown status/backend and wrong types', () => {
  assert.deepEqual(validatePaperText(readyText), readyText);
  assert.equal(validatePaperText({ ...readyText, preview: '' }).preview, '', 'an empty preview is still a string');
  assert.equal(validatePaperText({ ...parsingText, status: 'failed', error_code: 'DOCPARSE_OFFLINE_NOT_READY' }).error_code, 'DOCPARSE_OFFLINE_NOT_READY');
  const bad = [
    { ...readyText, status: 'done' },
    { ...readyText, status: undefined },
    { ...readyText, backend: 'cloud' },
    { ...readyText, page_count: '30' },
    { ...readyText, pages_parsed: -1 },
    { ...readyText, chunks: {} },
    { ...readyText, chunks: [{ first_page: 1 }] },
    { ...readyText, chunks: [{ status: 'done', first_page: 'x' }] },
    { ...readyText, warnings: [1] },
    { ...readyText, error_code: 3 },
    { ...readyText, preview: 7 },
    null,
    [],
  ];
  for (const value of bad) assert.throws(() => validatePaperText(value), ApiError, JSON.stringify(value));
});

test('validateDocParseStatus refuses unknown mode/offline.state and wrong types', () => {
  assert.deepEqual(validateDocParseStatus(statusDto({ mode: null })), statusDto({ mode: null }));
  const offline = statusDto().offline;
  assert.equal(validateDocParseStatus(statusDto({ offline: { ...offline, version: '' } })).offline.version, '', 'empty installer text is still a string');
  const bad = [
    statusDto({ mode: 'auto' }),
    statusDto({ mode: undefined }),
    statusDto({ offline: { ...offline, state: 'downloading' } }),
    statusDto({ offline: { ...offline, progress: '0.5' } }),
    statusDto({ offline: { ...offline, progress: 2 } }),
    statusDto({ offline: { ...offline, bytes_on_disk: 1.5 } }),
    statusDto({ offline: { ...offline, step: 4 } }),
    statusDto({ offline: { ...offline, error: {} } }),
    statusDto({ offline: null }),
    statusDto({ platform: 3 }),
  ];
  for (const value of bad) assert.throws(() => validateDocParseStatus(value), ApiError, JSON.stringify(value));
});
