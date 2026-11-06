import test from 'node:test';
import assert from 'node:assert/strict';
import { frozenMainProfile, recoveryProfileRequest, recoveryState } from '../src/features/autonomous/recovery.ts';
import { errorExplanation } from '../src/features/autonomous/config.ts';

// Shape written by service.create_task: ProfileStore.snapshot → public(), so no api_key.
const frozen = { id: 'main', base_url: 'https://llm.example/v1', model: 'm-1', api: 'openai-completions', auth_mode: 'api-key',
  credential_ref: null, context_window: 64000, max_tokens: 2048, secret_configured: true };
const task = (over = {}) => ({ id: 't', state: 'paused', outcome: null, config_snapshot: { model: { provider_profile: 'main', credential_ref: 'profile:main' }, model_profile: frozen }, ...over });
const event = (sequence, type, payload = {}) => ({ sequence, type, payload, created_at: '' });

test('without a live profile the key is re-saved under the frozen identity', () => {
  const request = recoveryProfileRequest(null, task().config_snapshot, ' sk-new ');
  assert.deepEqual(request, { id: 'main', base_url: 'https://llm.example/v1', model: 'm-1', api: 'openai-completions', auth_mode: 'api-key',
    context_window: 64000, max_tokens: 2048, api_key: 'sk-new' });
  assert.equal('secret_configured' in request, false, 'public-only fields are not sent back');
});

test('a live profile still wins when it exists', () => {
  const live = { id: 'main', base_url: 'https://llm.example/v1', model: 'm-1', api: 'openai-completions', auth_mode: 'api-key', secret_configured: false };
  assert.deepEqual(recoveryProfileRequest(live, task().config_snapshot, 'k'), { ...live, api_key: 'k' });
});

test('no request without a key or without a usable frozen profile', () => {
  assert.equal(recoveryProfileRequest(null, task().config_snapshot, '   '), null);
  assert.equal(recoveryProfileRequest(null, { model: { provider_profile: 'main' } }, 'k'), null, 'old snapshot without model_profile');
  assert.equal(recoveryProfileRequest(null, { model_profile: { ...frozen, api: 'other' } }, 'k'), null);
  assert.equal(recoveryProfileRequest(null, { model_profile: { ...frozen, base_url: '' } }, 'k'), null);
  assert.equal(frozenMainProfile({ model_profile: { ...frozen, auth_mode: 'none' } })?.auth_mode, 'none');
});

test('recovery_required events still drive the card until resume or finish', () => {
  const base = { profiles: [{ ...frozen }], profilesLoaded: true, lastControlError: null, keysInjected: false };
  assert.equal(recoveryState(task(), [event(1, 'recovery_required')], base).needed, true);
  assert.equal(recoveryState(task(), [event(1, 'recovery_required'), event(2, 'task_resumed')], base).needed, false);
  assert.deepEqual(recoveryState(task(), [event(1, 'recovery_required'), event(2, 'application_exit', { background_jobs_retained: ['j1'] })], base).retainedJobs, ['j1']);
  assert.equal(recoveryState(task(), [event(1, 'recovery_required')], { ...base, keysInjected: true }).needed, false);
});

test('a task paused before the restart needs recovery when its main profile is gone', () => {
  const base = { profiles: [], profilesLoaded: true, lastControlError: null, keysInjected: false };
  assert.equal(recoveryState(task(), [], base).needed, true, 'no recovery_required event was written');
  assert.equal(recoveryState(task(), [], { ...base, profilesLoaded: false }).needed, false, 'not before profiles are read');
  assert.equal(recoveryState(task(), [], { ...base, profiles: [{ ...frozen, secret_configured: false }] }).needed, true, 'present without a key');
  assert.equal(recoveryState(task(), [], { ...base, profiles: [{ ...frozen }] }).needed, false);
  assert.equal(recoveryState(task({ state: 'running' }), [], base).needed, false, 'only paused tasks');
  const anonymous = task({ config_snapshot: { model: { provider_profile: 'main' }, model_profile: { ...frozen, auth_mode: 'none' } } });
  assert.equal(recoveryState(anonymous, [], base).needed, false, 'anonymous loopback profiles have no key to lose');
});

test('a CREDENTIAL_REQUIRED control error shows the card even with no events', () => {
  const base = { profiles: [{ ...frozen }], profilesLoaded: true, lastControlError: 'CREDENTIAL_REQUIRED', keysInjected: false };
  assert.equal(recoveryState(task(), [], base).needed, true);
  assert.equal(recoveryState(task(), [], { ...base, lastControlError: 'TASK_BUSY' }).needed, false);
});

test('finished tasks never ask for recovery', () => {
  const base = { profiles: [], profilesLoaded: true, lastControlError: 'CREDENTIAL_REQUIRED', keysInjected: false };
  assert.equal(recoveryState(task({ state: 'stopped' }), [event(1, 'recovery_required')], base).needed, false);
  assert.equal(recoveryState(task({ outcome: 'verified' }), [event(1, 'recovery_required')], base).needed, false);
});

test('CREDENTIAL_REQUIRED explains where to re-enter the key', () => {
  assert.match(errorExplanation('CREDENTIAL_REQUIRED'), /重启后需要重新输入/);
  assert.match(errorExplanation('CREDENTIAL_REQUIRED'), /需要恢复/);
});
