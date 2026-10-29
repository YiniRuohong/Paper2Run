import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const source = await readFile(new URL('../src/features/dashboard/Dashboard.tsx', import.meta.url), 'utf8');
const page = await readFile(new URL('../src/features/guided/GuidedPage.tsx', import.meta.url), 'utf8');

test('dashboard is read-only: GET readers only, no write or persistence calls', () => {
  assert.match(source, /api\.readArray\(/);
  assert.doesNotMatch(source, /api\.write\(|createProject|uploadPaper|createTextPaper|createExtraction|createCodegen|download\(/);
  assert.doesNotMatch(source, /localStorage\.|sessionStorage\./);
});

test('shell routes between overview and the guided new-task flow', () => {
  assert.match(page, /<Dashboard api=\{api\} onNew=/);
  assert.match(page, /<RealFlowPanel api=\{api\} \/>/);
  assert.match(page, /#\/new/);
  assert.doesNotMatch(page, /离线教学/);
});
