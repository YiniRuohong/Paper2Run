import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createAutonomousApi } from '../src/api-client/autonomous.ts';
import { defaultConfig, validateConfig, modelReference, profileRequest, reusableProfiles, researchTaskTypes } from '../src/features/autonomous/config.ts';

const profile = { id: 'main', model: 'model-a', base_url: 'https://example.test/v1', api: 'openai-completions', secret_configured: true };
const specialist = () => ({ id: 'research-advisor', description: 'Review experimental design', task_types: ['method_design', 'review'], model: modelReference({ ...profile, id: 'expert' }) });
function config() {
  const value = defaultConfig('p'); value.model = modelReference(profile); value.experiment.objective = 'test'; return value;
}
const envelope = data => new Response(JSON.stringify({ data }));

test('legacy missing specialists and explicit empty list retain single-model config', () => {
  const value = config();
  assert.equal(Object.hasOwn(value, 'specialists'), false);
  assert.doesNotThrow(() => validateConfig(value));
  assert.doesNotThrow(() => validateConfig({ ...value, specialists: [] }));
  assert.equal(Object.hasOwn(value, 'specialists'), false);
});

test('specialist configs round-trip through versioned save and reload without secrets', async () => {
  let stored; const calls = [];
  const api = createAutonomousApi('actor', async (path, init) => {
    calls.push({ path, init });
    if (init.method === 'PUT') stored = JSON.parse(init.body).config;
    return envelope({ config: stored, version: 3 });
  }, () => 'csrf');
  const value = { ...config(), specialists: [specialist(), { ...specialist(), id: 'coder', task_types: researchTaskTypes.map(t => t.value) }] };
  validateConfig(value);
  const saved = await api.saveConfig('p', value, 2);
  assert.deepEqual(saved, await api.config('p'));
  assert.deepEqual(saved.config, value);
  assert.equal(new Headers(calls[0].init.headers).get('If-Match'), '2');
  assert.doesNotMatch(calls[0].init.body, /api_key|reuse_credentials_from/);
});

test('specialist validation enforces name, uniqueness, bounds and valid nonempty task type sets', () => {
  const valid = specialist();
  for (const change of [
    { id: '' }, { id: '-bad' }, { id: '中文' }, { id: 'a'.repeat(129) },
    { description: '' }, { description: '  ' }, { description: 'a'.repeat(8193) },
    { task_types: [] }, { task_types: ['review', 'review'] }, { task_types: ['execute'] }, { task_types: 'review' },
    { model: { provider_profile: '', credential_ref: '' } },
  ]) assert.throws(() => validateConfig({ ...config(), specialists: [{ ...valid, ...change }] }), JSON.stringify(change));
  for (const specialists of [null, {}, 'review']) assert.throws(() => validateConfig({ ...config(), specialists }));
  assert.throws(() => validateConfig({ ...config(), specialists: [valid, valid] }), /重复/);
  assert.throws(() => validateConfig({ ...config(), specialists: Array.from({ length: 17 }, (_, i) => ({ ...valid, id: `expert-${i}` })) }), /16/);
  assert.doesNotThrow(() => validateConfig({ ...config(), specialists: Array.from({ length: 16 }, (_, i) => ({ ...valid, id: `expert-${i}`, description: 'a'.repeat(8192) })) }));
  assert.doesNotThrow(() => validateConfig({ ...config(), specialists: [{ ...valid, id: `A${'_.-0'.repeat(31)}abc` }] }));
});

