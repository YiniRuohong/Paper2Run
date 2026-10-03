import test from 'node:test';
import assert from 'node:assert/strict';
import {
  GUIDED_PATHS,
  GuidedApiError,
  createGuidedApi,
  parseRunEvents,
  validateExport,
  validateReceipt,
  validateTextChunk,
} from '../src/api-client/guided.ts';

const HASH = 'a'.repeat(64);

function envelope(data, meta = { request_id: 'req-ok' }, extra = {}, status = 200) {
  return new Response(JSON.stringify({ data, meta, ...extra }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-ok' },
  });
}

function errorEnvelope(status, code, message = 'failed', meta = { request_id: 'req-error' }) {
  return new Response(JSON.stringify({
    error: { code, message, retryable: status >= 500, details: { field_errors: [] } },
    meta,
  }), {
    status,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-error' },
  });
}

function intent(key = 'write-key') {
  return { key, path: '/runs/run%20one/report-builds', body: '{"retry_of":null}', state: 'prepared' };
}

function capture() {
  const calls = [];
  const transport = {
    calls,
    respond: null,
    fetcher: async (path, init = {}) => {
      calls.push({ path, init, headers: new Headers(init.headers) });
      return transport.respond
        ? transport.respond(path, init, calls.length)
        : envelope({ ok: true });
    },
  };
  return transport;
}

test('writes reuse the exact key and body, attach actor, and support 202/409 outcomes', async () => {
  const transport = capture();
  transport.respond = async () => envelope(
    { run_id: 'run-1', status: 'queued', events_url: '/api/v1/runs/run-1/events', last_event_seq: 0 },
    { request_id: 'req-run' },
  );
  const api = createGuidedApi('demo-user', transport.fetcher);
  const first = await api.write(intent('stable-key'), (value) => value);
  const second = await api.write(intent('stable-key'), (value) => value);

  assert.equal(first.run_id, 'run-1');
  assert.equal(second.run_id, 'run-1');
  assert.equal(transport.calls.length, 2);
  for (const call of transport.calls) {
    assert.equal(call.path, '/api/v1/runs/run%20one/report-builds');
    assert.equal(call.init.method, 'POST');
    assert.equal(call.init.body, '{"retry_of":null}');
    assert.equal(call.headers.get('X-Actor-ID'), 'demo-user');
    assert.equal(call.headers.get('Idempotency-Key'), 'stable-key');
    assert.equal(call.headers.get('Content-Type'), 'application/json');
    assert.equal(call.headers.get('Accept'), 'application/json, text/event-stream');
    assert.equal(call.init.credentials, 'same-origin');
    assert.equal(call.init.cache, 'no-store');
  }

  const accepted = capture();
  accepted.respond = async () => envelope({ export_id: 'export-1', operation_id: 'op-1', status: 'queued' }, { request_id: 'req-202' }, {}, 202);
  const acceptedApi = createGuidedApi('demo-user', accepted.fetcher);
  const acceptedResult = await acceptedApi.write(intent(), (value) => value);
  assert.equal(acceptedResult.status, 'queued');
  assert.equal(accepted.calls[0].path, '/api/v1/runs/run%20one/report-builds');

  const conflict = capture();
  conflict.respond = async () => errorEnvelope(409, 'IDEMPOTENCY_CONFLICT', 'key conflict');
  const conflictApi = createGuidedApi('demo-user', conflict.fetcher);
  await assert.rejects(conflictApi.write(intent(), (value) => value), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, 'IDEMPOTENCY_CONFLICT');
    assert.equal(error.requestId, 'req-error');
    assert.equal(error.unknown, false);
    return true;
  });
  assert.equal(conflict.calls.length, 1, 'writes must not be automatically retried');
});

