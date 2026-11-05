import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import vm from 'node:vm';
import * as helpers from '../src/features/autonomous/config.ts';
import * as stages from '../src/features/autonomous/stages.ts';
import { GuidedApiError } from '../src/api-client/guided.ts';

async function harness(options = {}) {
  const source = await readFile(new URL('../src/features/autonomous/AutonomousPage.tsx', import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const slots = []; let cursor = 0; let effects = []; const calls = []; const timers = []; let resolveControl;
  const task = id => ({ id, project_id: 'A', objective: id, state: 'paused', usage: {}, updated_at: '', pending_decision: null });
  const api = {
    initializeSession: async () => {}, capabilities: async () => [], profiles: async () => options.profiles ?? [],
    config: async () => { if (options.config) return { config: structuredClone(options.config), version: 1 }; throw new GuidedApiError('missing', 404, 'NOT_FOUND'); },
    saveProfile: async body => { calls.push({ profile: body }); const { api_key, reuse_credentials_from, ...saved } = body; return { ...saved, secret_configured: !!api_key || !!reuse_credentials_from }; },
    tasks: async () => [task('ta'), task('tb')], task: async id => task(id), events: async () => ({ events: [], next_cursor: 0 }),
    saveConfig: async (_id, config) => { options.config = structuredClone(config); return { config, version: 2 }; },
    start: async (id, body) => { calls.push({ id, body }); return task('created'); },
    control: () => new Promise(resolve => { resolveControl = resolve; }),
  };
  const react = {
    createElement: (type, props, ...children) => typeof type === 'function' ? type({ ...props, children }) : ({ type, props: props ?? {}, children }),
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || deps.some((d, j) => d !== slots[i].deps[j])) { slots[i]?.cleanup?.(); slots[i] = { deps }; effects.push(() => { slots[i].cleanup = fn(); }); } },
  };
  const exports = {};
  const context = { exports, React: react, require: name => name === 'react' ? react : name.includes('api-client/autonomous') ? { createAutonomousApi: () => api } : name.includes('api-client/guided') ? { GuidedApiError } : name === './config' ? helpers : name === './stages' ? stages : {}, window: { location: { hash: '#/autonomous?project=A&task=ta' }, history: { replaceState() {} } }, AbortController, URLSearchParams, setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {} };
  vm.runInNewContext(code, context);
  const guided = { readArray: async path => ({ items: path === '/projects' ? [{ id: 'A' }, { id: 'B' }] : [{ id: 'paper' }] }), read: async () => ({ paper_version_id: 'pv' }) };
  let tree;
  const render = () => { cursor = 0; tree = exports.default({ guided }); const pending = effects; effects = []; pending.forEach(fn => fn()); return tree; };
  const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); render(); };
  const nodes = () => { const result = []; const visit = n => { if (Array.isArray(n)) return n.forEach(visit); if (n && typeof n === 'object') { result.push(n); visit(n.children); } }; visit(tree); return result; };
  render(); await settle(); await settle();
  return { slots, render, settle, nodes, calls, api, poll: () => timers.shift()(), resolve: value => resolveControl(value), task };
}
test('switching A to B clears A protocol before constructing B start body', async () => {
  const h = await harness();
  h.slots[11] = '{"owner":"A"}'; // protocol state in the actual component
  h.slots[0] = 'B'; h.render(); await h.settle(); await h.settle();
  h.slots[4].model = { provider_profile: 'm', credential_ref: 'profile:m' }; h.slots[4].experiment.objective = 'B goal'; h.slots[12] = true; h.render();
  const start = h.nodes().find(n => n.type === 'button' && n.children.includes('启动自主实验'));
  assert.equal(start.props.disabled, false); start.props.onClick(); await h.settle();
  assert.equal(h.calls[0].id, 'B'); assert.equal(h.calls[0].body.protocol, undefined);
});
function optionLabel(h, id) {
  return h.nodes().find(n => n.type === 'option' && n.props.value === id).children.join('');
}
test('poll updates selected task option alongside monitor without changing other tasks', async () => {
  const h = await harness();
  h.api.task = async id => ({ ...h.task(id), state: 'running' });
  h.poll(); await h.settle();
  assert.equal(h.slots[15].state, 'running');
  assert.match(optionLabel(h, 'ta'), /运行中/);
  assert.match(optionLabel(h, 'tb'), /已暂停/);
});
test('workbench metadata and stats display shared main-plus-specialist calls with legacy fallback', async () => {
  const h = await harness();
  const mainCalls = 2; const specialistCalls = 3;
  for (const [usage, expected] of [
    [{ calls: mainCalls + specialistCalls }, '5'],
    [{ calls: 7, model_calls: 2 }, '7'],
    [{ calls: 0, model_calls: 9 }, '0'],
    [{ model_calls: 4 }, '4'],
    [{}, '—'],
  ]) {
    h.api.task = async id => ({ ...h.task(id), usage });
    h.poll(); await h.settle();
    const workbench = h.nodes().find(n => n.props.meta);
    const metaCalls = workbench.props.meta.children.find(n => n.type === 'span' && n.children.includes('模型调用 '));
    assert.equal(metaCalls.children.join(''), `模型调用 ${expected}`);
    const stat = h.nodes().find(n => n.props.className === 'wb-stat' && n.children.some(c => c?.type === 'span' && c.children.includes('模型调用')));
    assert.equal(stat.children.find(n => n.type === 'b').children.join(''), expected);
  }
});

