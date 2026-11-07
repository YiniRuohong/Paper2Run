import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';
import vm from 'node:vm';
import { ApiError } from '../src/api-client/client.ts';
import * as config from '../src/features/autonomous/config.ts';
import * as errors from '../src/features/autonomous/errors.ts';
import * as launch from '../src/features/autonomous/launch.ts';
import * as probe from '../src/features/autonomous/probe.ts';
import * as draft from '../src/features/flow/draft.ts';
import * as draftData from '../src/features/flow/draftData.ts';
import * as timeline from '../src/features/flow/timeline.ts';

// Shallow-render one setup step in a VM: React hooks and UI children are
// stubbed, the draft helpers are real. Only the step's own save sequence runs.

const box = name => function Stub(props) { return { type: name, props: props ?? {}, children: props?.children }; };

async function mount(file, props) {
  const source = await readFile(new URL(`../src/features/flow/steps/${file}`, import.meta.url), 'utf8');
  const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const slots = []; let cursor = 0; let effects = [];
  const react = {
    Fragment: 'fragment',
    createElement: (type, p, ...children) => typeof type === 'function' ? type({ ...p, children }) : ({ type, props: p ?? {}, children }),
    useState(initial) { const i = cursor++; if (!(i in slots)) slots[i] = typeof initial === 'function' ? initial() : initial; return [slots[i], value => { slots[i] = typeof value === 'function' ? value(slots[i]) : value; }]; },
    useRef(initial) { const i = cursor++; return slots[i] ??= { current: initial }; },
    useCallback(fn) { cursor++; return fn; },
    useEffect(fn, deps) { const i = cursor++; if (!slots[i] || deps.some((d, j) => d !== slots[i].deps[j])) { slots[i] = { deps }; effects.push(fn); } },
  };
  const modules = {
    react,
    '../draft': draft,
    '../draftData': draftData,
    '../timeline': timeline,
    '../StepFrame': { default: box('StepFrame') },
    '../../../ui/Controls': { Segmented: box('Segmented'), Switch: box('Switch') },
    '../../../ui/Icon': { Icon: () => null },
    '../../autonomous/config': config,
    '../../autonomous/errors': errors,
    '../../autonomous/launch': launch,
    '../../autonomous/probe': probe,
    '../../autonomous/TargetProbeView': { default: box('TargetProbeView') },
  };
  const exports = {};
  vm.runInNewContext(code, { exports, React: react, require: name => modules[name] ?? {}, AbortController, window: { location: { replace() {} } } });
  let tree; let current = props;
  const render = (next = {}) => { current = { ...current, ...next }; cursor = 0; tree = exports.default(current); const pending = effects; effects = []; pending.forEach(fn => fn()); return tree; };
  const settle = async () => { await new Promise(resolve => setImmediate(resolve)); render(); };
  const nodes = () => {
    const out = [];
    const visit = n => { if (Array.isArray(n)) return n.forEach(visit); if (n && typeof n === 'object') { out.push(n); visit(n.children); } };
    visit(tree);
    return out;
  };
  render(); await settle();
  return { render, settle, nodes, frame: () => nodes().find(n => n.type === 'StepFrame'), text: () => JSON.stringify(tree) };
}

/** The page's act(): runs the work and records the error it would show. */
function pageAct() {
  const shown = [];
  const act = async work => { try { await work(); return true; } catch (e) { shown.push(e); return false; } };
  return { act, shown };
}

const saved = (version, over = {}) => ({ version, config: { ...config.defaultConfig('p1'), ...over } });

test('objective: a failed config save after the description PATCH re-reads the page, so the retry sends the fresh version', async () => {
  const patches = []; let refreshed = 0; let saves = 0;
  const { act, shown } = pageAct();
  const step = await mount('ObjectiveStep.tsx', {
    projectId: 'p1', projectVersion: 3, initial: '复现表 2', hasConfig: true, busy: false, feedback: null, act,
    api: { config: async () => saved(1), saveConfig: async () => { saves += 1; if (saves === 1) throw new ApiError('boom', 500, 'INTERNAL'); return saved(2); } },
    papers: { updateProjectDescription: async (_id, text, version) => { patches.push(version); return {}; } },
    refresh: async () => { refreshed += 1; },
    onDone: async () => {},
  });
  step.frame().props.next.onClick(); await step.settle();
  assert.deepEqual(patches, [3]);
  assert.equal(shown.length, 1, 'the save error stays visible');
  assert.equal(refreshed, 1, 'the page re-reads project version and profiles after the failure');

  step.render({ projectVersion: 4 }); // what the page passes after the re-read
  step.frame().props.next.onClick(); await step.settle();
  assert.deepEqual(patches, [3, 4], 'the retry uses the refreshed project version');
  assert.equal(shown.length, 1);
  assert.equal(refreshed, 1, 'a successful save does not re-read through refresh');
});

