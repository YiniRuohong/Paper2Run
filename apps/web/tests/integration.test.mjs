import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile, readdir } from 'node:fs/promises';

test('the product entry is the autonomous-only app shell', async () => {
  const entry = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
  assert.match(entry, /lazy\(\(\) => import\('\.\/shell\/AppShell'\)\)/);
  assert.doesNotMatch(entry, /import .* from ['"]\.\/(?:app|api|routing|types|MockWorkspace)/);
});

test('retired guided/lab/mock surfaces are gone from the source tree', async () => {
  const features = (await readdir(new URL('../src/features', import.meta.url))).sort();
  assert.deepEqual(features, ['autonomous', 'docparse', 'flow', 'overview', 'settings']);
  const clients = (await readdir(new URL('../src/api-client', import.meta.url))).sort();
  assert.deepEqual(clients, ['autonomous.ts', 'client.ts', 'papers.ts']);
  const root = (await readdir(new URL('../src', import.meta.url))).sort();
  for (const retired of ['api.ts', 'app.tsx', 'routing.ts', 'types.ts', 'styles.css', 'MockWorkspace.tsx', 'components', 'formatters']) {
    assert.ok(!root.includes(retired), `${retired} must stay deleted`);
  }
});

test('error envelopes retain HTTP and request_id diagnostics', async () => {
  const { errorEnvelope } = await import('../src/api-client/client.ts');
  const error = errorEnvelope(null, new Response('null', { status: 502, headers: { 'X-Request-ID': 'req-proxy' } }));
  assert.equal(error.status, 502);
  assert.equal(error.requestId, 'req-proxy');
  assert.equal(error.unknown, true, 'a non-envelope error is an unknown outcome');
});

test('malformed success payloads are rejected before any data reaches the UI', async () => {
  const { envelopeData } = await import('../src/api-client/client.ts');
  assert.throws(() => envelopeData(null, 200), /缺少 data/);
  assert.throws(() => envelopeData({ meta: {} }, 200), /缺少 data/);
  const { data, requestId } = envelopeData({ data: [1], meta: { request_id: 'r-1' } }, 200);
  assert.deepEqual(data, [1]);
  assert.equal(requestId, 'r-1');
});
