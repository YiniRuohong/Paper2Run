import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ancestors, buildTree, countFolders, filterFiles, formatBytes, parseCsv, previewKind } from '../src/ui/workbench/tree.ts';
import { artifactPath, buildStages, toolLabel, toWbFiles } from '../src/features/autonomous/stages.ts';
import { createRealFlowState } from '../src/features/guided/realflow-state.ts';
import { flowRecords, FLOW_PATHS } from '../src/features/guided/realflow-files.ts';

const f = (path, bytes = 1, mediaType = null) => ({ path, bytes, mediaType });

test('buildTree nests folders first, counts files, sorts naturally', () => {
  const tree = buildTree([f('b/fig_10.png'), f('b/fig_2.png'), f('a.txt'), f('b/sub/x.json')]);
  assert.deepEqual(tree.children.map(n => `${n.kind}:${n.name}`), ['folder:b', 'file:a.txt']);
  const b = tree.children[0];
  assert.equal(b.fileCount, 3);
  assert.deepEqual(b.children.map(n => n.name), ['sub', 'fig_2.png', 'fig_10.png']);
  assert.equal(countFolders(tree), 2);
});

test('filterFiles matches every term against the full path', () => {
  const files = [f('output/figures/loss.svg'), f('output/models/w.bin'), f('logs/train.log')];
  assert.deepEqual(filterFiles(files, 'output fig').map(x => x.path), ['output/figures/loss.svg']);
  assert.equal(filterFiles(files, '  ').length, 3);
  assert.deepEqual(ancestors('a/b/c.txt'), ['a', 'a/b']);
});

test('previewKind picks a safe renderer by extension and media type', () => {
  assert.equal(previewKind(f('x/fig.svg', 1, 'text/plain')), 'image');
  assert.equal(previewKind(f('paper.pdf')), 'pdf');
  assert.equal(previewKind(f('r.md')), 'markdown');
  assert.equal(previewKind(f('m.csv')), 'csv');
  assert.equal(previewKind(f('blob', 1, 'application/json')), 'json');
  assert.equal(previewKind(f('train.py')), 'text');
  assert.equal(previewKind(f('w.bin', 1, 'application/octet-stream')), 'binary');
  assert.equal(formatBytes(1536), '1.5 KB');
  assert.equal(formatBytes(null), '—');
});

test('parseCsv handles quotes and bounds rows', () => {
  const { rows } = parseCsv('a,b\n"x, y","he said ""hi"""\n');
  assert.deepEqual(rows, [['a', 'b'], ['x, y', 'he said "hi"']]);
  const big = parseCsv(Array.from({ length: 10 }, (_, i) => String(i)).join('\n'), 3);
  assert.equal(big.rows.length, 3); assert.equal(big.truncated, true);
});

const ev = (sequence, type, payload = {}) => ({ sequence, type, payload, created_at: '2026-11-03T12:00:00Z' });

test('autonomous stages follow the real event journal and colour tool results', () => {
  const task = { id: 't', state: 'running', pending_decision: null };
  const events = [
    ev(1, 'task_created'), ev(2, 'session_ready', { session_id: 's' }),
    ev(3, 'tool_started', { id: 'a', name: 'read', arguments: { path: 'paper.md' } }), ev(4, 'tool_completed', { id: 'a', name: 'read', result: {}, is_error: false }),
    ev(5, 'tool_started', { id: 'b', name: 'execute', arguments: { command: 'python train.py' } }), ev(6, 'tool_completed', { id: 'b', name: 'execute', result: { exit_code: 1 }, is_error: true }),
  ];
  const { stages, active } = buildStages(task, events);
  assert.equal(active, 'execute');
  const byId = Object.fromEntries(stages.map(s => [s.id, s]));
  assert.equal(byId.setup.mark, 'done');
  assert.equal(byId.plan.mark, 'done');
  assert.equal(byId.execute.mark, 'running');
  assert.equal(byId.evidence.mark, 'locked');
  assert.equal(byId.execute.items[0].label, '$ python train.py');
  assert.equal(byId.execute.items[0].tone, 'bad');
  assert.match(byId.execute.items[0].detail, /exit 1/);
  assert.equal(byId.plan.items[0].tone, 'ok');
});

