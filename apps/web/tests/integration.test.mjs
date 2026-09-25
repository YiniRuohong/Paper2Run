import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const loadApi = () => import('../src/api-client/phase01.ts');
const envelope = (data = {}) => new Response(JSON.stringify({ data }));

test('null error responses retain HTTP and request_id diagnostics', async () => {
  const { createApi, ApiError } = await loadApi();
  const api = createApi('owner', async () => new Response('null', { status: 502, headers: { 'X-Request-ID': 'req-proxy' } }));
  await assert.rejects(api.cell('s', 'a'), (error) => error instanceof ApiError && error.status === 502 && error.requestId === 'req-proxy');
});

test('post-write refresh failures preserve API diagnostics without claiming write failure', async () => {
  const { ApiError, snapshotRefreshError } = await loadApi();
  const reason = new ApiError('offline snapshot', 503, 'UNAVAILABLE', 'req-refresh', { retryable: true });
  const error = snapshotRefreshError(reason);
  assert.equal(error.requestId, 'req-refresh');
  assert.equal(error.status, 503);
  assert.equal(error.code, 'UNAVAILABLE');
  assert.deepEqual(error.details, { retryable: true });
  assert.match(error.message, /写请求已返回/);
  assert.match(error.message, /勿重复写入/);
});

test('entry lazily mounts independent surfaces and product client is explicitly mock', async () => {
  const entry = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
  assert.match(entry, /resolveSurface\(window.location.pathname, import.meta.env.VITE_DATA_MODE\)/);
  assert.match(entry, /lazy\(\(\) => import\('\.\/features\/lab\/Phase01Lab'\)\)/);
  assert.match(entry, /lazy\(\(\) => import\('\.\/MockWorkspace'\)\)/);
  assert.doesNotMatch(entry, /import .* from ['"]\.\/(?:app|api-client)/);
  const config = await readFile(new URL('../src/api-client/config.ts', import.meta.url), 'utf8');
  assert.match(config, /dataMode: DataMode = 'mock'/);
});

test('lab bypasses product mode and auth for mock, live and replay', async () => {
  const { resolveSurface } = await import('../src/routing.ts');
  for (const mode of ['mock', 'live', 'replay']) {
    for (const path of ['/lab', '/lab/']) {
      assert.deepEqual(resolveSurface(path, mode), { kind: 'lab', basename: '/' });
    }
    assert.deepEqual(resolveSurface('/mock/projects', mode), { kind: 'mock', basename: '/mock' });
  }
  assert.equal(resolveSurface('/laboratory', 'live').kind, 'unavailable');
  assert.equal(resolveSurface('/lab/unknown', 'live').kind, 'unavailable');
});

test('default mock preserves four views; live and replay never enter product auth', async () => {
  const { resolveSurface } = await import('../src/routing.ts');
  for (const path of ['/', '/projects', '/papers/p/review', '/runs/r', '/runs/r/report', '/login']) {
    assert.equal(resolveSurface(path, 'mock').kind, 'mock');
    assert.equal(resolveSurface(path, 'live').kind, 'unavailable');
    assert.equal(resolveSurface(path, 'replay').kind, 'unavailable');
  }
  assert.equal(resolveSurface('/mocking', 'live').kind, 'unavailable');
});

test('ambiguous write retry reuses identical key/body; successful next operation is new', async () => {
  const { createApi } = await loadApi();
  const calls = [];
  const api = createApi('owner', async (path, init) => {
    calls.push({ path, body: init.body, key: init.headers.get('Idempotency-Key') });
    if (calls.length === 1) throw new TypeError('offline');
    return envelope({ id: 'cell' });
  });
  await assert.rejects(api.cell('s', 'print(1)'));
  assert.equal(calls.length, 1, 'no automatic retries');
  await api.cell('s', 'print(1)');
  await api.cell('s', 'print(1)');
  assert.deepEqual(calls[0], calls[1]);
  assert.notEqual(calls[1].key, calls[2].key);
});

test('changed body or target never reuses another operation key', async () => {
  const { createApi } = await loadApi();
  const calls = [];
  const api = createApi('owner', async (path, init) => {
    calls.push({ path, key: init.headers.get('Idempotency-Key'), body: init.body });
    throw new TypeError('offline');
  });
  for (const [session, code] of [['s', 'a'], ['s', 'b'], ['other', 'a'], ['s', 'a']]) {
    await assert.rejects(api.cell(session, code));
  }
  assert.equal(new Set(calls.slice(0, 3).map((call) => call.key)).size, 3);
  assert.deepEqual(calls[0], calls[3]);
});

test('server errors and malformed success retain write identity without automatic retries', async () => {
  const { createApi } = await loadApi();
  for (const response of [
    () => new Response('{}', { status: 500 }),
    () => new Response('<html>bad proxy</html>'),
    () => new Response('{}'),
  ]) {
    const keys = [];
    const api = createApi('owner', async (_path, init) => {
      keys.push(init.headers.get('Idempotency-Key'));
      return keys.length === 1 ? response() : envelope();
    });
    await assert.rejects(api.restore('s', 'cp'));
    assert.equal(keys.length, 1);
    await api.restore('s', 'cp');
    assert.equal(keys[0], keys[1]);
  }
});

test('pending checkpoint body is serialized before await and key follows its snapshot', async () => {
  const { createApi } = await loadApi();
  const calls = [];
  const api = createApi('owner', async (_path, init) => {
    calls.push({ body: init.body, key: init.headers.get('Idempotency-Key') });
    throw new TypeError('offline');
  });
  const body = { name: 'before', manifest: { action_cursor: 1 } };
  const first = api.checkpoint('s', body);
  body.name = 'after';
  await assert.rejects(first);
  await assert.rejects(api.checkpoint('s', body));
  await assert.rejects(api.checkpoint('s', { name: 'before', manifest: { action_cursor: 1 } }));
  assert.equal(JSON.parse(calls[0].body).name, 'before');
  assert.notEqual(calls[0].key, calls[1].key);
  assert.deepEqual(calls[0], calls[2]);
});

test('live lab imports no mock/auth client and mounts no duplicate root', async () => {
  const source = await readFile(new URL('../src/features/lab/Phase01Lab.tsx', import.meta.url), 'utf8');
  assert.match(source, /api-client\/phase01/);
  assert.doesNotMatch(source, /createRoot|getClient/);
  const imports = source.match(/^import\s[\s\S]*?from\s*['"][^'"]+['"];|^import\s*['"][^'"]+['"];|import\s*\(['"][^'"]+['"]\)/gm) ?? [];
  for (const statement of imports) assert.doesNotMatch(statement, /mock\/|auth\//);
  assert.match(source, /href="\/mock\/projects"/);
  assert.match(source, /LIVE/);
  assert.match(source, /非生产 sandbox/);
  const client = await readFile(new URL('../src/api-client/phase01.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(client, /mock\/|getClient|localStorage|auth\//);
});

test('legacy modules delegate and lab CSS cannot leak global selectors', async () => {
  const oldClient = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(oldClient, /fetcher\(|createRoot/);
  const css = await readFile(new URL('../src/features/lab/lab.css', import.meta.url), 'utf8');
  assert.doesNotMatch(css, /:root|(^|[{}])\s*(body|html)\s*\{/m);
  for (const [, selector] of css.matchAll(/(?:^|(?<=[{}]))\s*([^{}]+)\{/g)) {
    if (selector.trim().startsWith('@')) continue;
    for (const part of selector.split(',')) assert.match(part.trim(), /^\.phase01-lab(?:[\s.:]|$)/);
  }
});
