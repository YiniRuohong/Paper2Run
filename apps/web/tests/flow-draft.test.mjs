import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api-client/client.ts';
import { defaultConfig } from '../src/features/autonomous/config.ts';
import * as draft from '../src/features/flow/draft.ts';
import { budgetLine, draftProgress, mergeConfig, newestTaskId, objectiveError, pickPaper, profileIdFor, saveConfigWithRetry } from '../src/features/flow/draft.ts';

const fresh = { paper: null, description: '', config: null, profiles: [], taskCount: 0 };
const paper = over => ({ paperId: 'pa', versionId: 'v1', title: 't', pdf: false, textStatus: 'none', pageCount: null, ...over });
const cfgWith = (profile, objective = '') => {
  const c = defaultConfig('p');
  c.model = { provider_profile: profile, credential_ref: `profile:${profile}` };
  c.experiment.objective = objective;
  return c;
};

test('fresh draft starts at paper', () => {
  const p = draftProgress(fresh);
  assert.equal(p.current, 'paper');
  assert.equal(p.connectionLost, false);
});

test('PDF still parsing blocks the paper step; pasted text does not', () => {
  assert.equal(draftProgress({ ...fresh, paper: paper({ pdf: true, textStatus: 'parsing' }) }).current, 'paper');
  assert.equal(draftProgress({ ...fresh, paper: paper({ pdf: true, textStatus: 'ready' }) }).done.paper, true);
  assert.equal(draftProgress({ ...fresh, paper: paper({ pdf: false, textStatus: 'none' }) }).done.paper, true);
});

test('description alone completes objective', () => {
  const p = draftProgress({ ...fresh, paper: paper(), description: '复现表 2' });
  assert.equal(p.current, 'model');
  assert.equal(p.objective, '复现表 2');
});

test('missing connection is flagged as lost', () => {
  const p = draftProgress({ ...fresh, paper: paper(), config: cfgWith('model-abc', 'goal'), profiles: [] });
  assert.equal(p.current, 'model');
  assert.equal(p.connectionLost, true);
  assert.equal(p.done.paper, true);
  assert.equal(p.done.objective, true);
});

test('configured connection moves to launch; a task finishes it', () => {
  const profiles = [{ id: 'model-abc', base_url: 'http://x', model: 'm', api: 'openai-completions', secret_configured: true }];
  const facts = { ...fresh, paper: paper(), config: cfgWith('model-abc', 'goal'), profiles };
  assert.equal(draftProgress(facts).current, 'launch');
  assert.equal(draftProgress(facts).done.launch, false);
  assert.equal(draftProgress({ ...facts, taskCount: 1 }).done.launch, true);
  const none = [{ id: 'model-abc', base_url: 'http://localhost', model: 'm', api: 'openai-completions', auth_mode: 'none' }];
  assert.equal(draftProgress({ ...facts, profiles: none }).done.model, true);
  const noSecret = [{ id: 'model-abc', base_url: 'http://x', model: 'm', api: 'openai-completions' }];
  assert.equal(draftProgress({ ...facts, profiles: noSecret }).done.model, false);
});

test('mergeConfig preserves untouched fields and does not mutate saved', () => {
  const v3 = defaultConfig('p');
  v3.specialists = [{ id: 's', description: 'd', task_types: ['coding'], model: { provider_profile: 'a', credential_ref: 'b' } }];
  v3.harness.skills = ['s1'];
  v3.limits.wall_time_seconds = 600;
  const snapshot = JSON.stringify(v3);
  const merged = mergeConfig(v3, 'p', { objective: '新' });
  assert.equal(merged.experiment.objective, '新');
  assert.equal(merged.specialists.length, 1);
  assert.deepEqual(merged.harness.skills, ['s1']);
  assert.equal(merged.limits.wall_time_seconds, 600);
  assert.equal(JSON.stringify(v3), snapshot);
  const model = { provider_profile: 'm', credential_ref: 'profile:m' };
  assert.deepEqual(mergeConfig(null, 'q', { model }).model, model);
  assert.equal(mergeConfig(null, 'q', {}).project.id, 'q');
});

test('profileIdFor', () => {
  const id = profileIdFor('3f2a-9c11-4b7e-8d00-aaaaaaaaaaaa', null);
  assert.match(id, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/);
  assert.equal(profileIdFor('p', cfgWith('keep-me')), 'keep-me');
});

test('objectiveError', () => {
  assert.equal(objectiveError(''), '请填写实验目标');
  assert.equal(objectiveError('   '), '请填写实验目标');
  assert.equal(objectiveError('x'.repeat(2001)), '实验目标最多 2000 字');
  assert.equal(objectiveError('ok'), '');
});

test('budgetLine', () => {
  assert.equal(budgetLine(defaultConfig('p').limits), '30 分钟 · 30 次模型调用 · 10 万 tokens · 3 轮修复');
  const l = { ...defaultConfig('p').limits, max_model_tokens: 123456, max_repair_rounds: undefined };
  assert.equal(budgetLine(l), '30 分钟 · 30 次模型调用 · 123456 tokens · 3 轮修复');
});