test('bad success envelopes, bad DTOs, 404, and unknown write results are explicit', async () => {
  const malformedEnvelope = capture();
  malformedEnvelope.respond = async () => new Response(JSON.stringify({}), {
    status: 200,
    headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-no-data' },
  });
  const missingData = createGuidedApi('demo-user', malformedEnvelope.fetcher);
  await assert.rejects(missingData.read('/demo', (value) => value), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.code, 'INVALID_ENVELOPE');
    assert.equal(error.requestId, 'header-no-data');
    return true;
  });

  const malformedWrite = capture();
  malformedWrite.respond = async () => new Response('<html>proxy accepted the write</html>', {
    status: 200,
    headers: { 'X-Request-ID': 'header-bad-write' },
  });
  const badWrite = createGuidedApi('demo-user', malformedWrite.fetcher);
  await assert.rejects(badWrite.write(intent(), (value) => value), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.code, 'INVALID_JSON');
    assert.equal(error.unknown, true, 'a successful HTTP response with invalid JSON may have committed the write');
    return true;
  });

  const invalidDto = capture();
  invalidDto.respond = async () => envelope({ unexpected: true }, { request_id: 'req-bad-dto' });
  const badDto = createGuidedApi('demo-user', invalidDto.fetcher);
  await assert.rejects(badDto.write(intent(), () => { throw new GuidedApiError('invalid DTO', 200, 'INVALID_DTO'); }), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.code, 'INVALID_DTO');
    assert.equal(error.unknown, true, 'a successful HTTP response with an invalid DTO may have committed the write');
    return true;
  });

  const notFound = capture();
  notFound.respond = async () => errorEnvelope(404, 'RESOURCE_NOT_FOUND', 'missing');
  const missing = createGuidedApi('demo-user', notFound.fetcher);
  await assert.rejects(missing.read('/demo', (value) => value), (error) => {
    assert.equal(error.status, 404);
    assert.equal(error.code, 'RESOURCE_NOT_FOUND');
    assert.equal(error.requestId, 'req-error');
    return true;
  });

  const network = capture();
  network.fetcher = async () => { throw new TypeError('offline'); };
  const offline = createGuidedApi('demo-user', network.fetcher);
  await assert.rejects(offline.write(intent(), (value) => value), (error) => {
    assert.ok(error instanceof GuidedApiError);
    assert.equal(error.code, 'NETWORK_ERROR');
    assert.equal(error.unknown, true);
    assert.match(error.message, /未知/);
    return true;
  });

  const serverError = capture();
  serverError.respond = async () => errorEnvelope(503, 'RUNNER_UNAVAILABLE', 'unavailable');
  const unavailable = createGuidedApi('demo-user', serverError.fetcher);
  await assert.rejects(unavailable.write(intent(), (value) => value), (error) => {
    assert.equal(error.status, 503);
    assert.equal(error.unknown, true);
    return true;
  });
  assert.equal(serverError.calls.length, 1, 'server failures must not trigger an automatic retry');
});

test('receipts preserve completed and pending contract states, while 404 stays a normal not-found', async () => {
  const receipts = [
    { status: 200, data: { state: 'completed', target_path: '/api/v1/demo/bootstrap', request_sha256: HASH, response: { project_id: 'p1' } } },
    { status: 202, data: { state: 'pending', target_path: '/api/v1/demo/bootstrap', request_sha256: HASH, response: null } },
  ];
  for (const receipt of receipts) {
    const transport = capture();
    transport.respond = async () => new Response(JSON.stringify({ data: receipt.data, meta: { request_id: 'req-receipt' } }), {
      status: receipt.status,
      headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'header-receipt' },
    });
    const api = createGuidedApi('demo-user', transport.fetcher);
    const result = await api.receipt('write-key');
    assert.equal(result.state, receipt.data.state);
    assert.equal(result.target_path, '/api/v1/demo/bootstrap');
    assert.equal(result.request_sha256, HASH);
    assert.deepEqual(result.response, receipt.data.response);
    assert.equal(transport.calls[0].path, '/api/v1/demo/write-receipts/write-key');
  }

  const missing = capture();
  missing.respond = async () => errorEnvelope(404, 'RESOURCE_NOT_FOUND', 'receipt missing');
  const api = createGuidedApi('demo-user', missing.fetcher);
  await assert.rejects(api.receipt('missing'), (error) => {
    assert.equal(error.status, 404);
    assert.equal(error.code, 'RESOURCE_NOT_FOUND');
    return true;
  });
  assert.equal(validateReceipt({
    state: 'completed',
    target_path: '/api/v1/demo/bootstrap',
    request_sha256: HASH,
    response: null,
  }).state, 'completed');
  assert.throws(() => validateReceipt({ state: 'unknown', target_path: '/x', request_sha256: HASH, response: null }), /state/);
});

test('partial exports accept planned filenames and code-only errors from the live API', async () => {
  const result = validateExport({
    id: 'export-partial', run_id: 'run-1', status: 'partial',
    operation_id: 'op-1', report_hash: HASH, created_at: '2026-09-27T00:00:00Z',
    items: [{
      format: 'bundle_zip', status: 'failed', artifact_id: null,
      sha256: null, bytes: null, media_type: null, filename: 'report-bundle.zip',
      error: { code: 'EXPORT_INCOMPLETE' },
    }],
  });
  assert.equal(result.items[0].filename, 'report-bundle.zip');
  assert.deepEqual(result.items[0].error, { code: 'EXPORT_INCOMPLETE', message: 'EXPORT_INCOMPLETE' });
  assert.equal(result.items[0].artifact_id, null);
});

