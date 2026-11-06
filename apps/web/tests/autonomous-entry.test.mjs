import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createAutonomousApi, authenticatedGuidedFetch, initializeAutonomousSession } from '../src/api-client/autonomous.ts';

test('cookie session token from the live session reaches autonomous writes', async () => {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init, headers: new Headers(init?.headers) });
    return new Response(JSON.stringify({ data: path === '/api/v1/auth/me' ? { csrf_token: 'cookie-session-token' } : { id: 'm' }, meta: { request_id: 'r' } }));
  };
  try {
    await initializeAutonomousSession();
    await createAutonomousApi().saveProfile({ id: 'm', model: 'model', api: 'openai-completions', base_url: 'https://example.test' });
    await authenticatedGuidedFetch('/api/v1/projects', { method: 'POST', body: '{}', credentials: 'same-origin' });
    assert.equal(calls[0].path, '/api/v1/auth/me');
    assert.equal(calls[0].init.credentials, 'same-origin');
    assert.equal(calls[1].headers.get('X-CSRF-Token'), 'cookie-session-token');
    assert.equal(calls[2].headers.get('X-CSRF-Token'), 'cookie-session-token');
  } finally { globalThis.fetch = original; }
});

test('entry mounts the autonomous-only app shell and redirects the retired route', async () => {
  const entry = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
  assert.match(entry, /lazy\(\(\) => import\('\.\/shell\/AppShell'\)\)/, 'the app shell is the only lazily mounted surface');
  assert.doesNotMatch(entry, /GuidedPage|MockWorkspace|Phase01Lab|resolveSurface/);
  const routes = await readFile(new URL('../src/shell/routes.ts', import.meta.url), 'utf8');
  assert.match(routes, /startsWith\('#\/autonomous'\)/, 'legacy #/autonomous links still land somewhere sensible');
  assert.match(routes, /#\/task\//);
  const shell = await readFile(new URL('../src/shell/AppShell.tsx', import.meta.url), 'utf8');
  assert.match(shell, /location\.replace/);
  assert.doesNotMatch(shell, /guided|MockWorkspace/);
});

test('new-experiment page owns upload, parse progress, layered config and authorized start', async () => {
  const page = await readFile(new URL('../src/features/autonomous/NewExperimentPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /ModeDialog/);
  assert.match(page, /ParseProgress/);
  assert.match(page, /paper_version_id/);
  assert.match(page, /授权并启动/);
  assert.match(page, /launchBlockReason\(/, 'an unfinished PDF parse blocks the start button (see autonomous-launch.test.mjs)');
  assert.match(page, /paperIsPdf\(info\)/, 'the paper kind comes from the stored source kind');
  assert.match(page, /startBlockReason/, 'the disabled start button explains why');
  assert.match(page, /crypto\.randomUUID\(\)/, 'parse calls carry an idempotency key');
  assert.doesNotMatch(page, /localStorage|sessionStorage/);
});

test('task page keeps approval, three-state notices, recovery and report sections as first-class surfaces', async () => {
  const page = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /ApprovalCard/);
  assert.match(page, /decideTask\(task\.id, approval\.id, 'approve'\)/, 'approval is an explicit decision, not free text');
  assert.match(page, /decideTask\(task\.id, approval\.id, 'reject', note\)/, 'rejection is an explicit decision with an optional note');
  assert.doesNotMatch(page, /approve:\$\{/, 'no approve:<id> resume message');
  const card = await readFile(new URL('../src/features/autonomous/ApprovalCard.tsx', import.meta.url), 'utf8');
  assert.doesNotMatch(card, /尚未提供显式拒绝接口/, 'reject is enabled');
  assert.match(card, /maxLength=\{REJECT_NOTE_LIMIT\}/, 'the note respects the backend 4000 character limit');
  assert.match(page, /recovery_required/);
  assert.match(page, /需要恢复/);
  assert.match(page, /ReportView/);
  assert.match(page, /UsageView/);
  assert.doesNotMatch(page, /localStorage|sessionStorage/);
});
