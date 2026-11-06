import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ancestors, buildTree, countFolders, filterFiles, formatBytes, parseCsv, previewKind } from '../src/ui/workbench/tree.ts';
import { artifactPath, buildStages, toolLabel, toWbFiles } from '../src/features/autonomous/stages.ts';

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

test('the task page keeps live controls as workbench children and releases blob previews', async () => {
  const page = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /<Workbench\b/);
  assert.doesNotMatch(page, /live=\{/, 'live view must be children, not a prop');
  const wb = await readFile(new URL('../src/ui/workbench/Workbench.tsx', import.meta.url), 'utf8');
  assert.match(wb, /URL\.revokeObjectURL/, 'blob previews are released');
  assert.doesNotMatch(wb, /dangerouslySetInnerHTML|localStorage/);
});

test('workbench supports an app layout with rail head, group separators and expandable items', async () => {
  const wb = await readFile(new URL('../src/ui/workbench/Workbench.tsx', import.meta.url), 'utf8');
  assert.match(wb, /railHead\?: ReactNode/);
  assert.match(wb, /wb-rail-head/);
  assert.match(wb, /is-app/);
  assert.match(wb, /className="wb-group"[^>]*role="presentation"|role="presentation"[^>]*className="wb-group"/);
  assert.match(wb, /aria-expanded=\{childOpen\}/, 'items with children expand via aria-expanded');
  const css = await readFile(new URL('../src/ui/workbench/workbench.css', import.meta.url), 'utf8');
  assert.match(css, /\.wb\.is-app\s*\{[^}]*"rail head head"\s*"rail viewer files"/);
  assert.match(css, /\[data-shell="darwin"\] \.wb\.is-app \.wb-rail-head/);
  assert.match(css, /82px/);
});

test('errorDetail lives in errors.ts and the test helper re-exports it', async () => {
  const errors = await readFile(new URL('../src/features/autonomous/errors.ts', import.meta.url), 'utf8');
  assert.match(errors, /export const errorDetail/);
  const helper = await import('./helpers/error-detail.mjs');
  assert.equal(typeof helper.errorDetail, 'function');
  const page = await readFile(new URL('../src/features/flow/DraftPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /from '\.\.\/autonomous\/errors'/);
});

test('app layout keeps the rail spanning every row and its head pinned', async () => {
  const css = await readFile(new URL('../src/ui/workbench/workbench.css', import.meta.url), 'utf8');
  const block = (re) => { const m = css.match(re); assert.ok(m, `block ${re} exists`); return m[0]; };
  const media1240 = block(/@media \(max-width: 1240px\) \{[\s\S]*?\n\}/);
  const appRail = media1240.match(/\.wb\.is-app \.wb-rail\s*\{[^}]*\}/);
  assert.ok(appRail, '1240 block styles the app rail');
  assert.doesNotMatch(appRail[0], /grid-row/, 'the rail must keep spanning rows through grid-area');
  const head = block(/\.wb\.is-app \.wb-rail-head\s*\{[^}]*\}/);
  assert.match(head, /position:\s*sticky/);
  assert.match(head, /top:\s*0/);
  assert.match(head, /background:/);
  assert.doesNotMatch(css.replace(/\.wb\.is-app \.wb-rail-head\s*\{[^}]*\}/, ''), /^\.wb-rail-head\s*\{[^}]*sticky/m, 'plain layout stays unchanged');
});

test('task page continues the nine-step timeline in the full-bleed layout', async () => {
  const page = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /flowStages\(/);
  assert.match(page, /railHead=/);
  assert.match(page, /<RailHead\b/);
  assert.doesNotMatch(page, /auto-taskbar/);
  assert.doesNotMatch(page, /项目任务/);
  assert.doesNotMatch(page, /export \{ errorDetail \}/);
  assert.match(page, /auto-live-status/);
  assert.match(page, /title="详细记录"/);
  assert.match(page, /进度 · \$\{doneCount\} \/ 9/);
});
