import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const read = rel => readFile(new URL(`../src/${rel}`, import.meta.url), 'utf8');

test('A1: the draft page passes no status line; the task page keeps only the status pill', async () => {
  const draft = await read('features/flow/DraftPage.tsx');
  assert.doesNotMatch(draft, /<Workbench[^>]*\n\s+status=/);
  assert.doesNotMatch(draft, /草稿 · 第/);
  const task = await read('features/autonomous/TaskPage.tsx');
  assert.doesNotMatch(task, /meta=\{/);
  assert.doesNotMatch(task, /生命周期：\{display\.lifecycle\}<\/span>/);
  assert.doesNotMatch(task, /更新于/);
  assert.doesNotMatch(task, /条事件<\/span>/);
  assert.doesNotMatch(task, /任务的阶段、命令输出和产物会在这里持续更新/);
  assert.match(task, /status=\{task && display/);
  // the verified caveat survives: tooltip of the pill and the live status card
  assert.match(task, /title: display\.caption/);
  assert.match(task, /display\?\.caption/);
});

test('A2: step 01 uses a hidden file input with a button-styled label and no placeholder text', async () => {
  const step = await read('features/flow/steps/PaperStep.tsx');
  assert.match(step, /className="flow-file-input"/);
  assert.match(step, /选择 PDF\s*<\/label>/);
  assert.match(step, /file && <span className="flow-file-name"/);
  assert.match(step, /type="file"/);
  const css = await read('features/flow/flow.css');
  assert.match(css, /\.flow-file-input\s*\{[^}]*(clip|opacity: 0)/);
  assert.match(css, /\.flow-file-pick:focus-within/);
});

test('A3/A4: no empty-file sentence and no file footer note on either page', async () => {
  const draft = await read('features/flow/DraftPage.tsx');
  const task = await read('features/autonomous/TaskPage.tsx');
  for (const text of [draft, task]) {
    assert.doesNotMatch(text, /filesNote=/);
    assert.doesNotMatch(text, /上传论文后，这里会/);
    assert.doesNotMatch(text, /还没有文件。实验写入/);
    assert.match(text, /filesEmpty=""/);
  }
  const wb = await read('ui/workbench/Workbench.tsx');
  assert.match(wb, /props\.filesEmpty !== ''/);
});

test('B: step guide is a hover/focus tooltip behind a round "!" button', async () => {
  const frame = await read('features/flow/StepFrame.tsx');
  assert.match(frame, /role="tooltip"/);
  assert.match(frame, /aria-describedby=\{tipId\}/);
  assert.match(frame, /<button type="button" className="flow-hint"/);
  assert.doesNotMatch(frame, /<p className="flow-guide">/);
  assert.match(frame, /title: string/);
  const css = await read('features/flow/flow.css');
  assert.match(css, /\.flow-tip\s*\{[^}]*visibility: hidden/);
  assert.match(css, /\.flow-hint:hover \+ \.flow-tip/);
  assert.match(css, /\.flow-hint:focus-visible \+ \.flow-tip/);
  for (const f of ['Paper', 'Objective', 'Model', 'Launch']) {
    const src = await read(`features/flow/steps/${f}Step.tsx`);
    assert.match(src, /title=\{SETUP_TITLES\./, `${f} passes its title`);
  }
});

test('C: progressBar replaces the 实时 tab segment and the rail progress line', async () => {
  const wb = await read('ui/workbench/Workbench.tsx');
  assert.match(wb, /progressBar\?: \{ done: number; total: number; label: string; extra\?: ReactNode \}/);
  assert.match(wb, /role="progressbar"/);
  assert.match(wb, /aria-valuenow=\{/);
  assert.match(wb, /aria-valuemin=\{0\}/);
  assert.match(wb, /aria-valuemax=\{/);
  assert.match(wb, /aria-label=\{`实时 · \$\{bar\.label\}`\}/);
  assert.match(wb, /!props\.progressBar && props\.progress/);
  const css = await read('ui/workbench/workbench.css');
  const done = css.match(/\.wb-step\.is-done\s*\{[^}]*\}/);
  assert.ok(done, 'done segment rule exists');
  assert.match(done[0], /var\(--accent\)/);
  assert.doesNotMatch(done[0], /--ok/);
  const draft = await read('features/flow/DraftPage.tsx');
  assert.match(draft, /progressBar=\{\{ done: doneCount, total: 9, label: /);
  assert.doesNotMatch(draft, /progress=\{/);
  const task = await read('features/autonomous/TaskPage.tsx');
  assert.match(task, /progressBar=\{\{ done: doneCount, total: 9, label: activeTitle/);
  assert.match(task, /阶段由 Pi 自报进度（note_progress）与事件归纳得出，不是程序判定/);
  assert.doesNotMatch(task, /progress=\{/);
});
