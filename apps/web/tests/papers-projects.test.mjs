import test from 'node:test';
import assert from 'node:assert/strict';
import { createPapersApi } from '../src/api-client/papers.ts';

// Project description / read / delete client calls against a fake fetcher.

function capture(respond) {
  const calls = [];
  const fetcher = async (path, init = {}) => {
    calls.push({ path, init, headers: new Headers(init.headers), body: init.body });
    return respond(path, init, calls.length);
  };
  return { calls, fetcher };
}

const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'Content-Type': 'application/json', 'X-Request-ID': 'hdr' },
});
const ok = (data, status = 200) => json({ data, meta: { request_id: 'req-ok' } }, status);
const project = (over = {}) => ({
  id: 'p', name: 'Demo', description: '目标', role: 'owner', version: 3,
  deletion_state: 'active', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z', ...over,
});

test('updateProjectDescription PATCHes with If-Match, Idempotency-Key and the new version', async () => {
  const { calls, fetcher } = capture(() => ok(project({ description: '目标', version: 4 })));
  const result = await createPapersApi('actor', fetcher).updateProjectDescription('p', '目标', 3);
  assert.equal(calls[0].path, '/api/v1/projects/p');
  assert.equal(calls[0].init.method, 'PATCH');
  assert.equal(calls[0].headers.get('If-Match'), '"3"');
  assert.ok(calls[0].headers.get('Idempotency-Key'));
  assert.equal(calls[0].headers.get('Content-Type'), 'application/json');
  assert.equal(calls[0].headers.get('X-Actor-ID'), 'actor');
  assert.equal(calls[0].body, JSON.stringify({ description: '目标' }));
  assert.equal(result.version, 4);
  assert.equal(result.description, '目标');
});

test('updateProjectDescription surfaces REVISION_CONFLICT', async () => {
  const { fetcher } = capture(() => json({ error: { code: 'REVISION_CONFLICT', message: 'x' } }, 409));
  await assert.rejects(createPapersApi('a', fetcher).updateProjectDescription('p', 'd', 1), e => e.code === 'REVISION_CONFLICT');
});

test('getProject reads one project', async () => {
  const { calls, fetcher } = capture(() => ok(project()));
  const result = await createPapersApi('a', fetcher).getProject('p');
  assert.equal(calls[0].path, '/api/v1/projects/p');
  assert.equal(calls[0].init.method, 'GET');
  assert.equal(result.description, '目标');
  assert.equal(result.version, 3);
  assert.equal(result.updated_at, '2026-01-02T00:00:00Z');
});

test('deleteProject sends DELETE and resolves on 204', async () => {
  const { calls, fetcher } = capture(() => new Response(null, { status: 204 }));
  await createPapersApi('a', fetcher).deleteProject('p');
  assert.equal(calls[0].path, '/api/v1/projects/p');
  assert.equal(calls[0].init.method, 'DELETE');
});

test('deleteProject rejects with PROJECT_HAS_TASKS on 409', async () => {
  const { fetcher } = capture(() => json({ error: { code: 'PROJECT_HAS_TASKS', message: 'x' } }, 409));
  await assert.rejects(createPapersApi('a', fetcher).deleteProject('p'), e => e.code === 'PROJECT_HAS_TASKS');
});

test('listProjects defaults description and version', async () => {
  const { fetcher } = capture(() => ok([{ id: 'a', name: 'A' }, project({ id: 'b', description: 'hi', version: 7 })]));
  const items = await createPapersApi('a', fetcher).listProjects();
  assert.equal(items[0].description, '');
  assert.equal(items[0].version, 1);
  assert.equal(items[1].description, 'hi');
  assert.equal(items[1].version, 7);
});
