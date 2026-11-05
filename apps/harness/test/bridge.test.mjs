import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { bridge, config } from './helpers.mjs';
import { validateStart } from '../bridge.mjs';
const temp = async () => { const root = fileURLToPath(new URL('../test-artifacts/', import.meta.url)); await mkdir(root, { recursive: true }); return mkdtemp(join(root, 'protocol-')); };
async function server(t, handler) {
  let calls = 0;
  const s = createServer(async (req, res) => {
    let text = ''; for await (const part of req) text += part;
    handler(JSON.parse(text), res, ++calls, req);
  });
  s.listen(0, '127.0.0.1'); await once(s, 'listening');
  t.after(() => { s.closeAllConnections(); s.close(); });
  return { url: `http://127.0.0.1:${s.address().port}/v1`, calls: () => calls };
}
function reply(res, tool = false, tokens = 4) {
  res.writeHead(200, { 'content-type': 'text/event-stream' });
  const delta = tool ? { tool_calls: [{ index: 0, id: 'call_1', type: 'function', function: { name: 'probe', arguments: '{}' } }] } : { content: 'DONE' };
  for (const data of [
    { choices: [{ index: 0, delta: { role: 'assistant' }, finish_reason: null }] },
    { choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: tool ? 'tool_calls' : 'stop' }], usage: { prompt_tokens: tokens, completion_tokens: 2, total_tokens: tokens + 2 } },
  ]) res.write(`data: ${JSON.stringify({ id: 'chat-test', object: 'chat.completion.chunk', model: 'test', ...data })}\n\n`);
  res.end('data: [DONE]\n\n');
}
test('anonymous auth requires explicit loopback mode and disables auth header', () => {
  const c = config('/unused', 'http://127.0.0.1:3425/v1');
  c.model = { ...c.model, auth_mode: 'none', api_key: '' };
  assert.equal(validateStart({ type: 'start', config: c, prompt: 'hi' }), c);
  c.model.base_url = 'https://provider.example/v1';
  assert.throws(() => validateStart({ type: 'start', config: c, prompt: 'hi' }), /loopback HTTP/);
  c.model.base_url = 'http://127.0.0.1:3425/v1'; c.model.api_key = 'fake';
  assert.throws(() => validateStart({ type: 'start', config: c, prompt: 'hi' }), /must be empty/);
});

test('anonymous auth performs a real SDK request without Authorization', async t => {
  const seen = [];
  const s = await server(t, (body, res, _n, req) => { seen.push({ body, authorization: req.headers.authorization }); reply(res); });
  const cwd = await temp(); const c = config(cwd, s.url);
  c.model = { ...c.model, auth_mode: 'none', api_key: '' };
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'hi' });
  const result = await b.wait(m => m.type === 'result' || m.type === 'error');
  assert.equal(result.type, 'result', JSON.stringify(result));
  assert.equal(seen.length, 1);
  assert.equal(seen[0].authorization, undefined);
  await b.done;
});