test('model: a saved connection whose config save failed re-reads the page, so the key need not be typed again', async () => {
  const profilesSaved = []; const configsSaved = []; let refreshed = 0; let step;
  const { act, shown } = pageAct();
  step = await mount('ModelStep.tsx', {
    projectId: 'p1', config: null, profiles: [], connectionLost: false, objective: '复现表 2', busy: false, feedback: null, act,
    api: {
      config: async () => null,
      saveConfig: async (_id, cfg) => { configsSaved.push(cfg); if (configsSaved.length === 1) throw new ApiError('boom', 500, 'INTERNAL'); return { version: 1, config: cfg }; },
      saveProfile: async body => { profilesSaved.push(body); const { api_key, ...rest } = body; return { ...rest, secret_configured: !!api_key }; },
    },
    // The page's reload: the connection saved before the failure is now listed.
    refresh: async () => { refreshed += 1; const { api_key, ...rest } = profilesSaved.at(-1); step.render({ profiles: [{ ...rest, secret_configured: true }] }); },
    onDone: async () => {},
  });
  const input = match => step.nodes().find(n => n.type === 'input' && match(n.props));
  input(p => p.type === 'url').props.onChange({ target: { value: 'https://api.example.com/v1' } });
  input(p => p.placeholder === 'qwen-max').props.onChange({ target: { value: 'qwen-max' } });
  input(p => p.type === 'password').props.onChange({ target: { value: 'sk-test' } });
  step.render();
  step.frame().props.next.onClick(); await step.settle();
  assert.equal(profilesSaved.length, 1);
  assert.equal(shown.length, 1, 'the save error stays visible');
  assert.equal(refreshed, 1, 'the saved connection is read back and offered on retry');

  assert.ok(!step.nodes().some(n => n.type === 'input' && n.props.type === 'password'), 'the retry offers the saved connection, not an empty key field');
  step.frame().props.next.onClick(); await step.settle();
  assert.equal(profilesSaved.length, 1, 'the key is not sent again');
  assert.equal(configsSaved.length, 2);
  assert.equal(configsSaved[1].model.provider_profile, profilesSaved[0].id);
  assert.equal(shown.length, 1);
});

const spec = (id, profile) => ({ id, description: 'd', task_types: ['coding'], model: { provider_profile: profile, credential_ref: `profile:${profile}` } });
const main = { id: 'main', base_url: 'https://api.example.com/v1', model: 'qwen-max', api: 'openai-completions', secret_configured: true };

async function launchStep(over) {
  const cfg = { ...config.defaultConfig('p1'), model: { provider_profile: 'main', credential_ref: 'profile:main' }, ...over };
  const { act } = pageAct();
  const step = await mount('LaunchStep.tsx', {
    projectId: 'p1', paper: { paperId: 'pa', versionId: 'v1', title: 'T', pdf: false, textStatus: 'none', pageCount: null }, paperText: null,
    saved: { version: 1, config: cfg }, profile: main, profiles: [main], objective: '复现表 2', busy: false, feedback: null, act,
    api: { config: async () => null, saveConfig: async () => ({ version: 2, config: cfg }), probeTarget: () => new Promise(() => {}), start: async () => ({ id: 't1' }) },
    onSaved: async () => {},
  });
  const authorize = step.nodes().filter(n => n.type === 'Switch').at(-1);
  authorize.props.onChange(true); step.render();
  return step;
}

test('launch: hidden v3 specialists and Skills are named read-only, and a lost specialist connection blocks the start', async () => {
  const lost = await launchStep({ specialists: [spec('s1', 'gone')], harness: { adapter: 'pi', skills: ['a', 'b'] } });
  assert.ok(lost.text().includes('沿用旧配置：1 位科研专家 · 2 个 Skills'));
  assert.equal(lost.frame().props.next.disabled, '旧配置中的科研专家模型连接已失效，无法启动（应用重启后需重新配置）');

  const ok = await launchStep({ specialists: [spec('s1', 'main')], harness: { adapter: 'pi', skills: [] } });
  assert.ok(ok.text().includes('沿用旧配置：1 位科研专家'));
  assert.equal(ok.frame().props.next.disabled, undefined, 'usable specialist connections do not block');

  const plain = await launchStep({});
  assert.ok(!plain.text().includes('沿用旧配置'), 'no line when there is nothing hidden');
  assert.equal(plain.frame().props.next.disabled, undefined);
  assert.ok(!plain.nodes().some(n => n.type === 'button' && JSON.stringify(n).includes('专家')), 'no control edits or removes specialists');
});
