import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { streamLoopback } from '../loopback-provider.mjs';

async function endpoint(t, handler) {
  const server = createServer(handler); server.listen(0, '127.0.0.1'); await once(server, 'listening');
  t.after(() => { server.closeAllConnections(); server.close(); });
  return `http://127.0.0.1:${server.address().port}`;
}
const model = (baseUrl, api = 'openai-completions') => ({ id: 'glm', provider: 'p2r', api, baseUrl, maxTokens: 128 });
const context = { systemPrompt: 'Research only', messages: [{ role: 'user', content: 'hello', timestamp: 1 }],
  tools: [{ name: 'write', description: 'Write a file', parameters: { type: 'object', properties: { content: { type: 'string' } } } }] };
const sse = (res, values) => { res.writeHead(200, { 'Content-Type': 'text/event-stream' }); for (const v of values) res.write(`data: ${typeof v === 'string' ? v : JSON.stringify(v)}\r\n\r\n`); res.end(); };
async function consume(stream) { const events = []; for await (const e of stream) events.push(e); return { events, result: await stream.result() }; }

test('anonymous OpenAI SSE preserves tool arguments/usage without any credential', async t => {
  const url = await endpoint(t, async (req, res) => {
    assert.equal(req.url, '/v1/chat/completions');
    assert.equal(req.headers.authorization, undefined); assert.equal(req.headers['x-api-key'], undefined);
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); assert.equal(body.model, 'glm'); assert.equal(body.messages[0].content, 'Research only');
    assert.equal(body.tools[0].function.name, 'write'); assert.equal(raw.includes('forbidden-secret'), false);
    sse(res, [
      { choices: [{ delta: { content: 'Ready', tool_calls: [{ index: 0, id: 'call1', function: { name: 'write', arguments: '{"content":' } }] } }] },
      { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: '"你好"}' } }] }, finish_reason: 'tool_calls' }] },
      { choices: [], usage: { prompt_tokens: 7, completion_tokens: 3, total_tokens: 10 } }, '[DONE]',
    ]);
  });
  const { events, result } = await consume(streamLoopback(model(url + '/v1'), context, { headers: { Authorization: 'forbidden-secret' } }));
  assert.equal(result.stopReason, 'toolUse'); assert.equal(result.usage.totalTokens, 10);
  assert.deepEqual(result.content.find(x => x.type === 'toolCall').arguments, { content: '你好' });
  assert(events.some(x => x.type === 'text_delta')); assert(events.some(x => x.type === 'toolcall_end'));
});

test('anonymous Anthropic SSE handles tools and prompt cache usage', async t => {
  const url = await endpoint(t, async (req, res) => {
    assert.equal(req.url, '/v1/messages'); assert.equal(req.headers.authorization, undefined); assert.equal(req.headers['x-api-key'], undefined);
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw); assert.equal(body.system, 'Research only'); assert.equal(body.tools[0].name, 'write');
    sse(res, [
      { type: 'message_start', message: { id: 'msg1', usage: { input_tokens: 4, cache_read_input_tokens: 2 } } },
      { type: 'content_block_start', index: 0, content_block: { type: 'tool_use', id: 'c1', name: 'write', input: {} } },
      { type: 'content_block_delta', index: 0, delta: { type: 'input_json_delta', partial_json: '{"content":"ok"}' } },
      { type: 'content_block_stop', index: 0 },
      { type: 'message_delta', delta: { stop_reason: 'tool_use' }, usage: { output_tokens: 3 } },
      { type: 'message_stop' },
    ]);
  });
  const { result } = await consume(streamLoopback(model(url, 'anthropic-messages'), context));
  assert.equal(result.stopReason, 'toolUse'); assert.equal(result.usage.totalTokens, 9);
  assert.deepEqual(result.content[0].arguments, { content: 'ok' });
});