test('validation accepts empty schema and rejects malformed protocol/schema', () => {
  const c = config('/unused', 'http://localhost'); c.tools = [{ name: 'probe', description: '', parameters: {} }];
  assert.equal(validateStart({ type: 'start', config: c, prompt: 'hi' }), c);
  assert.throws(() => validateStart({ type: 'start' }), /prompt/);
  c.tools[0].parameters = { type: 'nonsense' };
  assert.throws(() => validateStart({ type: 'start', config: c, prompt: 'hi' }), /schema/);
});
test('invalid JSON and abort before start return terminal errors', async () => {
  for (const input of ['bad\n', '{"type":"abort"}\n']) {
    const b = bridge(); b.child.stdin.write(input);
    assert.equal((await b.wait(m => m.type === 'error')).code, input === 'bad\n' ? 'protocol_error' : 'aborted');
    await b.done;
  }
});
test('real SDK tool roundtrip, only supplied tool, no key persisted, reopen without replay', async t => {
  const s = await server(t, (body, res, n) => { assert.deepEqual(body.tools.map(x => x.function.name), ['probe']); reply(res, n === 1); });
  const cwd = await temp(); const c = config(cwd, s.url); c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'call probe' });
  const ready = await b.wait(m => m.type === 'ready');
  const request = await b.wait(m => m.type === 'tool_request');
  b.send({ type: 'tool_result', id: request.id, result: { ok: true } });
  const result = await b.wait(m => m.type === 'result' || m.type === 'error');
  assert.equal(result.type, 'result', JSON.stringify(result)); assert.equal(result.text, 'DONE'); assert.equal(result.usage.calls, 2);
  await b.done;
  assert.ok(!(await readFile(ready.session_file, 'utf8')).includes(c.model.api_key));
  const reopened = bridge(); reopened.send({ type: 'start', config: { ...c, session_file: ready.session_file }, prompt: 'continue' });
  assert.equal((await reopened.wait(m => m.type === 'ready')).session_id, ready.session_id);
  assert.equal((await reopened.wait(m => m.type === 'result' || m.type === 'error')).type, 'result');
  await reopened.done;
  assert.equal(reopened.messages.filter(m => m.type === 'tool_request').length, 0);
  assert.equal(s.calls(), 3);
});
test('call budget stops extra provider request after tool', async t => {
  const s = await server(t, (_body, res) => reply(res, true)); const cwd = await temp(); const c = config(cwd, s.url);
  c.tools = [{ name: 'probe', description: 'probe', parameters: {} }]; c.limits.max_model_calls = 1;
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' });
  const request = await b.wait(m => m.type === 'tool_request'); b.send({ type: 'tool_result', id: request.id, result: 'ok' });
  assert.equal((await b.wait(m => m.type === 'error')).code, 'budget_exceeded'); await b.done; assert.equal(s.calls(), 1);
});
test('token budget stops extra provider request with queued steer', async t => {
  const s = await server(t, (_body, res, n) => reply(res, n === 1, 12));
  for (const limited of [true, false]) {
    const c = config(await temp(), s.url); c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
    c.limits.max_model_tokens = limited ? 1 : 10000;
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' });
    if (limited) {
      const request = await b.wait(m => m.type === 'tool_request');
      b.send({ type: 'steer', message: 'steered-message' });
      b.send({ type: 'tool_result', id: request.id, result: 'ok' });
      assert.equal((await b.wait(m => m.type === 'error')).code, 'budget_exceeded');
      assert.equal(s.calls(), 1);
    } else assert.equal((await b.wait(m => m.type === 'result' || m.type === 'error')).type, 'result');
    await b.done;
  }
});
test('zero budget blocks even first request', async t => {
  const s = await server(t, (_b, res) => reply(res)); const c = config(await temp(), s.url); c.limits.max_model_calls = 0;
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'hi' });
  assert.equal((await b.wait(m => m.type === 'error')).code, 'budget_exceeded'); await b.done; assert.equal(s.calls(), 0);
});
test('tool result credential is cleaned before it can re-enter model context', async t => {
  const key = 'audit"quote\\credential'; const bodies = [];
  const s = await server(t, (body, res, n) => { bodies.push(body); reply(res, n === 1); });
  const c = config(await temp(), s.url); c.model.api_key = key; c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' });
  const request = await b.wait(m => m.type === 'tool_request'); b.send({ type: 'tool_result', id: request.id, result: { nested: key } });
  assert.equal((await b.wait(m => m.type === 'result' || m.type === 'error')).type, 'result'); await b.done;
  assert.equal(bodies.length, 2); assert.ok(!JSON.stringify(bodies[1]).includes(key)); assert.ok(JSON.stringify(bodies[1]).includes('[REDACTED]'));
});
test('events and usage are ordered; echoed credentials never reach stdout or transcript', async t => {
  const secret = 'secret-bridge-test-key';
  const s = await server(t, (_body, res, n) => reply(res, n === 1));
  const c = config(await temp(), s.url); c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' });
  const request = await b.wait(m => m.type === 'tool_request'); b.send({ type: 'tool_result', id: request.id, result: secret });
  const result = await b.wait(m => m.type === 'result' || m.type === 'error'); await b.done;
  assert.equal(result.type, 'result'); assert.deepEqual(result.usage, { calls: 2, input_tokens: 8, output_tokens: 4, total_tokens: 12 });
  assert.equal(b.messages[0].type, 'ready'); assert.equal(b.messages.at(-1).type, 'result');
  assert.ok(b.messages.some(m => m.event?.type === 'tool_execution_end'));
  assert.ok(!JSON.stringify(b.messages).includes(secret)); assert.ok(!b.stderr().includes(secret));
  assert.ok(!(await readFile(result.session_file, 'utf8')).includes(secret));
});
test('provider error is terminal and credential echo is redacted', async t => {
  const s = await server(t, (_body, res) => { res.writeHead(401, { 'content-type': 'application/json' }); res.end(JSON.stringify({ error: { message: 'invalid secret-bridge-test-key' } })); });
  const b = bridge(); b.send({ type: 'start', config: config(await temp(), s.url), prompt: 'hi' });
  const err = await b.wait(m => m.type === 'error'); await b.done;
  assert.equal(err.code, 'model_error'); assert.ok(!JSON.stringify(b.messages).includes('secret-bridge-test-key'));
  const ready = b.messages.find(m => m.type === 'ready'); assert.ok(!(await readFile(ready.session_file, 'utf8')).includes('secret-bridge-test-key'));
  assert.equal(s.calls(), 1);
});
test('HTTP 401 embedded JSON credential echoes are redacted at bounded escaping depths', async t => {
  const key = 'audit"quote\\credential';
  const variants = [key];
  for (let depth = 0; depth < 8; depth++) variants.push(JSON.stringify(variants.at(-1)).slice(1, -1));
  for (const depth of [0, 1, 3, 7]) {
    const s = await server(t, (_body, res) => {
      res.writeHead(401, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'invalid ' + variants[depth] } }));
    });
    const c = config(await temp(), s.url); c.model.api_key = key;
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'hi' });
    const err = await b.wait(m => m.type === 'error'); await b.done;
    assert.equal(err.code, 'model_error'); assert.ok(err.message.includes('[REDACTED]'));
    const ready = b.messages.find(m => m.type === 'ready');
    const transcript = await readFile(ready.session_file, 'utf8');
    const surfaces = [JSON.stringify(b.messages), transcript, b.stderr()];
    // Parse the persisted entries too: checking serialized bytes alone misses
    // credentials whose backslashes have been escaped by the transcript itself.
    surfaces.push(JSON.stringify(transcript.trim().split('\n').map(line => JSON.parse(line))));
    for (const text of surfaces) for (const variant of variants) assert.ok(!text.includes(variant), `credential echo at depth ${depth}`);
    assert.equal(s.calls(), 1);
  }
});
test('unexpected commands and unknown tool ids cancel once', async t => {
  for (const bad of [{ type: 'start' }, { type: 'tool_result', id: 'unknown', result: 'bad' }, { type: 'steer', message: '' }]) {
    const s = await server(t, (_body, res) => reply(res, true)); const c = config(await temp(), s.url); c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' }); await b.wait(m => m.type === 'tool_request'); b.send(bad);
    assert.equal((await b.wait(m => m.type === 'error')).code, 'protocol_error'); await b.done;
    assert.equal(b.messages.filter(m => m.type === 'error').length, 1); assert.equal(b.messages.filter(m => m.type === 'result').length, 0);
  }
});
test('credential shell commands and environment expressions remain literal', async t => {
  const cwd = await temp(), marker = join(cwd, 'must-not-exist');
  const seen = [];
  const s = createServer(async (req, res) => { for await (const _ of req) {} seen.push(req.headers.authorization); reply(res); });
  s.listen(0, '127.0.0.1'); await once(s, 'listening'); t.after(() => { s.closeAllConnections(); s.close(); });
  const key = `!printf host-execution > '${marker}'`;
  const c = config(cwd, `http://127.0.0.1:${s.address().port}/v1`); c.model.api_key = key;
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'hi' });
  assert.equal((await b.wait(m => m.type === 'result' || m.type === 'error')).type, 'result'); await b.done;
  assert.equal(seen.at(-1), `Bearer ${key}`);
  await assert.rejects(stat(marker), { code: 'ENOENT' });
});
test('escaped credential is cleaned in nested event values and all persisted entry kinds', async t => {
  const key = 'audit"quote\\credential';
  const s = await server(t, (_body, res, n) => reply(res, n === 1));
  const c = config(await temp(), s.url); c.model.api_key = key; c.system_prompt = `system ${key}`;
  c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
  const b = bridge(); b.send({ type: 'start', config: c, prompt: `probe ${key}` });
  const request = await b.wait(m => m.type === 'tool_request'); b.send({ type: 'tool_result', id: request.id, result: { nested: [key] } });
  const result = await b.wait(m => m.type === 'result' || m.type === 'error'); await b.done; assert.equal(result.type, 'result');
  const serializedKey = JSON.stringify(key).slice(1, -1);
  for (const text of [JSON.stringify(b.messages), await readFile(result.session_file, 'utf8'), b.stderr()]) {
    assert.ok(!text.includes(key)); assert.ok(!text.includes(serializedKey));
  }
});
test('undeclared project system and append prompts are never loaded', async t => {
  const cwd = await temp(); await mkdir(join(cwd, '.pi'));
  for (const name of ['APPEND_SYSTEM.md', 'SYSTEM.md']) await writeFile(join(cwd, '.pi', name), 'AUDIT_INJECTION');
  await writeFile(join(cwd, 'AGENTS.md'), 'AUDIT_CONTEXT');
  const s = await server(t, (body, res) => { assert.ok(!JSON.stringify(body).includes('AUDIT_')); assert.ok(JSON.stringify(body).includes('You are a test assistant.')); reply(res); });
  const b = bridge(); b.send({ type: 'start', config: config(cwd, s.url), prompt: 'hi' });
  assert.equal((await b.wait(m => m.type === 'result' || m.type === 'error')).type, 'result'); await b.done;
});
test('empty, corrupt, orphaned and invalid-message resumes fail closed without changing files', async t => {
  const s = await server(t, (_body, res) => reply(res)); const cwd = await temp();
  const header = JSON.stringify({ type: 'session', version: 3, id: '01a1a155-f3d2-77e6-8bab-b432bfc9e750', cwd, timestamp: new Date().toISOString() });
  for (const text of ['', header + '\n{bad json}\n', header + '\n' + JSON.stringify({ type: 'message', id: 'one', parentId: 'missing', timestamp: new Date().toISOString(), message: { role: 'user', content: 'hi' } }), header + '\n' + JSON.stringify({ type: 'message', id: 'one', parentId: null, timestamp: new Date().toISOString(), message: {} })]) {
    const file = join(await temp(), 'bad.jsonl'); await writeFile(file, text);
    const b = bridge(); b.send({ type: 'start', config: { ...config(cwd, s.url), session_file: file }, prompt: 'hi' });
    assert.equal((await b.wait(m => m.type === 'error')).code, 'invalid_session'); await b.done;
    assert.deepEqual(b.messages.map(m => m.type), ['error']); assert.equal(await readFile(file, 'utf8'), text);
  }
  assert.equal(s.calls(), 0);
});
test('illegal command during initialization cannot emit ready after terminal error', async t => {
  const s = await server(t, (_body, res) => reply(res));
  const b = bridge(); b.child.stdin.write(JSON.stringify({ type: 'start', config: config(await temp(), s.url), prompt: 'hi' }) + '\n{"type":"invalid"}\n');
  await b.wait(m => m.type === 'error'); await b.done;
  assert.deepEqual(b.messages.map(m => m.type), ['error']); assert.equal(s.calls(), 0);
});
test('SIGTERM before start closes stdin and exits', async () => {
  const b = bridge();
  // Receiving a nonterminal process signal handler requires module startup first.
  await new Promise(resolve => setTimeout(resolve, 600)); b.child.kill('SIGTERM');
  const err = await b.wait(m => m.type === 'error', 3000); assert.equal(err.code, 'aborted');
  await b.done; assert.deepEqual(b.messages.map(m => m.type), ['error']);
});
function delegateArgs(id = 'methods') {
  return { specialist_id: id, task_type: 'method_design', instruction: 'Compare two methods', context: 'Small dataset', reason: 'Need a methodological second opinion' };
}
function specialistConfig(c, mode = 'api-key') {
  c.limits.max_model_calls = 6;
  c.specialists = [{ id: 'methods', description: 'Method comparison specialist', task_types: ['method_design'], model: { ...c.model, id: 'expert', auth_mode: mode, api_key: mode === 'none' ? '' : 'expert-secret-key' } }];
  return c;
}
function response(res, { calls, text = 'ADVICE', usage = { prompt_tokens: 5, completion_tokens: 3, total_tokens: 8 } } = {}) {
  res.writeHead(200, { 'content-type': 'text/event-stream' });
  const delta = calls ? { tool_calls: calls.map((args, index) => ({ index, id: `delegate_${index}`, type: 'function', function: { name: 'research_delegate', arguments: JSON.stringify(args) } })) } : { content: text };
  res.end(`data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason: null }] })}\n\ndata: ${JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: calls ? 'tool_calls' : 'stop' }], usage })}\n\ndata: [DONE]\n\n`);
}
const events = b => b.messages.filter(m => m.type === 'event').map(m => m.event);

