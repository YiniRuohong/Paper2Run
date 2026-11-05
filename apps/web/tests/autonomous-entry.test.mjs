import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createAutonomousApi, authenticatedGuidedFetch, initializeAutonomousSession } from '../src/api-client/autonomous.ts';

test('cookie session token from existing live ApiClient reaches autonomous and guided writes', async () => {
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
test('autonomous article entry reuses real upload, mode selection and parse progress with a return route', async () => {
  const page = await readFile(new URL('../src/features/autonomous/AutonomousPage.tsx', import.meta.url), 'utf8');
  const shell = await readFile(new URL('../src/features/guided/GuidedPage.tsx', import.meta.url), 'utf8');
  const flow = await readFile(new URL('../src/features/guided/RealFlowPanel.tsx', import.meta.url), 'utf8');
  assert.match(page, /href="#\/new\?return=autonomous"/);
  assert.match(page, /创建项目 \/ 上传 PDF \/ 粘贴论文/);
  assert.match(page, /!authorized \|\| !projectId \|\| !paperVersion/);
  assert.match(page, /paper_version_id/);
  assert.match(shell, /返回自主实验并读取论文/);
  assert.match(shell, /active=\{view === 'autonomous'\}/);
  assert.match(shell, /<RealFlowPanel api=\{api\}/);
  assert.match(flow, /ModeDialog/); assert.match(flow, /ParseProgress/);
  assert.doesNotMatch(page, /localStorage|sessionStorage/);
  assert.match(page, /批准此方案并继续/);
  assert.match(page, /approval\.changed\.map/);
  assert.match(page, /`approve:\$\{approval\.id\}`/);
  assert.match(page, /action === 'resume' && \(task\.state !== 'paused' \|\| task\.pending_decision != null\)/);
});
