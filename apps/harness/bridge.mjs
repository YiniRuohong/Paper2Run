import { createInterface } from 'node:readline';
import { resolve, join } from 'node:path';
import { stat, readFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import Ajv from 'ajv';
import { createAgentSession, DefaultResourceLoader, ModelRuntime, SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';

const ajv = new Ajv({ strict: false });
export class BridgeError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (message) => { throw new BridgeError('protocol_error', message); };
function validateModel(m) {
  if (!m || !['anthropic-messages', 'openai-completions'].includes(m.api)) fail('unsupported model.api');
  if (typeof m.id !== 'string' || !m.id) fail('model.id is required');
  if (typeof m.base_url !== 'string' || !m.base_url) fail('model.base_url is required');
  if (!['api-key', 'none'].includes(m.auth_mode ?? 'api-key')) fail('unsupported model.auth_mode');
  if (m.auth_mode === 'none' && (typeof m.api_key !== 'string' || m.api_key !== '')) fail('model.api_key must be empty for anonymous auth');
  if ((m.auth_mode ?? 'api-key') === 'api-key' && (typeof m.api_key !== 'string' || !m.api_key)) fail('model.api_key is required');
  let url;
  try { url = new URL(m.base_url); } catch { fail('invalid model.base_url'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || /[?#]/.test(m.base_url)) fail('invalid model.base_url');
  if (m.auth_mode === 'none' && (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) fail('anonymous auth requires loopback HTTP');
  for (const field of ['context_window', 'max_tokens']) if (!Number.isSafeInteger(m[field]) || m[field] <= 0) fail(`model.${field} must be positive`);
  if (m.max_tokens > m.context_window) fail('max_tokens exceeds context_window');
}
export function validateStart(message) {
  if (message?.type !== 'start' || typeof message.prompt !== 'string' || !message.prompt.trim()) fail('start requires a non-empty prompt');
  const c = message.config;
  if (!c || typeof c.cwd !== 'string' || typeof c.session_dir !== 'string') fail('config.cwd and session_dir are required');
  if (typeof c.system_prompt !== 'string' || !Array.isArray(c.tools)) fail('system_prompt and tools are required');
  validateModel(c.model);
  for (const field of ['max_model_calls', 'max_model_tokens']) if (!Number.isSafeInteger(c.limits?.[field]) || c.limits[field] < 0) fail(`limits.${field} must be a nonnegative integer`);
  if (!Number.isFinite(c.limits.wall_time_seconds) || c.limits.wall_time_seconds <= 0 || c.limits.wall_time_seconds > 2147483) fail('wall_time_seconds must be positive and bounded');
  c.specialists ??= [];
  if (!Array.isArray(c.specialists)) fail('specialists must be an array');
  if (c.specialists.length > 16) fail('specialists must contain at most 16 profiles');
  const specialistIds = new Set();
  const taskTypes = new Set(['paper_analysis', 'method_design', 'coding', 'data_analysis', 'review']);
  for (const s of c.specialists) {
    if (!s || typeof s !== 'object' || typeof s.id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(s.id) || specialistIds.has(s.id)) fail('specialist ids must be unique identifiers');
    if (typeof s.description !== 'string' || !s.description.trim() || s.description.length > 4000 || !Array.isArray(s.task_types) || !s.task_types.length || new Set(s.task_types).size !== s.task_types.length || s.task_types.some(x => typeof x !== 'string' || !taskTypes.has(x))) fail(`invalid specialist ${s.id}`);
    validateModel(s.model);
    specialistIds.add(s.id);
  }
  const names = new Set();
  for (const t of c.tools) {
    if (!t || !/^[A-Za-z_][A-Za-z0-9_-]*$/.test(t.name) || names.has(t.name) || t.name === 'research_delegate') fail('tool names must be unique identifiers and research_delegate is reserved');
    names.add(t.name);
    if (typeof t.description !== 'string' || !t.parameters || Array.isArray(t.parameters) || typeof t.parameters !== 'object') fail(`invalid tool ${t.name}`);
    try { ajv.compile(t.parameters); } catch { fail(`invalid JSON schema for ${t.name}`); }
  }
  return c;
}

// A request is admitted before SDK streamFn touches the provider. Token usage is
// provider-reported: no subsequent request is admitted once its cumulative limit
// is reached. A single in-flight response may exceed that observational limit.
export function guardedStream(streamFn, limits, usage, stopped) {
  return async (...args) => {
    if (stopped()) throw new BridgeError('aborted', 'run aborted');
    if (usage.calls >= limits.max_model_calls || usage.total_tokens >= limits.max_model_tokens) throw new BridgeError('budget_exceeded', 'model budget exhausted');
    usage.calls++;
    return streamFn(...args);
  };
}

// Fail closed instead of accepting SDK's best-effort, lossy transcript parser.
export async function validateSessionFile(path) {
  const invalid = message => { throw new BridgeError('invalid_session', message); };
  const text = await readFile(path, 'utf8');
  const lines = text.split('\n');
  if (lines.at(-1) === '') lines.pop();
  if (!lines.length || lines.some(line => !line.trim())) invalid('session is empty or contains empty records');
  const ids = new Set();
  const types = new Set(['message', 'thinking_level_change', 'model_change', 'usage', 'compaction', 'branch_summary', 'custom', 'label', 'session_info', 'custom_message', 'context_edit']);
  for (const [index, line] of lines.entries()) {
    let entry; try { entry = JSON.parse(line); } catch { invalid(`invalid session JSON at line ${index + 1}`); }
    if (!entry || Array.isArray(entry) || typeof entry !== 'object') invalid('session record must be an object');
    if (typeof entry.timestamp !== 'string' || !Number.isFinite(Date.parse(entry.timestamp))) invalid('invalid session timestamp');
    if (index === 0) {
      if (entry.type !== 'session' || entry.version !== 3 || typeof entry.id !== 'string' || !/^[0-9a-f-]{36}$/i.test(entry.id) || typeof entry.cwd !== 'string') invalid('invalid or unsupported session header');
      continue;
    }
    if (!types.has(entry.type) || typeof entry.id !== 'string' || !entry.id || ids.has(entry.id) || (entry.parentId !== null && !ids.has(entry.parentId))) invalid(`invalid session entry at line ${index + 1}`);
    if (entry.type === 'message' && (!entry.message || !['user', 'assistant', 'system', 'toolResult', 'custom', 'bashExecution', 'compactionSummary', 'branchSummary'].includes(entry.message.role) || (!Array.isArray(entry.message.content) && typeof entry.message.content !== 'string'))) invalid('invalid session message');
    if (entry.type === 'model_change' && (typeof entry.provider !== 'string' || typeof entry.modelId !== 'string')) invalid('invalid model change');
    if (entry.type === 'thinking_level_change' && typeof entry.thinkingLevel !== 'string') invalid('invalid thinking level');
    ids.add(entry.id);
  }
}

function explicitResources(systemPrompt, cwd, agentDir, settingsManager) {
  // The constructor supplies SDK's extension runtime, but reload() is deliberately
  // never called: even noContextFiles still discovers APPEND_SYSTEM.md upstream.
  const runtime = new DefaultResourceLoader({ cwd, agentDir, settingsManager, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true, systemPrompt }).getExtensions().runtime;
  return {
    getExtensions: () => ({ extensions: [], errors: [], runtime }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {}, reload: async () => {},
  };
}

export async function runBridge(input = process.stdin, output = process.stdout) {
  let session, started = false, terminal = false, errorSent = false, stopReason, wallTimer, key = '';
  let task = Promise.resolve();
  let secretVariants = [];
  const controller = new AbortController();
  const pending = new Map();
  const usage = { calls: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 };
  const redact = (value) => {
    for (const variant of secretVariants) value = value.replaceAll(variant, '[REDACTED]');
    return value;
  };
  const clean = value => {
    if (typeof value === 'string') return redact(value);
    if (Array.isArray(value)) return value.map(clean);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([name, item]) => [redact(name), clean(item)]));
    return value;
  };
  const send = (message) => { if (!output.destroyed && (!terminal || message.type === 'error')) output.write(JSON.stringify(clean(message)) + '\n'); };
  const error = (err) => { if (errorSent) return; errorSent = true; send({ type: 'error', code: err.code ?? 'sdk_error', message: redact(String(err.message ?? err)) }); };
  const cancel = (reason = 'aborted') => {
    if (terminal) return;
    stopReason ??= reason;
    controller.abort();
    for (const p of [...pending.values()]) p.reject(new BridgeError(reason, reason));
    void session?.abort().catch(() => {});
  };
  const terminate = (err) => {
    if (terminal) return;
    cancel(err.code ?? 'protocol_error');
    terminal = true;
    error(err);
    clearTimeout(wallTimer);
    rl.close();
    input.pause(); input.destroy();
    session?.dispose();
  };
  const rl = createInterface({ input, crlfDelay: Infinity });
  const onTerm = () => { if (!session) terminate(new BridgeError('aborted', 'aborted')); else cancel('aborted'); };
  process.on('SIGTERM', onTerm);
  process.on('SIGINT', onTerm);
  async function start(message) {
    // Collect credentials before validation as provider/config errors may echo input.
    const secrets = [message?.config?.model?.api_key, ...(Array.isArray(message?.config?.specialists) ? message.config.specialists.map(s => s?.model?.api_key) : [])];
    secretVariants = [...new Set(secrets.filter(k => typeof k === 'string' && k).flatMap(k => {
      const variants = [k];
      for (let depth = 0; depth < 8; depth++) {
        const escaped = JSON.stringify(variants.at(-1)).slice(1, -1);
        if (escaped === variants.at(-1)) break;
        variants.push(escaped);
      }
      return variants;
    }))].sort((a, b) => b.length - a.length);
    const c = validateStart(message);
    key = c.model.api_key;
    const cwd = resolve(c.cwd), sessionDir = resolve(c.session_dir);
    if (!(await stat(cwd)).isDirectory()) fail('cwd must be an existing directory');
    if (c.session_file) await validateSessionFile(resolve(c.session_file));
    if (terminal) return;
    const manager = c.session_file ? SessionManager.open(resolve(c.session_file), sessionDir, cwd) : SessionManager.create(cwd, sessionDir);
    // Redact even provider errors/tool results which unexpectedly echo the credential
    // before SDK appends them to the persistent transcript.
    const persist = manager._persist.bind(manager);
    manager._persist = entry => {
      // _persist initially flushes every buffered record, not only this entry.
      for (const record of manager.fileEntries) {
        const sanitized = clean(record);
        for (const name of Object.keys(record)) delete record[name];
        Object.assign(record, sanitized);
      }
      return persist(entry);
    };
    // No global auth, models.json, settings, project context, skills, or extensions.
    const credentials = { read: async () => undefined, list: async () => [], modify: async (_id, fn) => fn(undefined), delete: async () => {} };
    const runtime = await ModelRuntime.create({ credentials, modelsPath: null, refreshOnCreate: false, allowModelNetwork: false });
    const modelConfig = { id: c.model.id, name: c.model.id, reasoning: false, input: ['text'], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: c.model.context_window, maxTokens: c.model.max_tokens };
    const providerConfig = { api: c.model.api, baseUrl: c.model.base_url,
      authHeader: (c.model.auth_mode ?? 'api-key') !== 'none', models: [modelConfig] };
    const registerLoopback = async (provider, spec, config) => {
      const { streamLoopback } = await import('./loopback-provider.mjs');
      const model = { ...config, api: spec.api, provider, baseUrl: spec.base_url };
      runtime.registerNativeProvider({ id: provider, name: provider, baseUrl: spec.base_url,
        auth: { apiKey: { name: 'Explicit anonymous loopback', resolve: async () => ({ auth: {}, source: 'explicit anonymous loopback' }) } },
        getModels: () => [model], stream: streamLoopback, streamSimple: streamLoopback });
    };
    if (c.model.auth_mode === 'none') await registerLoopback('p2r', c.model, modelConfig);
    else runtime.registerProvider('p2r', providerConfig);
    if ((c.model.auth_mode ?? 'api-key') !== 'none') {
      await runtime.setRuntimeApiKey('p2r', key); // Runtime-key API stores a literal key without config expression resolution.
    }
    const specialistModels = new Map();
    for (const [index, specialist] of c.specialists.entries()) {
      const provider = `specialist_${index}`;
      const sm = specialist.model;
      const model = { id: sm.id, name: sm.id, reasoning: false, input: ['text'], api: sm.api,
        provider, baseUrl: sm.base_url, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: sm.context_window, maxTokens: sm.max_tokens };
      const pc = { api: sm.api, baseUrl: sm.base_url, authHeader: sm.auth_mode !== 'none', models: [model] };
      if (sm.auth_mode === 'none') await registerLoopback(provider, sm, model);
      else runtime.registerProvider(provider, pc);
      if (sm.auth_mode !== 'none') await runtime.setRuntimeApiKey(provider, sm.api_key);
      specialistModels.set(specialist.id, { specialist, model: runtime.getModel(provider, sm.id), provider });
    }
    const rawRuntimeStream = runtime.stream.bind(runtime);
    runtime.stream = guardedStream((model, context, options) => rawRuntimeStream(model, clean(context), { ...options, maxRetries: 0 }), c.limits, usage, () => Boolean(stopReason));
    const rawRuntimeSimple = runtime.streamSimple.bind(runtime);
    runtime.streamSimple = guardedStream((model, context, options) => rawRuntimeSimple(model, clean(context), { ...options, maxRetries: 0 }), c.limits, usage, () => Boolean(stopReason));
    const account = (u = {}) => {
      const input = (u.input ?? 0) + (u.cacheRead ?? 0) + (u.cacheWrite ?? 0);
      usage.input_tokens += input;
      usage.output_tokens += u.output ?? 0;
      usage.total_tokens += u.totalTokens ?? (input + (u.output ?? 0));
    };
    const settings = SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false }, cacheWarming: 'off' });
    const roster = c.specialists.map(s => ({ id: s.id, description: s.description, task_types: s.task_types, model: s.model.id }));
    const systemPrompt = c.system_prompt + (roster.length ? `\nResearch specialists (choose autonomously when useful via research_delegate; advisory output is unverified, not execution evidence; execute and verify with your original tools):\n${JSON.stringify(roster)}` : '');
    const loader = explicitResources(clean(systemPrompt), cwd, join(sessionDir, '.isolated'), settings);
    let delegationQueue = Promise.resolve();
    // Pi coerces tool arguments before execute(); retain the wire values so our
    // stricter all-strings boundary cannot be weakened by SDK coercion.
    const delegationArguments = new Map();
    const delegationParameters = {
      type: 'object', additionalProperties: false,
      required: ['specialist_id', 'task_type', 'instruction', 'context', 'reason'],
      properties: {
        specialist_id: { type: 'string', enum: roster.map(s => s.id), minLength: 1, maxLength: 128 },
        task_type: { type: 'string', enum: ['paper_analysis', 'method_design', 'coding', 'data_analysis', 'review'] },
        instruction: { type: 'string', minLength: 1, maxLength: 12000 },
        context: { type: 'string', maxLength: 24000 },
        reason: { type: 'string', minLength: 1, maxLength: 2000 },
      },
    };
    const validDelegation = roster.length ? ajv.compile(delegationParameters) : null;
    const specialistTool = roster.length ? {
      name: 'research_delegate', label: 'research_delegate',
      description: 'Request one isolated advisory inference from a specialist listed in the system prompt. No tools or recursive delegation; verify advice with your own tools.',
      parameters: delegationParameters,
      execute: async (_id, args, toolSignal) => {
        if (!validDelegation(args) || !args.instruction.trim() || !args.reason.trim()) throw new BridgeError('protocol_error', 'invalid delegation arguments');
        const item = specialistModels.get(args.specialist_id);
        if (!item || !item.specialist.task_types.includes(args.task_type)) throw new BridgeError('protocol_error', 'specialist is not allowed for task type');
        const signal = toolSignal ? AbortSignal.any([controller.signal, toolSignal]) : controller.signal;
        if (stopReason || signal.aborted) throw new BridgeError('aborted', 'delegation aborted');
        const { specialist, model } = item;
        args = clean(args);
        const eventBase = { specialist_id: specialist.id, task_type: args.task_type, model: model.id, reason: args.reason, evidence_status: 'unverified_advisory' };
        let admitted = false, finalMessage = { role: 'assistant', content: [], stopReason: 'error', usage: {} };
        try {
          // Same admission guard as the main agent. Invoke inside the serial queue;
          // emit billing events only AFTER admission, even when setup throws.
          const stream = await guardedStream(() => {
            admitted = true;
            send({ type: 'event', event: { type: 'research_delegation_started', ...eventBase } });
            send({ type: 'event', event: { type: 'turn_start', delegated: true, specialist_id: specialist.id, model: model.id } });
            return rawRuntimeSimple(model, clean({
              systemPrompt: `You are a research specialist. Give advisory analysis only, not evidence. Do not use credentials, tools, or recursive delegation.\nProfile: ${specialist.description}`,
              messages: [{ role: 'user', content: `${args.instruction}\n\nContext:\n${args.context}`, timestamp: Date.now() }],
            }), { reasoning: 'off', maxTokens: model.maxTokens, maxRetries: 0, signal });
          }, c.limits, usage, () => Boolean(stopReason) || signal.aborted)();
          for await (const ev of stream) finalMessage = ev.message ?? ev.error ?? ev.partial ?? finalMessage;
          finalMessage = clean(finalMessage);
          const text = (finalMessage.content ?? []).filter(x => x.type === 'text').map(x => x.text).join('').slice(0, 64000);
          const u = finalMessage.usage ?? {};
          const reportedTokens = u.totalTokens ?? ((u.input ?? 0) + (u.output ?? 0) + (u.cacheRead ?? 0) + (u.cacheWrite ?? 0));
          if (signal.aborted || finalMessage.stopReason === 'aborted') throw new BridgeError('aborted', 'delegation aborted');
          if (!['stop', 'length'].includes(finalMessage.stopReason) || !text.trim() || !(reportedTokens > 0)) throw new BridgeError('model_error', 'specialist inference failed or usage is unknown');
          send({ type: 'event', event: { type: 'research_delegation_completed', ...eventBase, text } });
          return { content: [{ type: 'text', text: JSON.stringify(clean({ specialist_id: specialist.id, task_type: args.task_type, model: model.id, text, evidence_status: 'unverified_advisory' })) }], details: {} };
        } catch (err) {
          if (!admitted) { if (err.code === 'budget_exceeded') stopReason ??= 'budget_exceeded'; throw err; }
          finalMessage = { ...finalMessage, stopReason: signal.aborted || err.code === 'aborted' ? 'aborted' : 'error' };
          // Unknown consumption cannot be erased by a retry or a later success.
          stopReason ??= finalMessage.stopReason === 'aborted' ? 'aborted' : 'model_error';
          throw new BridgeError(stopReason, 'specialist inference failed');
        } finally {
          if (admitted) {
            account(finalMessage.usage);
            const text = (finalMessage.content ?? []).filter(x => x.type === 'text').map(x => x.text).join('').slice(0, 64000);
            send({ type: 'event', event: { type: 'message_end', delegated: true, specialist_id: specialist.id, message: { role: 'assistant', model: model.id, content: [{ type: 'text', text }], usage: finalMessage.usage ?? {}, stopReason: finalMessage.stopReason } } });
          }
        }
      },
    } : null;
    const toolDefs = specialistTool ? [...c.tools, specialistTool] : c.tools;
    const customTools = toolDefs.map(t => ({ ...t, label: t.name, execute: async (id, args, signal) => {
      if (t.name === 'research_delegate') {
        const originalArgs = delegationArguments.has(id) ? delegationArguments.get(id) : args;
        const delegated = delegationQueue.then(() => t.execute(id, originalArgs, signal));
        delegationQueue = delegated.catch(() => {});
        return delegated;
      }
      if (stopReason || signal?.aborted) throw new BridgeError('aborted', 'tool aborted');
      return new Promise((resolveTool, rejectTool) => {
        const finish = (fn, value) => { clearTimeout(timer); signal?.removeEventListener('abort', abort); pending.delete(id); fn(value); };
        const abort = () => finish(rejectTool, new BridgeError('aborted', 'tool aborted'));
        const timer = setTimeout(() => { finish(rejectTool, new BridgeError('tool_timeout', 'parent tool response timed out')); cancel('tool_timeout'); }, Math.min(300000, c.limits.wall_time_seconds * 1000));
        signal?.addEventListener('abort', abort, { once: true });
        pending.set(id, { resolve: response => {
          const safeResult = clean(response.result);
          if (response.is_error) finish(rejectTool, new Error(typeof safeResult === 'string' ? safeResult : JSON.stringify(safeResult)));
          else finish(resolveTool, { content: [{ type: 'text', text: typeof safeResult === 'string' ? safeResult : JSON.stringify(safeResult) }], details: {} });
        }, reject: err => finish(rejectTool, err) });
        send({ type: 'tool_request', id, name: t.name, arguments: args });
      });
    } }));
    ({ session } = await createAgentSession({ cwd, agentDir: join(sessionDir, '.isolated'), modelRuntime: runtime, model: runtime.getModel('p2r', c.model.id), thinkingLevel: 'off', settingsManager: settings, resourceLoader: loader, sessionManager: manager, noTools: 'all', tools: toolDefs.map(t => t.name), customTools }));
    if (terminal) { session.dispose(); return; }
    let lastText = '', providerError;
    session.subscribe(event => {
      if (event.type === 'message_end' && event.message.role === 'assistant') {
        const m = event.message;
        delegationArguments.clear();
        for (const part of m.content ?? []) {
          if (part.type === 'toolCall' && part.name === 'research_delegate') delegationArguments.set(part.id, structuredClone(part.arguments));
        }
        account(m.usage);
        lastText = m.content.filter(x => x.type === 'text').map(x => x.text).join('');
        if (m.stopReason === 'error' || m.stopReason === 'aborted') providerError = m.errorMessage ?? 'model request failed';
      }
      if (event.type !== 'message_update') send({ type: 'event', event });
    });
    send({ type: 'ready', session_file: manager.getSessionFile(), session_id: manager.getSessionId() });
    wallTimer = setTimeout(() => cancel('wall_time_exceeded'), c.limits.wall_time_seconds * 1000);
    try {
      if (stopReason) throw new BridgeError(stopReason, stopReason);
      await session.prompt(clean(message.prompt), { expandPromptTemplates: false });
      if (stopReason) throw new BridgeError(stopReason, stopReason);
      if (providerError) throw new BridgeError(providerError.includes('model budget exhausted') ? 'budget_exceeded' : 'model_error', providerError);
      send({ type: 'result', text: lastText, usage, session_file: manager.getSessionFile() });
    } finally {
      terminal = true;
      clearTimeout(wallTimer);
      session.dispose();
      rl.close(); input.pause(); input.destroy();
    }
  }
  rl.on('line', line => {
    if (terminal) return;
    try {
      if (Buffer.byteLength(line) > 8 * 1024 * 1024) fail('JSONL record too large');
      let m; try { m = JSON.parse(line); } catch { fail('invalid JSON'); }
      if (!started) {
        if (m?.type === 'abort') { terminate(new BridgeError('aborted', 'aborted before start')); return; }
        started = true;
        task = start(m).catch(err => { if (!terminal) terminate(err); else error(err); });
      } else if (m?.type === 'abort') cancel();
      else if (m?.type === 'steer' && typeof m.message === 'string' && m.message.trim() && session) void session.steer(clean(m.message), undefined, { expandPromptTemplates: false }).catch(terminate);
      else if (m?.type === 'tool_result' && pending.has(m.id) && Object.hasOwn(m, 'result')) pending.get(m.id).resolve(m);
      else fail('unexpected command or unknown tool result id');
    } catch (err) { terminate(err); }
  });
  rl.on('close', () => { if (!terminal) { if (started) cancel('stdin_eof'); else terminate(new BridgeError('stdin_eof', 'stdin closed before start')); } });
  await new Promise(resolveDone => { rl.once('close', resolveDone); });
  await task;
  process.off('SIGTERM', onTerm); process.off('SIGINT', onTerm);
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  runBridge().catch(() => { process.exitCode = 1; });
}
