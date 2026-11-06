import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { TOAST_MS, toastLifetime } from '../src/ui/toastTiming.ts';

const read = rel => readFile(new URL(`../src/${rel}`, import.meta.url), 'utf8');
const rule = (css, selector) => {
  const m = css.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{[^}]*\\}`));
  assert.ok(m, `${selector} rule exists`);
  return m[0];
};

test('1: links styled as buttons never underline', async () => {
  const css = await read('ui/ui.css');
  assert.match(rule(css, '.btn'), /text-decoration:\s*none/);
  assert.match(rule(css, '.btn:hover'), /text-decoration:\s*none/);
});

test('2: settings no longer offers 新建实验', async () => {
  const settings = await read('features/settings/Settings.tsx');
  assert.doesNotMatch(settings, /#\/new/);
  assert.doesNotMatch(settings, /自主实验/);
});

test('3: roomier sidebar sizes', async () => {
  assert.match(await read('styles/tokens.css'), /--side-w:\s*272px/);
  const css = await read('shell/shell.css');
  const item = rule(css, '.side-item');
  assert.match(item, /min-height:\s*42px/);
  assert.match(item, /font-size:\s*15px/);
  assert.match(item, /gap:\s*12px/);
  assert.match(item, /padding:\s*0 12px/);
  assert.match(item, /margin:\s*3px 0/);
  assert.match(rule(css, '.side-group + .side-group'), /margin-top:\s*26px/);
  assert.match(rule(css, '.side-label'), /font-size:\s*13px/);
  const sub = rule(css, '.side-sub-item');
  assert.match(sub, /min-height:\s*36px/);
  assert.match(sub, /font-size:\s*14px/);
  assert.match(rule(css, '.side-brand b'), /font-size:\s*16px/);
});

test('4: sidebar collapse animates, the sidebar stays mounted but inert, reduced motion is honoured', async () => {
  const css = await read('shell/shell.css');
  assert.match(rule(css, '.shell'), /transition:\s*grid-template-columns var\(--dur-base\) var\(--ease\)/);
  assert.match(rule(css, '.shell.side-closed'), /grid-template-columns:\s*0px/);
  assert.match(css, /prefers-reduced-motion: reduce[\s\S]*transition: none/);
  const shell = await read('shell/AppShell.tsx');
  assert.doesNotMatch(shell, /\{sideOpen && <Sidebar/);
  assert.match(shell, /<Sidebar open=\{sideOpen\}/);
  const side = await read('shell/Sidebar.tsx');
  assert.match(side, /inert/);
  assert.match(side, /'aria-hidden': true/);
  const flow = await read('features/flow/flow.css');
  assert.match(rule(flow, '.flow-page'), /animation:\s*flow-enter 280ms var\(--ease-expo\)/);
  assert.match(flow, /@keyframes flow-enter/);
  assert.match(flow, /prefers-reduced-motion: reduce\) \{ \.flow-page \{ animation: none/);
});

test('5: the app shell header has no 新建实验 or refresh button', async () => {
  const shell = await read('shell/AppShell.tsx');
  assert.doesNotMatch(shell, /main-actions/);
  assert.doesNotMatch(shell, /<a [^>]*href="#\/new"/);
  assert.doesNotMatch(shell, /<Icon name="plus"/);
  assert.doesNotMatch(shell, /aria-label="刷新"/);
  assert.match(shell, /useRecentTasks/);
});

test('6: step feedback and errors are toasts, not inline callouts', async () => {
  const frame = await read('features/flow/StepFrame.tsx');
  assert.doesNotMatch(frame, /feedback/);
  assert.doesNotMatch(frame, /ui-callout/);
  const toast = await read('ui/Toast.tsx');
  assert.match(toast, /role=\{item\.tone === 'bad' \? 'alert' : 'status'\}/);
  assert.match(toast, /aria-label="关闭提示"/);
  const css = await read('ui/ui.css');
  const stack = rule(css, '.ui-toasts');
  assert.match(stack, /position:\s*fixed/);
  assert.match(stack, /right:\s*24px/);
  assert.match(stack, /bottom:\s*24px/);
  assert.match(css, /prefers-reduced-motion: reduce\)[\s\S]*\.ui-toast \{ animation: none/);
  const draft = await read('features/flow/DraftPage.tsx');
  assert.match(draft, /<ToastStack/);
  assert.match(draft, /loadFailed \? <div className="ui-callout tone-bad flow-error" role="alert">/);
  assert.doesNotMatch(draft, /error && <div className="ui-callout/);
});

test('6: toasts auto-dismiss after about six seconds, errors stay', () => {
  assert.equal(TOAST_MS, 6000);
  assert.equal(toastLifetime('ok'), 6000);
  assert.equal(toastLifetime('idle'), 6000);
  assert.equal(toastLifetime('bad'), null);
});

test('7: the overview is a centred, roomier column', async () => {
  const css = await read('features/overview/overview.css');
  assert.match(rule(css, '.page-overview'), /max-width:\s*960px/);
  assert.match(rule(css, '.overview .ui-card-title'), /font-size:\s*22px/);
  assert.match(rule(css, '.overview .ui-card-sub'), /font-size:\s*15px/);
  assert.match(rule(css, '.overview .ui-row'), /min-height:\s*64px/);
  assert.match(rule(css, '.overview .ui-row-title'), /font-size:\s*15px/);
  assert.match(rule(css, '.overview .ui-section-head h2'), /font-size:\s*16px/);
  assert.match(rule(css, '.overview .ui-empty .ui-tile'), /width:\s*56px/);
  assert.match(rule(css, '.overview .ui-empty h3'), /font-size:\s*20px/);
  assert.match(await read('features/overview/OverviewPage.tsx'), /import '\.\/overview\.css'/);
});

test('8: the workbench has no fullscreen button', async () => {
  const wb = await read('ui/workbench/Workbench.tsx');
  assert.doesNotMatch(wb, /maximize|minimize|setFull|is-full/);
  assert.match(wb, /aria-label="下载当前文件"/);
  assert.doesNotMatch(await read('ui/workbench/workbench.css'), /is-full/);
});

test('9: the live tab shows one segment per step with done / current / todo classes', async () => {
  const wb = await read('ui/workbench/Workbench.tsx');
  assert.match(wb, /Array\.from\(\{ length: bar\.total \}/);
  assert.match(wb, /i < barDone \? 'is-done' : i === barDone \? 'is-current' : 'is-todo'/);
  assert.match(wb, /role="progressbar"/);
  assert.match(wb, /aria-valuenow=\{barDone\}/);
  const css = await read('ui/workbench/workbench.css');
  const seg = rule(css, '.wb-step');
  assert.match(seg, /width:\s*14px/);
  assert.match(seg, /height:\s*6px/);
  assert.match(rule(css, '.wb-steps'), /gap:\s*4px/);
  assert.match(rule(css, '.wb-step.is-current'), /var\(--accent-soft\)[\s\S]*var\(--accent\)/);
  for (const sel of ['.wb-step', '.wb-step.is-done', '.wb-step.is-current']) assert.doesNotMatch(rule(css, sel), /--ok/);
});
