import test from 'node:test';
import assert from 'node:assert/strict';
import { OUTPUT_LIMIT, itemDetail, itemSequence } from '../src/features/autonomous/stepDetail.ts';
import { buildStages } from '../src/features/autonomous/stages.ts';

// The drawer shows the process behind one rail sub-step, read only from real events.

const ev = (sequence, type, payload = {}) => ({ sequence, type, payload, created_at: '2026-11-06T09:20:21Z' });
const started = (sequence, id, name, args = {}) => ev(sequence, 'tool_started', { id, name, arguments: args });
const completed = (sequence, id, name, result, isError = false) => ev(sequence, 'tool_completed', { id, name, result, is_error: isError });

test('rail item ids map to event sequences', () => {
  assert.equal(itemSequence('t12'), 12);
  assert.equal(itemSequence('e3'), 3);
  assert.equal(itemSequence('n7'), 7);
  assert.equal(itemSequence('paper'), null);
});

test('a finished command shows command, stdout, stderr and exit code', () => {
  const events = [
    started(1, 'c1', 'execute', { command: 'python train.py' }),
    completed(2, 'c1', 'execute', { state: 'exited', exit_code: 1, logs: { stdout: 'epoch 1\n', stderr: 'Traceback\n', output_truncated: false } }),
  ];
  const detail = itemDetail(events, 't1');
  assert.equal(detail.kind, 'command');
  assert.equal(detail.command, 'python train.py');
  assert.equal(detail.stdout, 'epoch 1\n');
  assert.equal(detail.stderr, 'Traceback\n');
  assert.equal(detail.exitCode, 1);
  assert.equal(detail.running, false);
  assert.equal(detail.truncated, false);
  assert.equal(detail.error, null);
});

test('a command still running has no output yet', () => {
  const detail = itemDetail([started(1, 'c1', 'execute', { command: 'sleep 9' })], 't1');
  assert.equal(detail.kind, 'command');
  assert.equal(detail.running, true);
  assert.equal(detail.stdout, null);
});

test('a failed tool reads as one sentence', () => {
  const events = [
    started(1, 'c1', 'register_artifact', { name: 'env', kind: 'environment', path: 'requirements.txt' }),
    completed(2, 'c1', 'register_artifact', { code: 'container_error', message: 'No such file' }, true),
  ];
  const detail = itemDetail(events, 't1');
  assert.equal(detail.kind, 'tool');
  assert.equal(detail.error, 'No such file');
  assert.equal(detail.running, false);
});

test('long output keeps the tail and says it was cut', () => {
  const long = 'x'.repeat(OUTPUT_LIMIT) + 'END';
  const events = [started(1, 'c1', 'execute', { command: 'yes' }), completed(2, 'c1', 'execute', { exit_code: 0, logs: { stdout: long } })];
  const detail = itemDetail(events, 't1');
  assert.equal(detail.stdout.length, OUTPUT_LIMIT);
  assert.ok(detail.stdout.endsWith('END'));
  assert.equal(detail.truncated, true);
});

test('a progress note opens as the note, whether the rail item is the tool or the event', () => {
  const events = [
    started(1, 'n', 'note_progress', { phase: '环境检查', summary: '检查 numpy' }),
    completed(2, 'n', 'note_progress', { recorded: true }),
    ev(3, 'progress_noted', { phase: '环境检查', summary: '检查 numpy' }),
  ];
  for (const id of ['t1', 'n3']) {
    const detail = itemDetail(events, id);
    assert.equal(detail.kind, 'note', id);
    assert.equal(detail.summary, '检查 numpy');
  }
});

test('a frozen artifact carries the path the file tree uses', () => {
  const events = [ev(5, 'artifact_frozen', { name: 'm.json', kind: 'result', sha256: 'ab'.repeat(32), bytes: 10, protocol_sha256: 'abcdef0123456789', round: 1, source_path: 'out/m.json' })];
  const detail = itemDetail(events, 'e5');
  assert.equal(detail.kind, 'artifact');
  assert.equal(detail.path, 'artifacts/abcdef012345/r1/m.json');
  assert.equal(detail.sourcePath, 'out/m.json');
});

test('other events show their label and payload; unknown ids give nothing', () => {
  const events = [ev(9, 'budget_exhausted', { reason: 'calls' })];
  const detail = itemDetail(events, 'e9');
  assert.equal(detail.kind, 'event');
  assert.equal(detail.label, '预算耗尽');
  assert.equal(itemDetail(events, 'e99'), null);
  assert.equal(itemDetail(events, 'launch'), null);
});

test('every rail sub-step the stages builder makes resolves to a detail', () => {
  const events = [
    started(1, 'c1', 'execute', { command: 'ls' }), completed(2, 'c1', 'execute', { exit_code: 0, logs: { stdout: '' } }),
    ev(3, 'artifact_frozen', { name: 'm.json', kind: 'result', protocol_sha256: 'abcdef0123456789', round: 1 }),
    ev(4, 'budget_exhausted', { reason: 'calls' }),
  ];
  const { stages } = buildStages({ id: 't', state: 'running' }, events);
  const walk = items => items.flatMap(item => [item, ...walk(item.children ?? [])]);
  const ids = stages.flatMap(stage => walk(stage.items ?? [])).map(item => item.id);
  assert.ok(ids.length >= 3);
  for (const id of ids) assert.ok(itemDetail(events, id), id);
});

const fig = (path, source, sha) => ({ path, source, bytes: 10, media_type: 'image/png', modified_at: null, sha256: sha, kind: null, protocol_sha256: null });

test('once the agent froze figures, only those key figures are shown', async () => {
  const { pickFigures } = await import('../src/features/autonomous/figures.ts');
  const picked = pickFigures([fig('outputs/a.png', 'workspace', null), fig('artifacts/x/r0/a.png', 'artifact', 's1'), fig('outputs/b.png', 'workspace', null)]);
  assert.deepEqual(picked.map(p => p.path), ['artifacts/x/r0/a.png']);
});

test('before any figure is frozen, dependency and hidden folders never show images', async () => {
  const { pickFigures } = await import('../src/features/autonomous/figures.ts');
  const picked = pickFigures([
    fig('vendor/fastText/website/static/img/logo.png', 'workspace', null),
    fig('.venv/lib/python3.12/site-packages/matplotlib/mpl-data/images/home.png', 'workspace', null),
    fig('node_modules/pkg/icon.svg', 'workspace', null),
    fig('.cache/thumb.png', 'workspace', null),
    fig('third_party/x/doc.png', 'workspace', null),
    fig('results/accuracy.png', 'workspace', null),
    fig('figure_1.png', 'workspace', null),
  ]);
  assert.deepEqual(picked.map(p => p.path), ['figure_1.png', 'results/accuracy.png']);
});
