import test from 'node:test';
import assert from 'node:assert/strict';
import { createApi, ApiError, parseEventSnapshot, buildCheckpoint } from '../src/api-client/phase01.ts';

const event = { sequence_no: 0, type: 'cell.completed', payload: { stdout: 'ok' }, created_at: '2026-09-25T00:00:00Z' };
const json = (data) => new Response(JSON.stringify({ data, meta: { request_id: 'req-1' } }), { headers: { 'Content-Type': 'application/json' } });

test('parses finite SSE snapshots including CRLF and comments', () => {
  assert.deepEqual(parseEventSnapshot(`: snapshot\r\nid: 0\r\nevent: cell.completed\r\ndata: ${JSON.stringify(event)}\r\n\r\n`), [event]);
  assert.deepEqual(parseEventSnapshot(''), []);
  assert.throws(() => parseEventSnapshot('data: not-json\n\n'), /事件/);
});

test('encodes project IDs and attaches actor and a unique UUID to every write', async () => {
  const calls = [];
  const api = createApi('demo-user', async (path, init) => { calls.push({ path, init }); return json({ id: 's' }); });
  await api.createSession('demo/project');
  await api.cell('s', 'print(42)');
  await api.sessions('demo/project');
  assert.equal(calls[0].path, '/api/v1/projects/demo%2Fproject/sessions');
  assert.equal(calls[0].init.headers.get('X-Actor-ID'), 'demo-user');
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  assert.match(calls[0].init.headers.get('Idempotency-Key'), uuid);
  assert.notEqual(calls[0].init.headers.get('Idempotency-Key'), calls[1].init.headers.get('Idempotency-Key'));
  assert.equal(calls[2].init.headers.has('Idempotency-Key'), false);
  assert.deepEqual(JSON.parse(calls[1].init.body), { session_id: 's', source: 'print(42)', requested_capabilities: [] });
  assert.equal(JSON.parse(calls[0].init.body).trust_class, 'untrusted');
});

test('preserves error envelope, details and request id; never retries writes', async () => {
  let count = 0;
  const api = createApi('owner', async () => {
    count++;
    return new Response(JSON.stringify({ error: { code: 'NOT_FOUND', message: 'project missing', details: { field_errors: [] } }, request_id: 'req-404' }), { status: 404 });
  });
  await assert.rejects(api.createSession('missing'), (error) => error instanceof ApiError && error.status === 404 && error.code === 'NOT_FOUND' && error.requestId === 'req-404');
  assert.equal(count, 1);
});

test('rejects malformed success JSON rather than reporting a fake success', async () => {
  const api = createApi('owner', async () => new Response('<html>proxy error</html>'));
  await assert.rejects(api.sessions('demo'), /JSON/);
});

test('health is raw JSON and events use a finite fetch, not EventSource', async () => {
  const api = createApi('owner', async (path) => path === '/healthz'
    ? new Response(JSON.stringify({ status: 'ok', service: 'p2r-api' }))
    : new Response(`id: 0\nevent: cell.completed\ndata: ${JSON.stringify(event)}\n\n`, { headers: { 'Content-Type': 'text/event-stream' } }));
  assert.equal((await api.health()).status, 'ok');
  assert.deepEqual(await api.events('s'), [event]);
});

test('builds checkpoint with real namespace/environment and canonical manifest hash', async () => {
  const cell = { id: 'cell-1', session_id: 's', namespace_hash: 'c'.repeat(64), environment_id: 'env-1' };
  const actions = [{ sequence_no: 0, cell_id: 'cell-1', kind: 'cell.submit' }];
  const payload = await buildCheckpoint('baseline', cell, actions);
  assert.equal(payload.namespace_snapshot_hash, cell.namespace_hash);
  assert.equal(payload.environment_id, 'env-1');
  assert.equal(payload.action_cursor, 1);
  assert.deepEqual(payload.manifest, { action_cursor: 1, environment_id: 'env-1', input_artifact_ids: [] });
  const { createHash } = await import('node:crypto');
  assert.equal(payload.manifest_hash, createHash('sha256').update(JSON.stringify(payload.manifest)).digest('hex'));
  await assert.rejects(buildCheckpoint('bad', cell, [...actions, { sequence_no: 1, cell_id: 'other' }]), /最新/);
  await assert.rejects(buildCheckpoint('bad', { ...cell, namespace_hash: '' }, actions), /元数据/);
});

test('session reads and snapshot cursors use the observed public paths', async () => {
  const paths = [];
  const api = createApi('owner', async (path) => { paths.push(path); return json([]); });
  await api.session('s/id');
  await api.actions('s/id', 99);
  await api.checkpoints('s/id');
  assert.deepEqual(paths, ['/api/v1/sessions/s%2Fid', '/api/v1/sessions/s%2Fid/actions?limit=100&after=99', '/api/v1/sessions/s%2Fid/checkpoints?limit=100']);
});

test('network failures warn about unknown write results without retrying', async () => {
  let calls = 0;
  const api = createApi('owner', async () => { calls++; throw new TypeError('offline'); });
  await assert.rejects(api.cell('s', 'print(1)'), (error) => error instanceof ApiError && error.code === 'NETWORK_ERROR' && error.message.includes('未知'));
  assert.equal(calls, 1);
});

test('rejects missing success envelopes and preserves validation details', async () => {
  const invalid = createApi('owner', async () => new Response('{}'));
  await assert.rejects(invalid.sessions('demo'), /缺少 data/);
  const details = { field_errors: [{ path: 'name', message: 'required' }] };
  const rejected = createApi('owner', async () => new Response(JSON.stringify({ error: { code: 'VALIDATION_ERROR', message: 'invalid', details }, meta: { request_id: 'req-validation' } }), { status: 422 }));
  await assert.rejects(rejected.restore('s', 'cp'), (error) => {
    assert.deepEqual(error.details, details);
    assert.equal(error.requestId, 'req-validation');
    return true;
  });
});

test('checkpoint and restore bodies follow the public contracts', async () => {
  const calls = [];
  const api = createApi('owner', async (path, init) => { calls.push({ path, body: JSON.parse(init.body) }); return json({}); });
  const payload = await buildCheckpoint('baseline', { id: 'c', namespace_hash: 'd'.repeat(64), environment_id: 'env' }, [{ sequence_no: 0, cell_id: 'c' }]);
  await api.checkpoint('s/id', payload);
  await api.restore('s/id', 'cp');
  assert.equal(calls[0].path, '/api/v1/sessions/s%2Fid/checkpoints');
  assert.deepEqual(calls[0].body, payload);
  assert.deepEqual(calls[1].body, { checkpoint_id: 'cp' });
});
