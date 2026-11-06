import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { ApiError } from '../src/api-client/client.ts';
import { loadAutonomousTasks } from '../src/shell/hooks.ts';
import { loadDraftFacts } from '../src/features/flow/draftData.ts';
import { draftProgress } from '../src/features/flow/draft.ts';
import { loadDraftSteps } from '../src/features/overview/draftSteps.ts';

function fakes() {
  const projects = [
    { id: 'p1', name: '有任务', version: 1, updated_at: '2026-01-02T00:00:00Z' },
    { id: 'p2', name: '旧草稿', version: 1, updated_at: '2026-01-01T00:00:00Z' },
    { id: 'p3', name: '新草稿', version: 1, updated_at: '2026-01-03T00:00:00Z' },
    { id: 'p4', name: '读取失败', version: 1 },
  ];
  const papers = {
    listProjects: async () => projects,
    getProject: async id => projects.find(p => p.id === id),
    listPapers: async () => [],
  };
  const api = {
    tasks: async id => {
      if (id === 'p4') throw new Error('boom');
      return id === 'p1' ? [{ id: 't1', project_id: 'p1', objective: 'o', state: 'completed', created_at: '2026-01-02T00:00:00Z' }] : [];
    },
    config: async () => { throw new ApiError('none', 404, 'RESOURCE_NOT_FOUND'); },
  };
  return { api, papers };
}

test('projects with zero tasks become drafts, newest first; failed reads are not drafts', async () => {
  const { api, papers } = fakes();
  const result = await loadAutonomousTasks(api, papers);
  assert.deepEqual(result.tasks.map(t => t.id), ['t1']);
  assert.deepEqual(result.drafts, [
    { projectId: 'p3', name: '新草稿', updatedAt: '2026-01-03T00:00:00Z' },
    { projectId: 'p2', name: '旧草稿', updatedAt: '2026-01-01T00:00:00Z' },
  ]);
  assert.equal(result.failedProjects, 1);
});

test('a draft without papers is at the paper step', async () => {
  const { api, papers } = fakes();
  const facts = await loadDraftFacts(api, papers, 'p2', []);
  assert.equal(draftProgress(facts).current, 'paper');
});

test('the overview filters on drafts, deletes with confirmation and links to the draft', async () => {
  const src = await readFile(new URL('../src/features/overview/OverviewPage.tsx', import.meta.url), 'utf8');
  assert.match(src, /'draft'/);
  assert.match(src, /deleteProject\(/);
  assert.match(src, /confirm\(/);
  assert.match(src, /draftHref\(/);
  assert.match(src, /loadDraftSteps/);
  assert.match(src, /errorDetail/);
});

test('the delete button is a sibling of the row link, labelled 删除草稿', async () => {
  const src = await readFile(new URL('../src/features/overview/OverviewPage.tsx', import.meta.url), 'utf8');
  const start = src.indexOf('href={draftHref(');
  const end = src.indexOf('</a>', start);
  assert.ok(start > 0 && end > start);
  assert.doesNotMatch(src.slice(start, end), /<button/);
  assert.match(src, />删除草稿</);
  assert.match(src, /aria-label=\{`删除草稿「\$\{/);
});

test('the step effect is keyed on draft ids, not the drafts array', async () => {
  const src = await readFile(new URL('../src/features/overview/OverviewPage.tsx', import.meta.url), 'utf8');
  assert.match(src, /draftIds/);
  assert.doesNotMatch(src, /\[api, papers, drafts\]/);
});

test('draft steps are not guessed when the profiles read fails', async () => {
  const { papers } = fakes();
  const api = {
    profiles: async () => { throw new Error('down'); },
    tasks: async () => [],
    config: async () => { throw new ApiError('none', 404, 'RESOURCE_NOT_FOUND'); },
  };
  assert.deepEqual(await loadDraftSteps(api, papers, ['p2', 'p3']), {});
  const ok = { ...api, profiles: async () => [] };
  assert.deepEqual(await loadDraftSteps(ok, papers, ['p2', 'p3']), { p2: 'paper', p3: 'paper' });
});