test('profile requests default to API key and reuse only configured same-endpoint credentials', async () => {
  const target = { ...profile, id: 'expert', model: 'model-b' };
  const sources = [profile, { ...profile, id: 'other', base_url: 'https://other.test/v1' }, { ...profile, id: 'protocol', api: 'anthropic-messages' }, { ...profile, id: 'none', auth_mode: 'none' }, { ...profile, id: 'empty', secret_configured: false }];
  assert.equal(profileRequest(target, '', '', sources).auth_mode, 'api-key');
  assert.deepEqual(reusableProfiles(target, sources).map(p => p.id), ['main']);
  const body = profileRequest(target, '', 'main', sources);
  assert.deepEqual(body, { id: 'expert', model: 'model-b', base_url: profile.base_url, api: profile.api, auth_mode: 'api-key', reuse_credentials_from: 'main' });
  let sent;
  const api = createAutonomousApi('actor', async (_path, init) => { sent = JSON.parse(init.body); return envelope(target); }, () => 'csrf');
  await api.saveProfile(body);
  assert.deepEqual(sent, body);
  assert.equal(Object.hasOwn(sent, 'api_key'), false);
  for (const id of ['other', 'protocol', 'none', 'empty', 'missing']) assert.throws(() => profileRequest(target, '', id, sources), /同一/);
  assert.throws(() => profileRequest(target, 'secret', 'main', sources), /只能选择一种/);
  assert.throws(() => profileRequest(target, '  ', '', sources));
  assert.equal(Object.hasOwn(target, 'reuse_credentials_from'), false);
});

test('credential-reference profiles cannot be reused even when their secrets are configured', () => {
  const target = { ...profile, id: 'expert', model: 'model-b' };
  for (const credential_ref of ['env:MODEL_API_KEY', 'credential-store:managed', 'profile:main', ' ']) {
    const source = { ...profile, credential_ref, secret_configured: true };
    assert.deepEqual(reusableProfiles(target, [source]), []);
    assert.throws(() => profileRequest(target, '', source.id, [source]), /同一/);
  }
  assert.deepEqual(reusableProfiles(target, [{ ...profile, credential_ref: '' }]).map(p => p.id), ['main']);
});

test('anonymous requests only allow SDK-supported HTTP loopback hosts and omit all credential fields', async () => {
  for (const base_url of ['http://localhost:3425/v1', 'http://127.0.0.1:3425/v1', 'http://[::1]:3425/v1']) {
    const input = { ...profile, base_url, auth_mode: 'none', credential_ref: 'profile:main' };
    const body = profileRequest(input, 'secret', 'main', [profile]);
    assert.equal(body.auth_mode, 'none');
    assert.equal(Object.hasOwn(body, 'api_key'), false);
    assert.equal(Object.hasOwn(body, 'credential_ref'), false);
    assert.equal(Object.hasOwn(body, 'reuse_credentials_from'), false);
    let sent;
    const api = createAutonomousApi('actor', async (_path, init) => { sent = JSON.parse(init.body); return envelope(body); }, () => 'csrf');
    await api.saveProfile({ ...input, api_key: 'secret', reuse_credentials_from: 'main' });
    assert.doesNotMatch(JSON.stringify(sent), /secret|credential_ref|reuse_credentials_from/);
    assert.equal(Object.hasOwn(sent, 'secret_configured'), false);
    // Project references stay valid, even though anonymous profile requests carry no credentials.
    assert.equal(modelReference(body).credential_ref, 'profile:main');
  }
  for (const base_url of ['http://127.0.0.2/v1', 'http://127.2.3.4/v1', 'https://localhost/v1', 'http://example.test/v1', 'http://localhost.example.test/v1', 'http://192.168.1.2/v1', 'http://user:pass@localhost/v1', 'http://localhost/v1?key=secret', 'file:///tmp/model']) {
    assert.throws(() => profileRequest({ ...profile, base_url, auth_mode: 'none' }, '', '', []), base_url);
  }
});

test('autonomous source never persists key or reuse source in browser storage or query', async () => {
  for (const file of ['../src/features/autonomous/NewExperimentPage.tsx', '../src/features/autonomous/TaskPage.tsx']) {
    const page = await readFile(new URL(file, import.meta.url), 'utf8');
    assert.doesNotMatch(page, /localStorage|sessionStorage/, file);
    for (const line of page.split('\n').filter(line => /replaceState|query\.get/.test(line))) assert.doesNotMatch(line, /api_key|reuseCredentials|credential_ref/);
  }
  const page = await readFile(new URL('../src/features/autonomous/NewExperimentPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /setKey\(''\); setReuseCredentials\(''\)/);
  assert.match(page, /专家输出仅建议/);
  assert.match(page, /共享总预算/);
});