test('export items retain every safe registration field and reject missing or invalid metadata', async () => {
  const item = {
    format: 'report_json',
    status: 'ready',
    artifact_id: HASH,
    sha256: HASH,
    bytes: 42,
    media_type: 'application/json',
    filename: 'report.json',
  };
  const failed = {
    format: 'report_pdf',
    status: 'failed',
    artifact_id: null,
    sha256: null,
    bytes: null,
    media_type: null,
    filename: null,
    error: { code: 'PDF_UNAVAILABLE', message: 'unavailable' },
  };
  const exportValue = validateExport({
    id: 'export-1',
    run_id: 'run-1',
    status: 'partial',
    operation_id: 'op-1',
    report_hash: HASH,
    items: [item, failed],
    created_at: '2026-09-27T00:00:00Z',
  });

  assert.deepEqual(exportValue.items[0], { ...item, error: null });
  assert.equal(exportValue.items[1].error.code, 'PDF_UNAVAILABLE');

  for (const override of [
    { format: 'unknown' },
    { sha256: 'not-a-hash' },
    { bytes: -1 },
    { media_type: 42 },
    { filename: 42 },
    { media_type: undefined },
    { filename: undefined },
  ]) {
    assert.throws(() => validateExport({
      ...exportValue,
      items: [{ ...item, ...override }],
    }), GuidedApiError);
  }

  const missingMetadata = capture();
  missingMetadata.respond = async () => envelope([{ ...exportValue, items: [{ ...item, bytes: undefined }] }]);
  const api = createGuidedApi('demo-user', missingMetadata.fetcher);
  await assert.rejects(api.readArray('/runs/run-1/exports', validateExport), /bytes/);
});

test('text offsets remain UTF-8 byte offsets and page cursors stay opaque', async () => {
  const multibyte = '你好';
  const encodedBytes = new TextEncoder().encode(multibyte).byteLength;
  const chunk = validateTextChunk({
    artifact_id: HASH,
    text: multibyte,
    offset: 0,
    next_offset: encodedBytes,
    eof: true,
  });
  assert.equal(chunk.next_offset, encodedBytes);
  assert.notEqual(chunk.next_offset, multibyte.length);
  assert.throws(() => validateTextChunk({
    artifact_id: HASH,
    text: multibyte,
    offset: 0,
    next_offset: encodedBytes + 1,
    eof: true,
  }), /UTF-8|byte|offset/);

  const cursorApi = createGuidedApi('demo-user', async () => envelope([{ id: 'one' }], { request_id: 'req-page' }, {
    page: { has_more: true, next_cursor: 'opaque-cursor-42' },
  }));
  const page = await cursorApi.readArray('/projects/project/items', (value) => value);
  assert.equal(page.next_cursor, 'opaque-cursor-42');
  assert.equal(page.has_more, true);

  const numericCursor = createGuidedApi('demo-user', async () => envelope([{ id: 'one' }], { request_id: 'req-page' }, {
    page: { has_more: true, next_cursor: '42' },
  }));
  assert.equal((await numericCursor.readArray('/projects/project/items', (value) => value)).next_cursor, '42');

  const noPage = createGuidedApi('demo-user', async () => envelope([{ id: 'one' }], { request_id: 'req-page' }));
  assert.deepEqual(await noPage.readArray('/projects/project/items', (value) => value), {
    items: [{ id: 'one' }],
    has_more: false,
    next_cursor: null,
  });
});