for (const mode of ['api-key', 'none']) test(`delegation real HTTP ${mode}: advertised whitelist, isolated inference and shared usage`, async t => {
  const seen = [];
  const s = await server(t, (body, res, n, req) => {
    seen.push({ body, headers: req.headers });
    response(res, n === 1 ? { calls: [delegateArgs()] } : { text: n === 2 ? 'ADVICE' : 'FINAL' });
  });
  const c = specialistConfig(config(await temp(), s.url), mode);
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'Investigate a research question' });
  const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
  assert.equal(result.type, 'result', JSON.stringify(result)); assert.equal(result.text, 'FINAL');
  assert.deepEqual(result.usage, { calls: 3, input_tokens: 15, output_tokens: 9, total_tokens: 24 });
  const initial = JSON.stringify(seen[0].body.messages);
  for (const value of ['methods', 'Method comparison specialist', 'method_design', 'expert']) assert.ok(initial.includes(value), value);
  assert.ok(!initial.includes(s.url)); assert.equal(seen[1].body.model, 'expert');
  assert.ok(!seen[1].body.tools?.length); assert.ok(!JSON.stringify(seen[1].body).includes('research_delegate'));
  assert.equal(seen[1].headers.authorization, mode === 'none' ? undefined : 'Bearer expert-secret-key');
  assert.ok(JSON.stringify(seen[2].body).includes('unverified_advisory'));
  assert.deepEqual(events(b).filter(e => e.delegated).map(e => e.type), ['turn_start', 'message_end']);
  const lifecycle = events(b).filter(e => e.type.startsWith('research_delegation_'));
  assert.deepEqual(lifecycle.map(e => e.type), ['research_delegation_started', 'research_delegation_completed']);
  assert.equal(lifecycle[1].reason, delegateArgs().reason); assert.equal(lifecycle[1].text, 'ADVICE');
});

