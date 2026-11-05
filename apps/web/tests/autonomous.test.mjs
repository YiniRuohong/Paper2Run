import test from 'node:test';
import assert from 'node:assert/strict';
import { createAutonomousApi } from '../src/api-client/autonomous.ts';
import { defaultConfig, validateConfig, parseProtocol, protocolApproval, mergeEvents, stateLabel, terminalState } from '../src/features/autonomous/config.ts';
const envelope = (data, status = 200) => new Response(JSON.stringify({ data, meta: { request_id: 'req-1' } }), { status });
function capture(data) {
  const calls = [];
  const api = createAutonomousApi('actor', async (path, init) => { calls.push({ path, init, headers: new Headers(init.headers) }); return typeof data === 'function' ? data(path, init) : envelope(data); }, () => 'csrf-token');
  return { api, calls };
}
test('config uses version precondition, actor, csrf and idempotency; no raw credential', async () => {
  const config = defaultConfig('p/1'); config.model = { provider_profile: 'model', credential_ref: 'profile:model' };
  const { api, calls } = capture({ version: 3, config });
  await api.saveConfig('p/1', config, 2);
  assert.equal(calls[0].path, '/api/v1/autonomous/projects/p%2F1/config');
  assert.equal(calls[0].init.method, 'PUT');
  assert.equal(calls[0].headers.get('If-Match'), '2');
  assert.equal(calls[0].headers.get('X-Actor-ID'), 'actor');
  assert.equal(calls[0].headers.get('X-CSRF-Token'), 'csrf-token');
  assert.ok(calls[0].headers.get('Idempotency-Key'));
  assert.equal(calls[0].init.credentials, 'same-origin');
  assert.equal(calls[0].init.body.includes('api_key'), false);
});
test('config conflicts and auth errors preserve request id and status', async () => {
  for (const status of [401, 409, 412]) {
    const { api } = capture(() => new Response(JSON.stringify({ error: { code: 'CONFLICT', message: 'Refresh config' }, meta: { request_id: 'req-conflict' } }), { status }));
    await assert.rejects(api.config('p'), e => e.status === status && e.requestId === 'req-conflict' && e.code === 'CONFLICT');
  }
});
test('start binds immutable paper version and explicit authorization', async () => {
  const { api, calls } = capture({ id: 'task-1', state: 'queued' });
  await api.start('p', { objective: 'test', paper_version_id: 'pv-1', protocol: { frozen: true }, authorization: { confirmed: true } });
  assert.deepEqual(JSON.parse(calls[0].init.body), { objective: 'test', paper_version_id: 'pv-1', protocol: { frozen: true }, authorization: { confirmed: true } });
  assert.equal(calls[0].path, '/api/v1/autonomous/projects/p/tasks');
});
test('profiles isolate API key from later config requests', async () => {
  const { api, calls } = capture({ id: 'm', credential_ref: 'profile:m' });
  await api.saveProfile({ id: 'm', model: 'test', base_url: 'https://example.test/v1', api: 'openai-completions', api_key: 'secret' });
  await api.saveConfig('p', defaultConfig('p'));
  assert.equal(JSON.parse(calls[0].init.body).api_key, 'secret');
  assert.equal(calls[1].init.body.includes('secret'), false);
});
test('skill imports, versions and enabled update use fixed contract', async () => {
  const { api, calls } = capture({ id: 's', version: '1.2', enabled: true });
  assert.equal((await api.importSkill('/skills/research')).version, '1.2');
  await api.enableSkill('s/1', false);
  assert.equal(calls[1].path, '/api/v1/autonomous/capabilities/s%2F1');
  assert.equal(calls[1].init.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[1].init.body), { enabled: false });
});
test('events cursor and all task control actions preserve blocked state', async () => {
  const { api, calls } = capture((path) => envelope(path.includes('/events') ? { events: [], next_cursor: 7 } : { id: 't', state: 'blocked', pending_decision: { reason: 'protocol change' }, usage: { model_calls: 2 } }));
  assert.equal((await api.events('t', 6)).next_cursor, 7);
  assert.equal(calls[0].path, '/api/v1/autonomous/tasks/t/events?after=6');
  for (const action of ['pause', 'resume', 'stop', 'message']) {
    const task = await api.control('t', action, action === 'message' ? 'approved' : undefined);
    assert.equal(task.state, 'blocked'); assert.equal(task.usage.model_calls, 2); assert.ok(task.pending_decision);
  }
  assert.deepEqual(JSON.parse(calls.at(-1).init.body), { action: 'message', message: 'approved' });
});
test('protocol approval is explicit and bound to the pending decision ID', async () => {
  const decision = { id: 'decision-1', kind: 'protocol_approval', proposed: { protocol: { parameters: { lr: 0.01 } }, sha256: 'abc' }, changed: ['parameters'], reason: 'New learning rate' };
  assert.equal(protocolApproval(decision)?.id, 'decision-1');
  for (const invalid of [null, {}, { ...decision, id: '' }, { ...decision, proposed: {} }, { ...decision, changed: [1] }]) assert.equal(protocolApproval(invalid), null);
  const { api, calls } = capture({ id: 't', state: 'queued' });
  await api.control('t', 'resume');
  await api.control('t', 'resume', `approve:${protocolApproval(decision).id}`);
  assert.deepEqual(JSON.parse(calls[0].init.body), { action: 'resume' });
  assert.deepEqual(JSON.parse(calls[1].init.body), { action: 'resume', message: 'approve:decision-1' });
});
test('ZIP export errors surface backend diagnostics', async () => {
  const { api, calls } = capture(() => new Response(JSON.stringify({ error: { code: 'REPORT_NOT_READY', message: 'Not ready' } }), { status: 409 }));
  await assert.rejects(api.exportTask('t/1'), e => e.status === 409 && e.code === 'REPORT_NOT_READY');
  assert.equal(calls[0].path, '/api/v1/autonomous/tasks/t%2F1/export');
});
test('network and malformed envelope failures are explicit', async () => {
  const api = createAutonomousApi('actor', async () => { throw new TypeError('offline'); });
  await assert.rejects(api.tasks('p'), e => e.code === 'NETWORK_ERROR');
  const { api: bad } = capture(() => new Response('{}'));
  await assert.rejects(bad.tasks('p'), e => e.code === 'INVALID_ENVELOPE');
});
test('finite budgets, SSH requirements, protocol object and state helpers', () => {
  const config = defaultConfig('p'); config.model = { provider_profile: 'm', credential_ref: 'profile:m' }; config.experiment.objective = 'test';
  validateConfig(config);
  for (const value of [0, -1, Infinity, NaN, 1.5]) { assert.throws(() => validateConfig({ ...config, limits: { ...config.limits, max_model_calls: value } })); }
  assert.throws(() => validateConfig({ ...config, target: { ...config.target, kind: 'ssh' } }));
  assert.throws(() => parseProtocol('[]')); assert.throws(() => parseProtocol('{')); assert.equal(parseProtocol(''), undefined);
  assert.deepEqual(parseProtocol('{"frozen":true}'), { frozen: true });
  assert.equal(stateLabel('blocked'), '已阻塞'); assert.equal(terminalState('running'), false); assert.equal(terminalState('completed'), true);
  assert.deepEqual(mergeEvents([{ sequence: 2, type: 'old' }], [{ sequence: 1 }, { sequence: 2, type: 'new' }]).map(x => x.sequence), [1, 2]);
});