test('downloads verify MIME, length, and SHA-256 before creating a real Blob and browser download', async () => {
  const bytes = new TextEncoder().encode('downloaded bytes');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  const created = [];
  const clicked = [];
  const revoked = [];
  const originalBlob = globalThis.Blob;
  const originalCreateUrl = globalThis.URL?.createObjectURL;
  const originalRevokeUrl = globalThis.URL?.revokeObjectURL;
  const originalAnchor = globalThis.HTMLAnchorElement;
  const originalDocument = globalThis.document;
  const TestAnchor = class {
    constructor() {
      this.clicked = false;
      this.href = '';
      this.download = '';
    }
    click() {
      this.clicked = true;
      clicked.push(this);
    }
    remove() {}
  };

  globalThis.document = { createElement: (tag) => {
    assert.equal(tag, 'a');
    return new TestAnchor();
  } };

  globalThis.Blob = class TestBlob {
    constructor(parts, options = {}) {
      this.parts = parts;
      this.type = options.type ?? '';
      created.push(this);
    }
  };
  globalThis.URL.createObjectURL = (blob) => {
    assert.equal(blob, created[0]);
    return 'blob:test-object-url';
  };
  globalThis.URL.revokeObjectURL = (url) => revoked.push(url);
  globalThis.HTMLAnchorElement = class TestAnchor {
    constructor() {
      this.clicked = false;
      this.href = '';
      this.download = '';
    }
    click() {
      this.clicked = true;
      clicked.push(this);
    }
    remove() {}
  };

  try {
    const transport = capture();
    transport.respond = async () => new Response(bytes, {
      status: 200,
      headers: { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': String(bytes.byteLength) },
    });
    const api = createGuidedApi('demo-user', transport.fetcher);
    await api.download({ artifact_id: HASH, filename: 'report.json', bytes: bytes.byteLength, sha256, media_type: 'application/json' });

    assert.equal(created.length, 1);
    assert.equal(created[0].type, 'application/json');
    assert.equal(clicked.length, 1);
    assert.equal(clicked[0].href, 'blob:test-object-url');
    assert.match(clicked[0].download, /report|artifact/);
    assert.deepEqual(revoked, ['blob:test-object-url']);
    assert.equal(transport.calls[0].path, `/api/v1/artifacts/${HASH}/download`);
    assert.equal(transport.calls[0].headers.get('Accept'), 'application/octet-stream');
    assert.equal(transport.calls[0].headers.get('X-Actor-ID'), 'demo-user');

    const badMime = capture();
    badMime.respond = async () => new Response(bytes, {
      status: 200,
      headers: { 'Content-Type': 'text/plain', 'Content-Length': String(bytes.byteLength) },
    });
    const badMimeApi = createGuidedApi('demo-user', badMime.fetcher);
    await assert.rejects(badMimeApi.download({ artifact_id: HASH, filename: 'report.json', bytes: bytes.byteLength, sha256, media_type: 'application/json' }), /MIME/);
    assert.equal(created.length, 1, 'failed validation must not create another download blob');

    const badHash = capture();
    badHash.respond = async () => new Response(bytes, {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'Content-Length': String(bytes.byteLength) },
    });
    const badHashApi = createGuidedApi('demo-user', badHash.fetcher);
    await assert.rejects(badHashApi.download({ artifact_id: HASH, filename: 'report.json', bytes: bytes.byteLength, sha256: 'b'.repeat(64), media_type: 'application/json' }), /SHA-256/);
    assert.equal(created.length, 1, 'failed validation must not create another download blob');

    const badLength = capture();
    badLength.respond = async () => new Response(bytes, {
      status: 200,
      headers: { 'Content-Type': 'application/json', 'Content-Length': String(bytes.byteLength + 1) },
    });
    const badLengthApi = createGuidedApi('demo-user', badLength.fetcher);
    await assert.rejects(badLengthApi.download({ artifact_id: HASH, filename: 'report.json', bytes: bytes.byteLength, sha256, media_type: 'application/json' }), /length/);
    assert.equal(created.length, 1, 'failed validation must not create another download blob');
  } finally {
    globalThis.Blob = originalBlob;
    globalThis.HTMLAnchorElement = originalAnchor;
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
    if (originalCreateUrl) globalThis.URL.createObjectURL = originalCreateUrl;
    else delete globalThis.URL.createObjectURL;
    if (originalRevokeUrl) globalThis.URL.revokeObjectURL = originalRevokeUrl;
    else delete globalThis.URL.revokeObjectURL;
  }
});

test('run events use the real seq contract, reject invalid cursors, and do not drop seq 0', async () => {
  const transport = capture();
  transport.respond = async () => new Response([
    'data: {"seq":0,"run_id":"run-1","type":"run.created","occurred_at":"2026-09-27T00:00:00Z","payload":{"status":"queued"}}',
    '',
    'data: {"seq":1,"run_id":"run-1","type":"run.started","occurred_at":"2026-09-27T00:00:01Z","payload":{"status":"running"}}',
    '',
  ].join('\n'), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
  const api = createGuidedApi('demo-user', transport.fetcher);
  const events = await api.runEvents('run-1', 0);
  assert.equal(events.length, 2);
  assert.equal(events[0].seq, 0);
  assert.equal(events[1].seq, 1);
  assert.equal(events[0].run_id, 'run-1');
  assert.equal(events[0].payload.status, 'queued');
  assert.equal(transport.calls[0].path, '/api/v1/runs/run-1/events?after=0');
  assert.equal(parseRunEvents('data: {"seq":0,"run_id":"run-1","type":"run.created","occurred_at":"now","payload":{}}\n\n', 'run-1')[0].seq, 0);

  await assert.rejects(api.runEvents('run-1', -1), /non-negative/);
  await assert.rejects(api.runEvents('run-1', 1.5), /integer/);
  assert.equal(transport.calls.length, 1, 'invalid cursor requests must not reach the transport');
  assert.equal(GUIDED_PATHS.runEvents('run 1'), '/runs/run%201/events');
});

test('paths encode every identifier and never expose storage keys', () => {
  assert.equal(GUIDED_PATHS.experiments('project/one'), '/projects/project%2Fone/experiments');
  assert.equal(GUIDED_PATHS.experiment('exp id'), '/experiments/exp%20id');
  assert.equal(GUIDED_PATHS.text('artifact/id'), '/artifacts/artifact%2Fid/text');
  assert.equal(GUIDED_PATHS.download('artifact/id'), '/artifacts/artifact%2Fid/download');
});