test('specialist validation rejects duplicate task types and unsafe URLs', () => {
  for (const change of [c => c.specialists[0].task_types.push('method_design'), ...['ftp://localhost/v1', 'http://user:pass@localhost/v1', 'http://localhost/v1?q=key', 'http://localhost/v1#key'].map(url => c => c.specialists[0].model.base_url = url)]) {
    const c = specialistConfig(config('/unused', 'http://localhost/v1')); change(c);
    assert.throws(() => validateStart({ type: 'start', config: c, prompt: 'hi' }));
  }
});

test('delegation budget rejection emits no delegated billing events', async t => {
  const s = await server(t, (_body, res) => response(res, { calls: [delegateArgs()] }));
  const c = specialistConfig(config(await temp(), s.url)); c.limits.max_model_calls = 1;
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
  assert.equal((await b.wait(m => m.type === 'error')).code, 'budget_exceeded'); await b.done;
  assert.equal(s.calls(), 1); assert.equal(events(b).filter(e => e.delegated).length, 0);
});

for (const mode of ['error', 'empty', 'abort']) test(`delegation ${mode} settles exactly once and stops entire run`, async t => {
  let notify; const requested = new Promise(r => notify = r);
  const s = await server(t, (_body, res, n) => {
    if (n === 1) return response(res, { calls: [delegateArgs()] });
    notify();
    if (mode === 'abort') return;
    if (mode === 'empty') return response(res, { text: '' });
    res.writeHead(400); res.end('specialist failure');
  });
  const c = specialistConfig(config(await temp(), s.url));
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
  if (mode === 'abort') { await requested; b.send({ type: 'abort' }); }
  const err = await b.wait(m => m.type === 'error'); await b.done;
  assert.equal(err.code, mode === 'abort' ? 'aborted' : 'model_error'); assert.equal(s.calls(), 2);
  const ends = events(b).filter(e => e.delegated && e.type === 'message_end');
  assert.equal(ends.length, 1); assert.equal(ends[0].message.stopReason, mode === 'abort' ? 'aborted' : 'error');
  assert.ok(!events(b).some(e => e.type === 'research_delegation_completed'));
});

