import test from 'node:test';
import assert from 'node:assert/strict';
import { finishUpload, carryNotice, peekNotice, clearNotice } from '../src/features/flow/draftUpload.ts';

test('a parse that fails to start still lands on the draft and reports the error once', async () => {
  const calls = [];
  const boom = new Error('parse rejected');
  const failure = await finishUpload({
    startParse: async () => { calls.push('startParse'); throw boom; },
    accepted: async f => { calls.push('accepted'); assert.equal(f, boom); },
  });
  assert.deepEqual(calls, ['startParse', 'accepted']);
  assert.equal(failure, boom);
});

test('a parse that starts reports no failure', async () => {
  const calls = [];
  const failure = await finishUpload({ startParse: async () => { calls.push('s'); }, accepted: async () => { calls.push('a'); } });
  assert.deepEqual(calls, ['s', 'a']);
  assert.equal(failure, undefined);
});

test('a notice survives until it is cleared, per project', () => {
  carryNotice('p1', 'msg');
  assert.equal(peekNotice('p1'), 'msg');
  assert.equal(peekNotice('p1'), 'msg');
  assert.equal(peekNotice('p2'), '');
  clearNotice('p1');
  assert.equal(peekNotice('p1'), '');
});

test('DraftPage uploads once, lands on the draft, and carries the parse error across the route change', async () => {
  const { readFile } = await import('node:fs/promises');
  const src = await readFile(new URL('../src/features/flow/DraftPage.tsx', import.meta.url), 'utf8');
  assert.equal(src.match(/uploadPaperPdf\(/g).length, 1);
  assert.match(src, /finishUpload\(/);
  assert.match(src, /carryNotice\(/);
  assert.doesNotMatch(src, /await startParse\([^)]*\);\s*\n\s*await accepted/);
});

test('a draft that fails to load shows the error with 重新读取, never an empty step form', async () => {
  const { readFile } = await import('node:fs/promises');
  const src = await readFile(new URL('../src/features/flow/DraftPage.tsx', import.meta.url), 'utf8');
  assert.match(src, /const loadFailed = !!projectId && !data && !loading;/);
  assert.match(src, /if \(loadFailed\)/, 'no step form is chosen while the draft is unread');
  assert.match(src, /loadFailed \? <div[^]*?role="alert"[^]*?\{error \|\| '正在重新读取…'\}[^]*?onClick=\{retryLoad\}[^]*?重新读取/);
  assert.match(src, /\.catch\(e => \{ if \(alive\) setError\(errorDetail\(e\)\); \}\)/, 'the first failure is shown through errorDetail');
  assert.match(src, /const retryLoad = \(\) => void act\(async \(\) => \{ const next = await reload\(\);/, '重新读取 calls reload() through act, which shows a second failure via errorDetail');
  assert.match(src, /files = useMemo<WbFile\[\] \| null>\(\(\) => projectId && !data \? \(loading \? null : \[\]\)/, 'the file pane stops loading once the read failed');
});

test('deleting a draft replaces the history entry so back does not reopen it', async () => {
  const { readFile } = await import('node:fs/promises');
  const src = await readFile(new URL('../src/features/flow/DraftPage.tsx', import.meta.url), 'utf8');
  assert.match(src, /deleteProject\(projectId\); window\.location\.replace\('#\/'\)/);
  assert.doesNotMatch(src, /window\.location\.hash = '#\/'/);
});
