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
