// Project-owned anonymous transport: no SDK credential precheck, placeholder key,
// environment auth, model-supplied headers, or redirects. Text/tool protocols only.
import { createAssistantMessageEventStream, normalizeContext, getCurrentSystemPrompt, getCurrentTools } from '@earendil-works/pi-ai';

const MAX_RESPONSE = 8 * 1024 * 1024;
const MAX_EVENT = 1024 * 1024;
const assert = (condition, message) => { if (!condition) throw new Error(message); };
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const text = content => typeof content === 'string' ? content : (content ?? []).filter(x => x.type === 'text').map(x => x.text).join('\n');
const count = value => { assert(Number.isSafeInteger(value) && value >= 0, 'Invalid provider token usage'); return value; };

function request(model, context, options) {
  const url = new URL(model.baseUrl);
  assert(url.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
    && !url.username && !url.password && !url.search && !url.hash, 'Anonymous provider requires a plain HTTP loopback endpoint');
  // Avoid depending on local DNS for the no-auth destination boundary.
  if (url.hostname === 'localhost') url.hostname = '127.0.0.1';
  assert(['anthropic-messages', 'openai-completions'].includes(model.api), 'Unsupported anonymous provider API');
  const anthropic = model.api === 'anthropic-messages';
  const path = url.pathname.replace(/\/$/, '');
  url.pathname = anthropic ? path + (path.endsWith('/v1') ? '/messages' : '/v1/messages') : path + '/chat/completions';
  const normalized = normalizeContext(context);
  const system = getCurrentSystemPrompt(normalized.messages), tools = getCurrentTools(normalized.messages);
  const messages = [];
  for (const m of normalized.messages) {
    if (m.role === 'system') continue;
    assert(['user', 'assistant', 'toolResult'].includes(m.role), 'Unsupported anonymous message role');
    if (m.role === 'user') {
      assert(typeof m.content === 'string' || m.content.every(x => x.type === 'text'), 'Anonymous adapter supports text input only');
      messages.push({ role: 'user', content: anthropic ? [{ type: 'text', text: text(m.content) }] : text(m.content) });
    } else if (m.role === 'toolResult') {
      messages.push(anthropic ? { role: 'user', content: [{ type: 'tool_result', tool_use_id: m.toolCallId, content: text(m.content), is_error: Boolean(m.isError) }] }
        : { role: 'tool', tool_call_id: m.toolCallId, content: text(m.content) });
    } else {
      const calls = m.content.filter(x => x.type === 'toolCall');
      if (anthropic) {
        const content = m.content.filter(x => x.type === 'text' || x.type === 'toolCall').map(x => x.type === 'text' ? { type: 'text', text: x.text } : { type: 'tool_use', id: x.id, name: x.name, input: x.arguments });
        if (content.length) messages.push({ role: 'assistant', content });
      } else {
        messages.push({ role: 'assistant', content: text(m.content) || null,
          ...(calls.length ? { tool_calls: calls.map(x => ({ id: x.id, type: 'function', function: { name: x.name, arguments: JSON.stringify(x.arguments) } })) } : {}) });
      }
    }
  }
  if (anthropic) {
    for (let i = 1; i < messages.length;) {
      if (messages[i].role === messages[i - 1].role) { messages[i - 1].content.push(...messages[i].content); messages.splice(i, 1); }
      else i++;
    }
  } else if (system) messages.unshift({ role: 'system', content: system });
  const maxTokens = Math.min(options.maxTokens ?? model.maxTokens, model.maxTokens);
  assert(Number.isSafeInteger(maxTokens) && maxTokens > 0, 'Invalid output token limit');
  const body = { model: model.id, messages, max_tokens: maxTokens, stream: true };
  if (anthropic) {
    if (system) body.system = system;
    if (tools.length) body.tools = tools.map(t => ({ name: t.name, description: t.description, input_schema: t.parameters }));
  } else {
    body.stream_options = { include_usage: true };
    if (tools.length) body.tools = tools.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.parameters } }));
  }
  const encoded = JSON.stringify(body);
  assert(Buffer.byteLength(encoded) <= MAX_RESPONSE, 'Anonymous request exceeds size limit');
  return { url, body: encoded, anthropic };
}