test('all specialist keys and nested encodings are absent from contexts, output and session', async t => {
  const key = 'other"expert\\credential'; const variants = [key];
  for (let i = 0; i < 8; i++) variants.push(JSON.stringify(variants.at(-1)).slice(1, -1));
  const seen = [];
  const s = await server(t, (body, res, n) => { seen.push(body); response(res, n === 1 ? { calls: [{ ...delegateArgs(), context: variants[3] }] } : { text: n === 2 ? variants[7] : 'FINAL' }); });
  const c = specialistConfig(config(await temp(), s.url));
  c.specialists.push({ ...c.specialists[0], id: 'other', model: { ...c.model, api_key: key } });
  c.system_prompt += key;
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research ' + variants[2] });
  const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
  assert.equal(result.type, 'result');
  for (const surface of [...seen.map(x => JSON.stringify(x)), JSON.stringify(b.messages), await readFile(result.session_file, 'utf8'), b.stderr()]) {
    for (const v of variants) assert.ok(!surface.includes(v));
    assert.ok(!surface.includes(c.specialists[0].model.api_key));
  }
});

test('delegation rejects invalid ids, task types, extra fields and oversized or blank arguments before admission', async t => {
  for (const invalid of [
    { specialist_id: 'unknown' }, { task_type: 'coding' }, { task_type: 'bad' }, { extra: 'not allowed' },
    { instruction: 'x'.repeat(12001) }, { context: 'x'.repeat(24001) }, { reason: 'x'.repeat(2001) },
    { instruction: ' ' }, { reason: ' ' }, { context: 12 },
  ]) {
    const s = await server(t, (_body, res, n) => response(res, n === 1 ? { calls: [{ ...delegateArgs(), ...invalid }] } : { text: 'FINAL' }));
    const c = specialistConfig(config(await temp(), s.url));
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
    const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
    assert.equal(result.type, 'result', JSON.stringify(invalid));
    assert.equal(result.usage.calls, 2); assert.equal(s.calls(), 2);
    assert.equal(events(b).filter(e => e.delegated || e.type.startsWith('research_delegation_')).length, 0);
  }
});