test('late poll cannot change monitor or task options after project scope changes', async () => {
  const h = await harness(); let resolvePoll;
  h.api.task = () => new Promise(resolve => { resolvePoll = resolve; });
  h.poll();
  h.slots[0] = 'B'; h.render(); await h.settle();
  const before = h.slots[13];
  resolvePoll({ ...h.task('ta'), state: 'stopped' }); await h.settle();
  assert.equal(h.slots[13], before);
  assert.notEqual(h.slots[15]?.state, 'stopped');
});
test('control response updates selected task option alongside monitor', async () => {
  const h = await harness();
  h.nodes().find(n => n.type === 'button' && n.children.includes('停止')).props.onClick();
  h.resolve({ ...h.task('ta'), state: 'stopped' }); await h.settle();
  assert.equal(h.slots[15].state, 'stopped');
  assert.match(optionLabel(h, 'ta'), /已停止/);
  assert.match(optionLabel(h, 'tb'), /已暂停/);
});
for (const label of ['暂停', '发送指令', '批准此方案并继续']) {
  test(`late A ${label} response cannot replace B monitor; busy disables task selector`, async () => {
    const h = await harness();
    h.slots[17] = 'A instruction';
    if (label === '批准此方案并继续') h.slots[15] = { ...h.task('ta'), pending_decision: { id: 'decision-A', kind: 'protocol_approval', proposed: { protocol: {}, sha256: 'abc' }, changed: [], reason: 'review' } };
    h.render();
    h.nodes().find(n => n.type === 'button' && n.children.includes(label)).props.onClick(); h.render();
    assert.equal(h.nodes().find(n => n.type === 'select' && n.props.value === 'ta').props.disabled, true);
    h.slots[14] = 'tb'; h.render(); await h.settle();
    h.slots[17] = 'B instruction';
    h.resolve({ ...h.task('ta'), state: 'stopped' }); await h.settle();
    assert.equal(h.slots[15].id, 'tb');
    assert.equal(h.slots[13].find(t => t.id === 'ta').state, 'paused');
    assert.equal(h.slots[17], 'B instruction');
  });
}

