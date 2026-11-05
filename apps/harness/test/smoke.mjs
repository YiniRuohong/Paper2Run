// Opt-in external-model verification. Credentials enter the child only on stdin.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { bridge, config } from './helpers.mjs';
const enabled = ['P2R_TEST_API_KEY', 'P2R_TEST_BASE_URL', 'P2R_TEST_MODEL'].every(k => process.env[k]);
test('real model repairs a real file error, reopens without replay, and aborts a pending tool', { skip: !enabled, timeout: 240000 }, async () => {
  // Explicit project-owned output location; retained so the parent can inspect evidence.
  const root = resolve(process.env.P2R_TEST_OUTPUT_DIR ?? 'test-artifacts');
  const { mkdir } = await import('node:fs/promises'); await mkdir(root, { recursive: true });
  const cwd = await mkdtemp(join(root, 'smoke-'));
  await writeFile(join(cwd, 'value.txt'), 'broken');
  const c = config(cwd, process.env.P2R_TEST_BASE_URL);
  Object.assign(c.model, { api: process.env.P2R_TEST_API ?? 'anthropic-messages', api_key: process.env.P2R_TEST_API_KEY, id: process.env.P2R_TEST_MODEL, max_tokens: 1024 });
  c.limits = { max_model_calls: 8, max_model_tokens: 32000, wall_time_seconds: 120 };
  c.tools = [
    { name: 'check', description: 'Read value.txt; returns a real error until repaired.', parameters: { type: 'object', properties: {}, additionalProperties: false } },
    { name: 'repair', description: 'Replace value.txt contents with the provided text.', parameters: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'], additionalProperties: false } },
  ];
  let failed = false, repaired = false, checked = false;
  const b = bridge();
  b.send({ type: 'start', config: c, prompt: 'Call check first. If it errors, repair value.txt to exactly fixed, check again, then answer FIXED. Do not skip tools.' });
  const ready = await b.wait(m => m.type === 'ready', 30000);
  let cursor = 0;
  while (true) {
    const m = await b.wait(m => b.messages.indexOf(m) >= cursor && ['tool_request', 'error', 'result'].includes(m.type), 150000);
    cursor = b.messages.indexOf(m) + 1;
    if (m.type === 'error') assert.fail(JSON.stringify(m));
    if (m.type === 'result') { assert.ok(m.usage.calls >= 3); break; }
    if (m.name === 'check') {
      const value = await readFile(join(cwd, 'value.txt'), 'utf8');
      if (value !== 'fixed') { failed = true; b.send({ type: 'tool_result', id: m.id, result: `AssertionError: expected fixed; actual ${value}`, is_error: true }); }
      else { checked = true; b.send({ type: 'tool_result', id: m.id, result: 'PASS: value.txt == fixed' }); }
    } else {
      await writeFile(join(cwd, 'value.txt'), m.arguments.text); repaired = true;
      b.send({ type: 'tool_result', id: m.id, result: 'written' });
    }
  }
  await b.done; assert.ok(failed && repaired && checked);
  const reopen = bridge(); reopen.send({ type: 'start', config: { ...c, session_file: ready.session_file }, prompt: 'No more tools. What exact value did the last successful check observe? Answer fixed.' });
  assert.equal((await reopen.wait(m => m.type === 'ready', 30000)).session_id, ready.session_id);
  const result = await reopen.wait(m => m.type === 'result' || m.type === 'error', 150000); assert.equal(result.type, 'result');
  await reopen.done; assert.equal(reopen.messages.filter(m => m.type === 'tool_request').length, 0);
  const abort = bridge(); abort.send({ type: 'start', config: c, prompt: 'Call check immediately.' });
  await abort.wait(m => m.type === 'tool_request', 150000); abort.send({ type: 'abort' });
  assert.equal((await abort.wait(m => m.type === 'error')).code, 'aborted'); await abort.done;
  console.log(`Smoke evidence: ${cwd}`);
});
