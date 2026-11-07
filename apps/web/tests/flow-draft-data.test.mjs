import test from 'node:test';
import assert from 'node:assert/strict';
import { ApiError } from '../src/api-client/client.ts';
import { defaultConfig } from '../src/features/autonomous/config.ts';
import { loadDraft, loadDraftFacts } from '../src/features/flow/draftData.ts';

const text = over => ({ status: 'ready', backend: 'online_agent', page_count: 12, pages_parsed: 12, chunks: [], warnings: [], error_code: null, preview: '# T', ...over });
function fakes({ config = null, tasks = [], rows = ['a', 'b'], info = {} } = {}) {
  const seen = { text: [] };
  const papers = {
    getProject: async id => ({ id, name: 'n', description: '复现表 2', version: 4 }),
    listPapers: async () => rows.map(paper_id => ({ paper_id, title: null })),
    getPaper: async id => {
      if (info[id] instanceof Error) throw info[id];
      return { paper_id: id, title: `title-${id}`, page_count: 3, paper_version_id: `v-${id}`, source_kind: 'text', ...info[id] };
    },
    getPaperText: async id => { seen.text.push(id); return text(); },
  };
  const api = {
    config: async () => { if (config instanceof Error) throw config; if (!config) throw new ApiError('none', 404, 'RESOURCE_NOT_FOUND'); return config; },
    tasks: async () => tasks,
  };
  return { api, papers, seen };
}

test('loadDraftFacts picks the newest versioned paper and reads parse state only for PDFs', async () => {
  const { api, papers, seen } = fakes({ info: { b: { source_kind: 'pdf' } } });
  const facts = await loadDraftFacts(api, papers, 'p', []);
  assert.deepEqual(facts.paper, { paperId: 'b', versionId: 'v-b', title: 'title-b', pdf: true, textStatus: 'ready', pageCount: 12 });
  assert.deepEqual(seen.text, ['b'], 'pasted text has no parse record to read');
  assert.equal(facts.description, '复现表 2');
  assert.equal(facts.config, null, 'a 404 config is an unsaved draft, not an error');
  assert.equal(facts.taskCount, 0);
});

test('a paper whose details cannot be read is skipped, and other config errors surface', async () => {
  const { api, papers } = fakes({ info: { b: new Error('gone') } });
  const facts = await loadDraftFacts(api, papers, 'p', []);
  assert.equal(facts.paper.paperId, 'a');
  const untitled = fakes({ rows: ['a'], info: { a: { title: null } } });
  assert.equal((await loadDraftFacts(untitled.api, untitled.papers, 'p', [])).paper.title, 'n', 'an untitled paper is named after its project');
  const broken = fakes({ config: new ApiError('boom', 500, 'INTERNAL') });
  await assert.rejects(loadDraftFacts(broken.api, broken.papers, 'p', []), error => error.code === 'INTERNAL');
});

test('loadDraft also returns what the page needs: project version, saved config, task list and the PDF text', async () => {
  const saved = { version: 2, config: defaultConfig('p') };
  const tasks = [{ id: 't1', created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z' }];
  const { api, papers } = fakes({ config: saved, tasks, rows: ['a'], info: { a: { source_kind: 'pdf' } } });
  const draft = await loadDraft(api, papers, 'p', []);
  assert.equal(draft.project.version, 4);
  assert.deepEqual(draft.saved, saved);
  assert.deepEqual(draft.facts.config, saved.config);
  assert.equal(draft.facts.taskCount, 1);
  assert.equal(draft.tasks, tasks);
  assert.equal(draft.paperText.preview, '# T');
});