function field(h, title, index = 0) {
  const labels = h.nodes().filter(n => n.type === 'label' && n.children.some(c => c?.type === 'span' && c.children.includes(title)));
  return labels[index]?.children.flat(Infinity).find(c => c && ['input', 'select', 'textarea'].includes(c.type));
}
function change(h, title, value, index = 0) { field(h, title, index).props.onChange({ target: { value } }); h.render(); }
function click(h, text) { h.nodes().find(n => n.type === 'button' && n.children.includes(text)).props.onClick(); h.render(); }
const model = { id: 'main', model: 'main-model', api: 'openai-completions', base_url: 'https://example.test/v1', secret_configured: true };
function savedConfig() { const config = helpers.defaultConfig('A'); config.model = helpers.modelReference(model); config.experiment.objective = 'Research'; return config; }

test('expert editor adds, validates, saves, reloads and deletes without changing main model', async () => {
  const h = await harness({ config: savedConfig(), profiles: [model, { ...model, id: 'expert', model: 'expert-model' }] });
  click(h, '增加科研专家');
  change(h, '专家 ID', 'method-advisor');
  change(h, '专家模型配置', 'expert');
  change(h, '专长描述（供主模型选择专家）', 'Experimental design');
  click(h, '校验配置'); await h.settle();
  assert.match(h.slots[20], /至少一种/);
  const taskType = h.nodes().find(n => n.type === 'label' && n.children.includes('方法设计')).children.flat(Infinity).find(n => n?.type === 'input');
  taskType.props.onChange({ target: { checked: true } }); h.render();
  click(h, '校验配置'); await h.settle(); assert.equal(h.slots[20], '');
  click(h, '保存配置'); await h.settle();
  change(h, '专长描述（供主模型选择专家）', 'Unsaved edit');
  click(h, '重新加载配置'); await h.settle();
  assert.equal(field(h, '专长描述（供主模型选择专家）').props.value, 'Experimental design');
  assert.equal(field(h, '专家模型配置').props.value, 'expert');
  assert.equal(field(h, '主模型（编排器）').props.value, 'main');
  assert.deepEqual([...h.slots[4].specialists[0].task_types], ['method_design']);
  const remove = h.nodes().find(n => n.type === 'button' && n.children.includes('删除专家 ')); remove.props.onClick(); h.render();
  assert.equal(h.slots[4].specialists.length, 0);
});

test('profile UI defaults to key mode, clears saved key and reuses an existing endpoint key', async () => {
  const h = await harness({ config: savedConfig(), profiles: [model] });
  assert.equal(field(h, '认证方式').props.value, 'api-key');
  change(h, '配置 ID', 'expert'); change(h, '模型名称', 'expert-model'); change(h, 'Base URL', model.base_url);
  change(h, 'API Key（仅本次内存）', 'private-secret');
  click(h, '保存模型连接'); await h.settle();
  assert.equal(h.calls[0].profile.api_key, 'private-secret');
  assert.equal(field(h, 'API Key（仅本次内存）').props.value, '');
  assert.equal(JSON.stringify(h.slots[4]).includes('private-secret'), false);
  assert.equal(field(h, '主模型（编排器）').props.value, 'main');
  change(h, '配置 ID', 'reviewer'); change(h, '复用已有配置凭据（同端点 / 协议）', 'main');
  click(h, '保存模型连接'); await h.settle();
  assert.equal(h.calls[1].profile.reuse_credentials_from, 'main');
  assert.equal(Object.hasOwn(h.calls[1].profile, 'api_key'), false);
  assert.equal(field(h, '复用已有配置凭据（同端点 / 协议）').props.value, '');
  change(h, '认证方式', 'none'); change(h, 'Base URL', 'http://localhost:3425/v1');
  assert.equal(field(h, 'API Key（仅本次内存）'), undefined);
  click(h, '保存并用作主模型'); await h.settle();
  assert.equal(h.calls[2].profile.auth_mode, 'none');
  assert.equal(Object.hasOwn(h.calls[2].profile, 'api_key'), false);
  assert.equal(field(h, '主模型（编排器）').props.value, 'reviewer');
});