test('newestTaskId and pickPaper', () => {
  assert.equal(newestTaskId([]), null);
  assert.equal(newestTaskId([
    { id: 'a', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' },
    { id: 'b', created_at: '2026-01-03T00:00:00Z', updated_at: '' },
    { id: 'c', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-02T00:00:00Z' },
  ]), 'b');
  assert.equal(pickPaper([]), null);
  assert.equal(pickPaper([paper({ versionId: '' })]), null);
  assert.equal(pickPaper([paper({ paperId: 'old', versionId: 'v0' }), paper({ paperId: 'new', versionId: 'v1' })]).paperId, 'new');
});

test('saveConfigWithRetry', async () => {
  const conflict = () => new ApiError('x', 409, 'REVISION_CONFLICT');
  const cfg = defaultConfig('p');
  let reads = 0;
  const read = async () => ({ version: ++reads, config: cfg });
  const apply = saved => saved ?? cfg;
  let calls = 0;
  const result = await saveConfigWithRetry(read, async (c, v) => {
    calls += 1;
    if (calls === 1) { assert.equal(v, 1); throw conflict(); }
    assert.equal(v, 2);
    return { version: 9, config: c };
  }, apply);
  assert.equal(result.version, 9);
  assert.equal(reads, 2);

  reads = 0;
  await assert.rejects(saveConfigWithRetry(read, async () => { throw conflict(); }, apply), ApiError);
  assert.equal(reads, 2);

  reads = 0;
  await assert.rejects(saveConfigWithRetry(read, async () => { throw new ApiError('x', 400, 'BAD'); }, apply), error => error.code === 'BAD');
  assert.equal(reads, 1);

  const first = await saveConfigWithRetry(async () => null, async (c, v) => { assert.equal(v, undefined); return { version: 1, config: c }; }, saved => { assert.equal(saved, null); return cfg; });
  assert.equal(first.version, 1);
});

test('legacy placeholder descriptions are not an objective', () => {
  assert.deepEqual([...draft.LEGACY_PLACEHOLDER_DESCRIPTIONS].sort(), ['Autonomous experiment flow', 'Guided real article flow']);
  for (const placeholder of ['Autonomous experiment flow', '  Guided real article flow \n']) {
    assert.equal(draft.userDescription(placeholder), '');
    const p = draftProgress({ ...fresh, paper: paper(), description: placeholder });
    assert.equal(p.objective, '', 'the placeholder never pre-fills the objective input');
    assert.equal(p.done.objective, false);
    assert.equal(p.current, 'objective');
  }
  assert.equal(draft.userDescription(' 复现表 2 '), '复现表 2');
  assert.equal(draft.userDescription('Autonomous experiment flow, CIFAR'), 'Autonomous experiment flow, CIFAR');
  assert.equal(draftProgress({ ...fresh, paper: paper(), description: 'Autonomous experiment flow', config: cfgWith('', 'real goal') }).objective, 'real goal');
});

const usable = id => ({ id, base_url: 'https://api.example.com/v1', model: 'qwen-max', api: 'openai-completions', secret_configured: true });

test('usableConnection is the one rule for a connection this server process can use', () => {
  assert.equal(draft.usableConnection(usable('a')), true);
  assert.equal(draft.usableConnection({ ...usable('a'), secret_configured: false, auth_mode: 'none' }), true);
  assert.equal(draft.usableConnection({ ...usable('a'), secret_configured: false }), false);
  assert.equal(draft.usableConnection({ ...usable('a'), secret_configured: undefined }), false);
  assert.equal(draft.connectionLabel(usable('a')), 'api.example.com · qwen-max');
});

test('missingSpecialistConnections lists hidden v3 specialists whose connection is not usable', () => {
  const c = cfgWith('main', 'goal');
  assert.deepEqual(draft.missingSpecialistConnections(c, [usable('main')]), []);
  assert.deepEqual(draft.missingSpecialistConnections(null, []), []);
  const spec = (id, profile) => ({ id, description: 'd', task_types: ['coding'], model: { provider_profile: profile, credential_ref: `profile:${profile}` } });
  c.specialists = [spec('s1', 'ok'), spec('s2', 'gone'), spec('s3', 'nokey'), spec('s4', 'gone')];
  const profiles = [usable('main'), usable('ok'), { ...usable('nokey'), secret_configured: false }];
  assert.deepEqual(draft.missingSpecialistConnections(c, profiles), ['gone', 'nokey']);
  assert.equal(draft.legacyLaunchBlock(c, profiles), '旧配置中的科研专家模型连接已失效，无法启动（应用重启后需重新配置）');
  assert.equal(draft.legacyLaunchBlock(c, [usable('main'), usable('ok'), usable('gone'), usable('nokey')]), '');
});

test('legacyConfigLine names only the hidden v3 parts that exist', () => {
  const c = cfgWith('main', 'goal');
  assert.equal(draft.legacyConfigLine(c), '');
  assert.equal(draft.legacyConfigLine(null), '');
  c.harness.skills = ['a', 'b'];
  assert.equal(draft.legacyConfigLine(c), '沿用旧配置：2 个 Skills');
  c.specialists = [{ id: 's', description: 'd', task_types: ['coding'], model: { provider_profile: 'x', credential_ref: 'profile:x' } }];
  assert.equal(draft.legacyConfigLine(c), '沿用旧配置：1 位科研专家 · 2 个 Skills');
  c.harness.skills = [];
  assert.equal(draft.legacyConfigLine(c), '沿用旧配置：1 位科研专家');
});

test('refreshOnFailure re-reads the server after a failed save and keeps the error', async () => {
  let refreshed = 0;
  assert.equal(await draft.refreshOnFailure(async () => 7, async () => { refreshed += 1; }), 7);
  assert.equal(refreshed, 0);
  const boom = new Error('config save failed');
  await assert.rejects(draft.refreshOnFailure(async () => { throw boom; }, async () => { refreshed += 1; }), error => error === boom);
  assert.equal(refreshed, 1);
  await assert.rejects(draft.refreshOnFailure(async () => { throw boom; }, async () => { throw new Error('reload failed'); }), error => error === boom, 'a failed refresh never hides the save error');
});
