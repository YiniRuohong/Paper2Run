import test from 'node:test';
import assert from 'node:assert/strict';
import { createRemoteApi, RemoteApiError, remoteView, formatCountdown } from '../src/api-client/remote.ts';

// Api-client tests for tablet remote (/api/v1/remote/*) against a mock fetch,
// plus the pure view-state helper the settings group renders from.

const ID = 'a'.repeat(64);

function envelope(data, status = 200) {
  return new Response(JSON.stringify({ data, meta: { request_id: 'r' } }), { status, headers: { 'Content-Type': 'application/json' } });
}
function errorEnvelope(status, code) {
  return new Response(JSON.stringify({ error: { code, message: 'nope', retryable: false, details: {} }, meta: { request_id: 'r' } }), { status, headers: { 'Content-Type': 'application/json' } });
}
function capture(respond) {
  const calls = [];
  const fetcher = async (path, init = {}) => { calls.push({ path, method: init.method ?? 'GET' }); return respond(path, init); };
  return { calls, fetcher };
}

test('status, pairing and revoke hit the right paths and parse responses', async () => {
  const { calls, fetcher } = capture((path, init) => {
    if (path === '/api/v1/remote/status') return envelope({ available: true, remote: false, desktop_id: 'd', relay: 'r', devices: [{ id: ID, name: 'Tab', paired_at: 10, online: true, path: 'relay', last_seen: 11 }] });
    if (path === '/api/v1/remote/pairing' && init.method === 'POST') return envelope({ code: '123-45678', expires_in_ms: 300000 });
    if (path === '/api/v1/remote/pairing') return envelope({ state: 'paired', device: { id: ID, name: 'Tab', paired_at: 10 } });
    if (path === `/api/v1/remote/devices/${ID}`) return new Response(null, { status: 204 });
    return errorEnvelope(404, 'NOPE');
  });
  const api = createRemoteApi(fetcher);
  const s = await api.status();
  assert.equal(s.available, true);
  assert.deepEqual(s.devices[0], { id: ID, name: 'Tab', paired_at: 10, online: true, path: 'relay', last_seen: 11 });
  assert.deepEqual(await api.startPairing(), { code: '123-45678', expires_in_ms: 300000 });
  assert.equal((await api.pairingState()).state, 'paired');
  await api.revoke(ID);
  assert.deepEqual(calls.map(c => `${c.method} ${c.path}`), [
    'GET /api/v1/remote/status', 'POST /api/v1/remote/pairing', 'GET /api/v1/remote/pairing', `DELETE /api/v1/remote/devices/${ID}`,
  ]);
});

test('errors carry the server error code', async () => {
  const api = createRemoteApi(capture(() => errorEnvelope(503, 'REMOTE_UNAVAILABLE')).fetcher);
  await assert.rejects(api.startPairing(), (e) => e instanceof RemoteApiError && e.code === 'REMOTE_UNAVAILABLE' && e.status === 503);
  const forbidden = createRemoteApi(capture(() => errorEnvelope(403, 'REMOTE_FORBIDDEN')).fetcher);
  await assert.rejects(forbidden.revoke(ID), (e) => e instanceof RemoteApiError && e.code === 'REMOTE_FORBIDDEN');
});

test('status rejects malformed payloads', async () => {
  const api = createRemoteApi(capture(() => envelope({ available: true, remote: false, devices: 'x' })).fetcher);
  await assert.rejects(api.status(), RemoteApiError);
});

test('remoteView picks off / desktop / tablet', () => {
  assert.deepEqual(remoteView({ available: false, remote: false }), { kind: 'off', canManage: false, devices: [] });
  const dev = { id: ID, name: 'Tab', paired_at: 1, online: false, path: null, last_seen: null };
  assert.deepEqual(remoteView({ available: true, remote: false, desktop_id: 'd', relay: 'r', devices: [dev] }), { kind: 'desktop', canManage: true, devices: [dev] });
  assert.deepEqual(remoteView({ available: true, remote: true, desktop_id: 'd', relay: 'r', devices: [dev] }), { kind: 'tablet', canManage: false, devices: [dev] });
  // A tablet never manages, even if the sidecar were to vanish mid-session.
  assert.equal(remoteView({ available: false, remote: true }).canManage, false);
});

test('formatCountdown rounds up, shows m:ss and clamps at zero', () => {
  assert.equal(formatCountdown(300000), '5:00');
  assert.equal(formatCountdown(299990), '5:00');
  assert.equal(formatCountdown(61500), '1:02');
  assert.equal(formatCountdown(1), '0:01');
  assert.equal(formatCountdown(-5), '0:00');
});