test('two profiles sharing a model are selected by id, run serially, and cannot bypass token budget', async t => {
  for (const limited of [false, true]) {
    let active = 0, maximum = 0; const seen = [];
    const s = await server(t, (body, res, n, req) => {
      seen.push({ body, authorization: req.headers.authorization });
      if (n === 1) return response(res, { calls: [delegateArgs(), delegateArgs('second')] });
      if (body.model !== 'expert') return response(res, { text: 'FINAL' });
      active++; maximum = Math.max(maximum, active);
      setTimeout(() => { active--; response(res); }, 40);
    });
    const c = specialistConfig(config(await temp(), s.url));
    c.specialists.push({ ...c.specialists[0], id: 'second', description: 'Independent comparison reviewer', model: { ...c.specialists[0].model, api_key: 'second-profile-key' } });
    if (limited) c.limits.max_model_tokens = 16;
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
    const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
    assert.equal(maximum, 1);
    assert.deepEqual(events(b).filter(e => e.type === 'research_delegation_started').map(e => e.specialist_id), limited ? ['methods'] : ['methods', 'second']);
    if (limited) { assert.equal(result.code, 'budget_exceeded'); assert.equal(s.calls(), 2); }
    else {
      assert.equal(result.type, 'result'); assert.equal(result.usage.calls, 4);
      assert.deepEqual(seen.slice(1, 3).map(x => x.authorization), ['Bearer expert-secret-key', 'Bearer second-profile-key']);
      assert.equal(events(b).filter(e => e.delegated && e.type === 'message_end').length, 2);
    }
  }
});