export function streamLoopback(model, context, options = {}) {
  const stream = createAssistantMessageEventStream();
  const message = { role: 'assistant', content: [], api: model.api, provider: model.provider, model: model.id,
    usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
    stopReason: 'stop', timestamp: Date.now() };
  const controller = new AbortController();
  const signal = options.signal ? AbortSignal.any([controller.signal, options.signal]) : controller.signal;
  const timer = setTimeout(() => controller.abort(), 300000);
  void (async () => {
    const blocks = new Map();
    const toolIds = new Set();
    let terminal = false, finished = false, finishReason;
    const usage = (data, anthropic) => {
      if (!data) return;
      const u = message.usage;
      if (anthropic) {
        for (const [wire, local] of [['input_tokens', 'input'], ['output_tokens', 'output'], ['cache_read_input_tokens', 'cacheRead'], ['cache_creation_input_tokens', 'cacheWrite']]) {
          if (data[wire] !== undefined) u[local] = count(data[wire]);
        }
      } else {
        if (data.prompt_tokens !== undefined) {
          const cached = count(data.prompt_tokens_details?.cached_tokens ?? 0);
          const prompt = count(data.prompt_tokens); assert(cached <= prompt, 'Invalid cached token usage');
          u.input = prompt - cached; u.cacheRead = cached;
        }
        if (data.completion_tokens !== undefined) u.output = count(data.completion_tokens);
      }
      u.totalTokens = u.input + u.output + u.cacheRead + u.cacheWrite;
      if (!anthropic && data.total_tokens !== undefined) {
        assert(count(data.total_tokens) >= u.totalTokens, 'Inconsistent provider usage'); u.totalTokens = data.total_tokens;
      }
    };
    const startBlock = (key, type, info = {}) => {
      assert(blocks.size < 256, 'Too many response content blocks');
      const part = type === 'toolCall' ? { type, id: info.id ?? '', name: info.name ?? '', arguments: {} }
        : type === 'thinking' ? { type, thinking: '' } : { type: 'text', text: '' };
      if (type === 'toolCall' && info.id) assert(!toolIds.has(info.id), 'Duplicate tool call ID');
      if (type === 'toolCall' && info.id) toolIds.add(info.id);
      const state = { index: message.content.length, part, json: '', ended: false, initial: info.input ?? {} };
      blocks.set(key, state); message.content.push(part);
      stream.push({ type: type === 'toolCall' ? 'toolcall_start' : `${type}_start`, contentIndex: state.index, partial: message });
      return state;
    };
    const delta = (block, value) => {
      assert(!block.ended && typeof value === 'string', 'Invalid content delta');
      const type = block.part.type;
      if (type === 'toolCall') block.json += value;
      else block.part[type === 'text' ? 'text' : 'thinking'] += value;
      stream.push({ type: type === 'toolCall' ? 'toolcall_delta' : `${type}_delta`, contentIndex: block.index, delta: value, partial: message });
    };
    const endBlock = block => {
      if (block.ended) return;
      block.ended = true;
      if (block.part.type === 'toolCall') {
        block.part.arguments = block.json ? JSON.parse(block.json) : block.initial;
        assert(block.part.id && block.part.name && object(block.part.arguments), 'Invalid tool call');
        stream.push({ type: 'toolcall_end', contentIndex: block.index, toolCall: block.part, partial: message });
      } else stream.push({ type: `${block.part.type}_end`, contentIndex: block.index,
        content: block.part[block.part.type === 'text' ? 'text' : 'thinking'], partial: message });
    };
    const reason = value => {
      assert(['stop', 'end_turn', 'stop_sequence', 'tool_calls', 'tool_use', 'length', 'max_tokens'].includes(value), 'Unsupported provider stop reason');
      assert(!finished, 'Duplicate provider stop reason');
      finishReason = ['tool_calls', 'tool_use'].includes(value) ? 'toolUse' : ['length', 'max_tokens'].includes(value) ? 'length' : 'stop';
      finished = true;
    };
    const openai = data => {
      assert(object(data) && !data.error, 'Provider reported an error');
      if (data.id) message.responseId = data.id;
      usage(data.usage, false);
      if (finished) { assert(!(data.choices ?? []).length, 'Data after provider stop reason'); return; }
      for (const c of data.choices ?? []) {
        assert(!finished, 'Choice after provider stop reason');
        assert((c.index ?? 0) === 0, 'Multiple response choices are unsupported');
        const d = c.delta ?? c.message ?? {};
        if (d.content) delta(blocks.get('text') ?? startBlock('text', 'text'), d.content);
        if (d.reasoning_content) delta(blocks.get('thinking') ?? startBlock('thinking', 'thinking'), d.reasoning_content);
        for (const [i, call] of (d.tool_calls ?? []).entries()) {
          const index = call.index ?? i; assert(Number.isSafeInteger(index) && index >= 0 && index < 256, 'Invalid tool index');
          const key = `tool-${index}`, b = blocks.get(key) ?? startBlock(key, 'toolCall', call.function ? { id: call.id, name: call.function.name } : {});
          if (call.id) {
            assert(!b.part.id || b.part.id === call.id, 'Tool call ID changed mid-stream');
            assert(!toolIds.has(call.id) || b.part.id === call.id, 'Duplicate tool call ID');
            if (!b.part.id) { assert(!toolIds.has(call.id), 'Duplicate tool call ID'); toolIds.add(call.id); }
            b.part.id = call.id;
          }
          if (call.function?.name) b.part.name = call.function.name;
          if (call.function?.arguments) delta(b, call.function.arguments);
        }
        if (c.finish_reason) reason(c.finish_reason);
      }
    };
    const anthropic = data => {
      assert(object(data) && data.type !== 'error', 'Provider reported an error');
      assert(data.type === 'message_stop' || (!finished && !terminal), 'Data after provider stop event');
      if (data.type === 'message_start') { usage(data.message?.usage, true); if (data.message?.id) message.responseId = data.message.id; }
      else if (data.type === 'content_block_start') {
        assert(!blocks.has(data.index), 'Duplicate content index');
        const c = data.content_block;
        assert(c && ['text', 'thinking', 'tool_use', 'redacted_thinking'].includes(c.type), 'Unsupported content block');
        const b = startBlock(data.index, c.type === 'tool_use' ? 'toolCall' : c.type === 'text' ? 'text' : 'thinking', c);
        if (c.text || c.thinking) delta(b, c.text ?? c.thinking);
      } else if (data.type === 'content_block_delta') {
        const b = blocks.get(data.index); assert(b && !b.ended, 'Content delta without open block');
        const d = data.delta;
        const expected = { text_delta: 'text', thinking_delta: 'thinking', input_json_delta: 'toolCall', signature_delta: 'thinking' };
        assert(object(d) && expected[d.type] === b.part.type, 'Content delta type mismatch');
        if (d.type === 'text_delta') delta(b, d.text);
        else if (d.type === 'thinking_delta') delta(b, d.thinking);
        else if (d.type === 'input_json_delta') delta(b, d.partial_json);
        else if (d.type === 'signature_delta') {
          assert(typeof d.signature === 'string', 'Invalid thinking signature');
          b.part.thinkingSignature = (b.part.thinkingSignature ?? '') + d.signature;
        }
        else throw new Error('Unsupported content delta');
      } else if (data.type === 'content_block_stop') { const b = blocks.get(data.index); assert(b && !b.ended, 'Content stop without open block'); endBlock(b); }
      else if (data.type === 'message_delta') {
        for (const b of blocks.values()) assert(b.ended, 'Message delta before content block stop');
        usage(data.usage, true); if (data.delta?.stop_reason) reason(data.delta.stop_reason);
      }
      else if (data.type === 'message_stop') {
        for (const block of blocks.values()) assert(block.ended, 'Content block missing stop event');
        terminal = true;
      }
      else assert(data.type === 'ping', 'Unsupported message event');
    };
    try {
      const req = request(model, context, options);
      const response = await fetch(req.url, { method: 'POST', body: req.body, redirect: 'error', signal,
        headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream', ...(req.anthropic ? { 'anthropic-version': '2023-06-01' } : {}) } });
      if (!response.ok || !response.body) {
        await response.body?.cancel().catch(() => {});
        assert(false, `Anonymous provider HTTP ${response.status}`);
      }
      stream.push({ type: 'start', partial: message });
      const reader = response.body.getReader(), decoder = new TextDecoder('utf-8', { fatal: true });
      const jsonResponse = (response.headers.get('content-type') ?? '').includes('application/json');
      let buffer = '', received = 0;
      const packet = value => {
        const lines = value.split('\n').map(x => x.replace(/\r$/, ''));
        const payload = lines.filter(x => x.startsWith('data:')).map(x => x.slice(5).replace(/^ /, '')).join('\n');
        if (!payload) return;
        assert(!terminal, 'Data after stream terminator');
        if (payload === '[DONE]') { assert(!req.anthropic, 'Invalid Anthropic terminator'); terminal = true; return; }
        (req.anthropic ? anthropic : openai)(JSON.parse(payload));
      };
      try {
        while (true) {
          const chunk = await reader.read();
          if (chunk.done) break;
          received += chunk.value.byteLength; assert(received <= MAX_RESPONSE, 'Anonymous response exceeds size limit');
          buffer += decoder.decode(chunk.value, { stream: true });
          if (!jsonResponse) {
            let match;
            while ((match = /\r?\n\r?\n/.exec(buffer))) {
              const part = buffer.slice(0, match.index); assert(Buffer.byteLength(part) <= MAX_EVENT, 'SSE event exceeds size limit');
              buffer = buffer.slice(match.index + match[0].length); packet(part);
            }
            assert(Buffer.byteLength(buffer) <= MAX_EVENT, 'SSE event exceeds size limit');
          }
        }
        buffer += decoder.decode();
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock(); }
      if (jsonResponse) {
        const data = JSON.parse(buffer); assert(!data.error, 'Provider reported an error');
        if (req.anthropic) {
          usage(data.usage, true); if (data.id) message.responseId = data.id;
          for (const [index, c] of (data.content ?? []).entries()) anthropic({ type: 'content_block_start', index, content_block: c });
          reason(data.stop_reason);
        } else openai(data);
        terminal = true;
      } else if (buffer.trim()) packet(buffer);
      assert(terminal && finishReason, 'Truncated provider stream');
      for (const b of blocks.values()) endBlock(b);
      assert(finishReason !== 'toolUse' || message.content.some(x => x.type === 'toolCall'), 'Tool stop without tool calls');
      message.stopReason = finishReason;
      stream.push({ type: 'done', reason: finishReason, message });
    } catch (error) {
      message.stopReason = signal.aborted ? 'aborted' : 'error';
      // Do not echo provider response bodies, URLs or arbitrary fetch errors.
      message.errorMessage = signal.aborted ? 'Anonymous provider request aborted' : 'Anonymous provider request failed: ' + (error.message?.startsWith('Anonymous provider HTTP ') ? error.message : 'invalid response or transport');
      stream.push({ type: 'error', reason: message.stopReason, error: message });
    } finally { clearTimeout(timer); controller.abort(); stream.end(); }
  })();
  return stream;
}