test('a paused task waiting for approval is marked attention, never done', () => {
  const task = { id: 't', state: 'paused', pending_decision: { kind: 'protocol_approval' } };
  const { stages } = buildStages(task, [ev(1, 'task_created'), ev(2, 'protocol_approval_required', { reason: 'r' })]);
  assert.equal(stages.find(s => s.id === 'plan').mark, 'attention');
});

test('frozen artifacts link to the listing path the backend serves', () => {
  const { stages } = buildStages({ id: 't', state: 'completed' }, [ev(1, 'artifact_frozen', { name: 'out/metrics.json', kind: 'result', bytes: 9, protocol_sha256: 'abcdef0123456789' })]);
  assert.equal(stages.find(s => s.id === 'evidence').items[0].openPath, 'artifacts/abcdef012345/out/metrics.json');
  assert.equal(artifactPath('x', null), 'artifacts/unversioned/x');
  assert.equal(toolLabel('write', { path: 'a.py' }), '写入 a.py');
  assert.deepEqual(toWbFiles([{ path: 'artifacts/u/x', source: 'artifact', bytes: 1, media_type: 'text/plain', sha256: 'h' }])[0].badge, '证据');
});

test('real-flow records only contain what the server returned', () => {
  const empty = flowRecords(createRealFlowState(), null, []);
  assert.equal(empty.files.length, 0);
  const s = createRealFlowState();
  s.paper = { paper_id: 'p1', project_id: 'pr', title: 'T', status: 'ready', sha256: 'a'.repeat(64), page_count: 3, content_available: true, current_extraction_id: null, active_operation_id: null, error: null, created_at: '2026-11-03T00:00:00Z' };
  s.codegen = { attemptId: 'at', attemptIndex: 0, code: 'print(1)\n', codeSha256: 'c'.repeat(64), notes: null };
  const exp = [{ id: 'x', run_id: 'r', status: 'succeeded', operation_id: null, report_hash: null, created_at: '', items: [
    { format: 'report_json', status: 'ready', artifact_id: 'd'.repeat(64), filename: 'report.json', bytes: 10, media_type: 'application/json', sha256: 'd'.repeat(64), error: null },
    { format: 'report_pdf', status: 'pending', artifact_id: null, filename: null, bytes: null, media_type: null, sha256: null, error: null },
  ] }];
  const r = flowRecords(s, null, exp);
  assert.ok(r.text.has(FLOW_PATHS.paper));
  assert.equal(r.text.get('代码/attempt-0/reproduce.py'), 'print(1)\n');
  assert.equal(r.artifacts.get('导出/report.json'), 'd'.repeat(64));
  assert.equal(r.files.filter(x => x.path.startsWith('导出/')).length, 1, 'pending exports are not listed');
  assert.ok(!JSON.stringify([...r.text.values()]).includes('api_key'));
});

test('the three task surfaces share the workbench and keep live controls as children', async () => {
  for (const file of ['../src/features/guided/RealFlowPanel.tsx', '../src/features/dashboard/TaskDetail.tsx', '../src/features/autonomous/AutonomousPage.tsx']) {
    const src = await readFile(new URL(file, import.meta.url), 'utf8');
    assert.match(src, /<Workbench\b/, file);
    assert.doesNotMatch(src, /live=\{/, `${file}: live view must be children, not a prop`);
  }
  const wb = await readFile(new URL('../src/ui/workbench/Workbench.tsx', import.meta.url), 'utf8');
  assert.match(wb, /URL\.revokeObjectURL/, 'blob previews are released');
  assert.doesNotMatch(wb, /dangerouslySetInnerHTML|localStorage/);
});
