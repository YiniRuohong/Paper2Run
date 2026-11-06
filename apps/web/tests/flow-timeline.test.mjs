import test from 'node:test';
import assert from 'node:assert/strict';
import { AI_GROUP, RUN_TITLES, SETUP_TITLES, flowStages } from '../src/features/flow/timeline.ts';

const ev = (sequence, type, payload = {}) => ({ sequence, type, payload, created_at: '2026-11-06T12:00:00Z' });
const setup = (doneIds) => ['paper', 'objective', 'model', 'launch'].map(id => ({ id, done: doneIds.includes(id), summary: `s-${id}` }));
const TITLES = ['上传论文', '实验目标', '连接模型', '确认并启动', '阅读与方案', '编写与执行', '冻结证据', '候选与复核', '报告'];

test('without a task the timeline has nine steps with setup progress and locked AI phases', () => {
  const { stages, activeId, doneCount } = flowStages({ setup: setup(['paper', 'objective']), current: 'model', task: null, events: [] });
  assert.equal(stages.length, 9);
  assert.deepEqual(stages.map(s => s.title), TITLES);
  assert.deepEqual(stages.map(s => s.mark), ['done', 'done', 'current', 'locked', 'locked', 'locked', 'locked', 'locked', 'locked']);
  assert.equal(stages[4].group, AI_GROUP);
  assert.equal(AI_GROUP, '启动后由 AI 推进');
  assert.equal(activeId, 'model');
  assert.equal(doneCount, 2);
  assert.equal(stages[3].disabled, true);
  assert.equal(stages[0].disabled, false);
  assert.equal(stages[0].summary, 's-paper');
  assert.deepEqual(stages[5].items, []);
  assert.equal(stages[5].summary, '写代码、在隔离环境运行');
  assert.equal(SETUP_TITLES.launch, '确认并启动');
  assert.equal(RUN_TITLES.report, '报告');
});

test('with a task the setup steps are done, launch holds the creation events and the AI phase is active', () => {
  const task = { id: 't', state: 'running', pending_decision: null };
  const events = [
    ev(1, 'task_created'), ev(2, 'session_ready', { session_id: 's' }),
    ev(3, 'tool_started', { id: 'a', name: 'read', arguments: { path: 'paper.md' } }), ev(4, 'tool_completed', { id: 'a', name: 'read', result: {}, is_error: false }),
    ev(5, 'tool_started', { id: 'b', name: 'execute', arguments: { command: 'python train.py' } }), ev(6, 'tool_completed', { id: 'b', name: 'execute', result: { exit_code: 1 }, is_error: true }),
    ev(7, 'tool_started', { id: 'n', name: 'note_progress', arguments: { phase: '训练', summary: '开始' } }),
    ev(8, 'progress_noted', { phase: '训练', summary: '开始' }),
  ];
  const { stages, activeId, doneCount } = flowStages({ setup: setup([]), current: null, task, events });
  assert.equal(stages.length, 9);
  assert.deepEqual(stages.slice(0, 4).map(s => s.mark), ['done', 'done', 'done', 'done']);
  assert.ok(stages[3].items.some(i => i.label === '任务已创建'));
  assert.equal(activeId, 'execute');
  assert.equal(stages[4].mark, 'done');
  assert.equal(stages[4].title, '阅读与方案');
  assert.equal(stages[4].group, AI_GROUP);
  assert.equal(doneCount, stages.filter(s => s.mark === 'done').length);
  const all = stages.flatMap(s => (s.items ?? []).flatMap(i => [i, ...(i.children ?? [])]));
  const narrative = all.filter(i => (i.detail ?? '').includes('模型叙述'));
  assert.ok(narrative.length > 0);
  for (const item of narrative) assert.notEqual(item.tone, 'ok');
});

test('a task that has only just been created keeps launch active', () => {
  const { activeId } = flowStages({ setup: setup([]), current: null, task: { id: 't', state: 'queued' }, events: [ev(1, 'task_created')] });
  assert.equal(activeId, 'launch');
});