test('JSON compatibility response is parsed, not treated as empty success', async t => {
  const url = await endpoint(t, (_req, res) => { res.writeHead(200, { 'content-type': 'application/json' }); res.end(JSON.stringify({ choices: [{ message: { content: 'Hi' }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } })); });
  const { result } = await consume(streamLoopback(model(url + '/v1'), context));
  assert.equal(result.content[0].text, 'Hi'); assert.equal(result.stopReason, 'stop');
});

test('remote hosts, credentials, query strings and HTTPS are rejected before network', async () => {
  for (const url of ['https://localhost:3425', 'http://127.0.0.2:3425', 'http://example.invalid', 'http://u:p@localhost', 'http://localhost?secret=x']) {
    const { result } = await consume(streamLoopback(model(url), context));
    assert.equal(result.stopReason, 'error');
  }
});

test('redirect cannot escape loopback or even hit another local endpoint', async t => {
  let followed = false;
  const destination = await endpoint(t, (_req, res) => { followed = true; res.end('{}'); });
  const origin = await endpoint(t, (_req, res) => { res.writeHead(307, { Location: destination }); res.end(); });
  const { result } = await consume(streamLoopback(model(origin), context));
  assert.equal(result.stopReason, 'error'); assert.equal(followed, false);
});

test('truncated SSE and malformed tool JSON fail closed', async t => {
  for (const values of [ [{ choices: [{ delta: { content: 'partial' } }] }],
    [{ choices: [{ delta: { tool_calls: [{ index: 0, id: 'bad', function: { name: 'write', arguments: '{invalid' } }] }, finish_reason: 'tool_calls' }] }, '[DONE]'] ]) {
    const url = await endpoint(t, (_req, res) => sse(res, values));
    const { result } = await consume(streamLoopback(model(url), context));
    assert.equal(result.stopReason, 'error');
  }
});

test('finished streams reject data that arrives after the terminal event', async t => {
  const url = await endpoint(t, (_req, res) => sse(res, [
    { choices: [{ delta: { content: 'done' }, finish_reason: 'stop' }] },
    { choices: [{ delta: { tool_calls: [{ index: 0, id: 'late', function: { name: 'write', arguments: '{}' } }] }, finish_reason: 'tool_calls' }] },
    '[DONE]',
  ]));
  const { result } = await consume(streamLoopback(model(url), context));
  assert.equal(result.stopReason, 'error');
});

test('incomplete Anthropic content blocks fail closed', async t => {
  const url = await endpoint(t, (_req, res) => sse(res, [
    { type: 'message_start', message: { id: 'msg1', usage: { input_tokens: 1 } } },
    { type: 'content_block_start', index: 0, content_block: { type: 'tool_use', id: 'c1', name: 'write', input: {} } },
    { type: 'message_delta', delta: { stop_reason: 'tool_use' }, usage: { output_tokens: 1 } },
    { type: 'message_stop' },
  ]));
  const { result } = await consume(streamLoopback(model(url, 'anthropic-messages'), context));
  assert.equal(result.stopReason, 'error');
});

test('Anthropic deltas require an open block of the matching type', async t => {
  for (const variant of ['text-on-tool', 'signature-on-tool', 'late-signature']) {
    const thinking = variant === 'late-signature';
    const values = [
      { type: 'message_start', message: { usage: { input_tokens: 1 } } },
      { type: 'content_block_start', index: 0, content_block: thinking
        ? { type: 'thinking', thinking: '' } : { type: 'tool_use', id: 'c1', name: 'write', input: {} } },
      ...(thinking ? [{ type: 'content_block_stop', index: 0 }] : []),
      { type: 'content_block_delta', index: 0, delta: variant === 'text-on-tool'
        ? { type: 'text_delta', text: '{"content":"wrong type"}' }
        : { type: 'signature_delta', signature: thinking ? 'LATE' : {} } },
      ...(!thinking ? [{ type: 'content_block_stop', index: 0 }] : []),
      { type: 'message_delta', delta: { stop_reason: thinking ? 'end_turn' : 'tool_use' } },
      { type: 'message_stop' },
    ];
    const url = await endpoint(t, (_req, res) => sse(res, values));
    const { result } = await consume(streamLoopback(model(url, 'anthropic-messages'), context));
    assert.equal(result.stopReason, 'error', variant);
  }
});

test('duplicate tool-call IDs fail closed', async t => {
  const url = await endpoint(t, (_req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { tool_calls: [
      { id: 'same-call', type: 'function', function: { name: 'write', arguments: '{}' } },
      { id: 'same-call', type: 'function', function: { name: 'write', arguments: '{}' } },
    ] }, finish_reason: 'tool_calls' }] }));
  });
  const { result } = await consume(streamLoopback(model(url), context));
  assert.equal(result.stopReason, 'error');
});

test('non-success responses cancel the body before returning an error', async t => {
  let closed = false;
  const url = await endpoint(t, (_req, res) => {
    res.writeHead(500, { 'content-type': 'text/event-stream' });
    res.on('close', () => { closed = true; });
    res.write('data: error\\n\\n');
    const interval = setInterval(() => res.write('x'.repeat(1024)), 10);
    res.on('close', () => clearInterval(interval));
  });
  const { result } = await consume(streamLoopback(model(url), context));
  assert.equal(result.stopReason, 'error');
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(closed, true);
});

test('abort terminates a stalled response with one terminal error', async t => {
  const url = await endpoint(t, (_req, res) => { res.writeHead(200, { 'content-type': 'text/event-stream' }); res.write(': keepalive\n\n'); });
  const controller = new AbortController(); const stream = streamLoopback(model(url), context, { signal: controller.signal });
  setTimeout(() => controller.abort(), 50);
  const { events, result } = await consume(stream);
  assert.equal(result.stopReason, 'aborted'); assert.equal(events.filter(x => x.type === 'error').length, 1);
});