for (const authMode of ['api-key', 'none']) test(`Anthropic ${authMode} specialist cache read/write usage joins main budget`, async t => {
  const s = await server(t, (body, res, n) => {
    if (n === 1) return response(res, { calls: [delegateArgs()] });
    if (body.model !== 'expert') return response(res, { text: 'FINAL' });
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    for (const event of [
      { type: 'message_start', message: { id: 'expert-test', type: 'message', role: 'assistant', model: 'expert', content: [], usage: { input_tokens: 5, cache_read_input_tokens: 7, cache_creation_input_tokens: 11, output_tokens: 0 } } },
      { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
      { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'ADVICE' } },
      { type: 'content_block_stop', index: 0 },
      { type: 'message_delta', delta: { stop_reason: 'end_turn' }, usage: { output_tokens: 3 } },
      { type: 'message_stop' },
    ]) res.write(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`);
    res.end();
  });
  const c = specialistConfig(config(await temp(), s.url), authMode); c.specialists[0].model.api = 'anthropic-messages';
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
  const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
  assert.equal(result.type, 'result', JSON.stringify(result));
  assert.deepEqual(result.usage, { calls: 3, input_tokens: 33, output_tokens: 9, total_tokens: 42 });
  const end = events(b).find(e => e.delegated && e.type === 'message_end');
  assert.equal(end.message.usage.cacheRead, 7); assert.equal(end.message.usage.cacheWrite, 11);
});

test('delegation without reported usage fails closed instead of allowing a retry', async t => {
  const s = await server(t, (_body, res, n) => response(res, n === 1 ? { calls: [delegateArgs()] } : { usage: null, text: 'unmetered' }));
  const c = specialistConfig(config(await temp(), s.url));
  const b = bridge(); b.send({ type: 'start', config: c, prompt: 'research' });
  const result = await b.wait(m => ['result', 'error'].includes(m.type)); await b.done;
  assert.equal(result.type, 'error'); assert.equal(result.code, 'model_error'); assert.equal(s.calls(), 2);
  assert.equal(events(b).filter(e => e.delegated && e.type === 'message_end').length, 1);
});

test('pending tool abort, EOF, SIGTERM and wall deadline settle', async t => {
  for (const mode of ['abort', 'eof', 'sigterm', 'wall']) {
    const s = await server(t, (_body, res) => reply(res, true)); const c = config(await temp(), s.url); c.tools = [{ name: 'probe', description: 'probe', parameters: {} }];
    if (mode === 'wall') c.limits.wall_time_seconds = 0.5;
    const b = bridge(); b.send({ type: 'start', config: c, prompt: 'probe' }); await b.wait(m => m.type === 'tool_request');
    if (mode === 'abort') b.send({ type: 'abort' });
    if (mode === 'eof') b.child.stdin.end();
    if (mode === 'sigterm') b.child.kill('SIGTERM');
    const err = await b.wait(m => m.type === 'error'); assert.equal(err.code, mode === 'wall' ? 'wall_time_exceeded' : mode === 'eof' ? 'stdin_eof' : 'aborted');
    await b.done; assert.equal(s.calls(), 1);
  }
});
