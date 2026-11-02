import test from 'node:test';
import assert from 'node:assert/strict';
import { DOCPARSE_STATUS_POLL_MS, DOCPARSE_TEXT_POLL_MS, RealFlowController } from '../src/features/guided/realflow.ts';
import { createRealFlowState, realFlowReducer } from '../src/features/guided/realflow-state.ts';
import { canExtract, docParseLabel } from '../src/features/docparse/docparse.ts';

// Guided real flow × MinerU PDF parsing: after a PDF upload the controller
// reads the global parse mode and the paper text, asks the user to choose a
// mode when none is set, parses only when the chosen backend is usable, polls
// while parsing/installing, and never switches between online and offline on
// its own. Poll intervals are injected so no test waits real seconds.

const SHA = 'b'.repeat(64);
const project = { id: 'prj', name: '真实文章复现', role: 'owner', version: 1, created_at: 't' };
const paperInfo = (status = 'uploaded') => ({
  paper_id: 'paper', project_id: 'prj', title: null, status, sha256: SHA,
  page_count: 30, content_available: true, current_extraction_id: null, active_operation_id: null,
  error: null, created_at: 't',
});
const extraction = {
  extraction_id: 'x1', paper_id: 'paper', schema_version: '1.0', fields: {}, methods: [], formulas: [],
  candidate_experiments: [], source_refs: [], warnings: [], provider: { model_id: 'm' },
};
const pdf = () => new File(['%PDF-fake'], 'paper.pdf', { type: 'application/pdf' });

function textOf(status, extra = {}) {
  const done = status === 'ready';
  return {
    status, backend: done ? 'online_agent' : null, page_count: 30, pages_parsed: done ? 30 : 0,
    chunks: status === 'parsing' ? [{ status: 'done' }, { status: 'pending' }] : done ? [{ first_page: 1, last_page: 30, status: 'done' }] : [],
    warnings: [], error_code: null, preview: done ? '# A paper' : null, ...extra,
  };
}

function setup(initial = {}) {
  const calls = [];
  const server = {
    mode: null, offline: 'not_installed', installPolls: 2, installEnd: 'ready',
    texts: [textOf('none')], afterParse: [textOf('parsing'), textOf('parsing'), textOf('ready')],
    paperStatus: 'uploaded', gates: {}, ...initial,
  };
  // A test may queue promises under gates.text / gates.install to hold the
  // next matching call open (the call is recorded before it waits).
  const gate = async (name) => { const g = server.gates[name]?.shift(); if (g) await g; };
  const status = () => ({
    mode: server.mode,
    offline: { state: server.offline, step: server.offline === 'installing' ? 'packages' : null, progress: 0.5, tier: null, version: null, bytes_on_disk: 0, error: null },
    platform: 'darwin-arm64',
  });
  const api = {
    async createProject(name, key) { calls.push(['POST /projects', key]); return project; },
    async uploadPaperPdf(projectId, file, options) { calls.push([`POST /projects/${projectId}/papers`, options.key]); return { paper_id: 'paper', operation_id: null, status: server.paperStatus }; },
    async createTextPaper(projectId, input) { calls.push([`POST /projects/${projectId}/papers/text`, input.key]); return { paper_id: 'paper', operation_id: null, status: 'uploaded' }; },
    async getPaper(paperId) { calls.push([`GET /papers/${paperId}`]); return paperInfo(server.paperStatus); },
    async getDocParseStatus() {
      calls.push(['GET /docparse/status']);
      if (server.offline === 'installing' && --server.installPolls <= 0) server.offline = server.installEnd;
      return status();
    },
    async setDocParseMode(mode) { calls.push(['PUT /docparse/mode', mode]); server.mode = mode; return status(); },
    async startOfflineInstall() { calls.push(['POST /docparse/offline/install']); await gate('install'); server.offline = 'installing'; return status(); },
    async cancelOfflineInstall() { calls.push(['POST /docparse/offline/cancel']); server.offline = 'not_installed'; return status(); },
    async uninstallOffline() { calls.push(['DELETE /docparse/offline']); server.offline = 'not_installed'; return status(); },
    async parsePaper(paperId, key) {
      calls.push([`POST /papers/${paperId}/parse`, key, server.mode]);
      if (server.mode === null) throw Object.assign(new Error('choose online or offline parsing first'), { status: 409, code: 'DOCPARSE_MODE_UNSET' });
      server.texts = [...server.afterParse];
      return { paper_id: paperId, job_id: 'job', status: 'parsing' };
    },
    async getPaperText(paperId) {
      calls.push([`GET /papers/${paperId}/text`]);
      await gate('text');
      return server.texts.length > 1 ? server.texts.shift() : server.texts[0];
    },
    async createExtraction(paperId, input) { calls.push([`POST /papers/${paperId}/extractions`, input.key]); return { paper_id: paperId, operation_id: 'op', status: 'ready', extraction_id: 'x1' }; },
    async getExtraction(paperId) { calls.push([`GET /papers/${paperId}/extraction`]); return extraction; },
    async write(intent) { throw new Error(`unexpected write ${intent.path}`); },
    async read(path) { throw new Error(`unexpected read ${path}`); },
  };
  const controllers = [];
  const create = () => {
    const c = new RealFlowController({ api, docParsePollMs: { text: 1, status: 1 } });
    controllers.push(c);
    return c;
  };
  const names = () => calls.map(x => x[0]);
  const count = (name) => names().filter(x => x === name).length;
  return { api, calls, server, create, names, count, dispose: () => controllers.forEach(c => c.dispose()) };
}

async function until(predicate, label, ms = 1000) {
  const start = Date.now();
  while (!predicate()) {
    if (Date.now() - start > ms) assert.fail(`timed out waiting for: ${label}`);
    await new Promise(resolve => setTimeout(resolve, 2));
  }
}
const settle = () => new Promise(resolve => setTimeout(resolve, 25));

test('default poll cadence: text every ~2 s while parsing, status every ~3 s while installing', () => {
  assert.equal(DOCPARSE_TEXT_POLL_MS, 2000);
  assert.equal(DOCPARSE_STATUS_POLL_MS, 3000);
});

test('pdf upload with no mode chosen asks for a choice and never parses', async (t) => {
  const f = setup(); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  const s = c.getSnapshot();
  assert.equal(s.docparse.needsChoice, true);
  assert.equal(s.docparse.status.mode, null);
  assert.equal(s.docparse.text.status, 'none');
  assert.equal(f.count('POST /papers/paper/parse'), 0);
  assert.equal(f.count('PUT /docparse/mode'), 0);
  assert.equal(f.count('POST /docparse/offline/install'), 0);
  assert.equal(canExtract(s), false);
  assert.equal(docParseLabel(s.docparse.status, s.docparse.text), '等待选择解析方式');
  await assert.rejects(c.runExtraction(), /PAPER_TEXT_NOT_READY/);
  assert.equal(f.count('POST /papers/paper/extractions'), 0, 'extraction is refused before any write');
});

test('chooseParseMode(online) sets the mode, parses, and unlocks extraction only once text is ready', async (t) => {
  const f = setup(); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await c.chooseParseMode('online');
  assert.deepEqual(f.calls.find(x => x[0] === 'PUT /docparse/mode'), ['PUT /docparse/mode', 'online']);
  assert.equal(f.count('POST /papers/paper/parse'), 1);
  assert.equal(typeof f.calls.find(x => x[0] === 'POST /papers/paper/parse')[1], 'string');
  assert.equal(f.count('POST /docparse/offline/install'), 0, 'online never touches the offline installer');
  let s = c.getSnapshot();
  assert.equal(s.docparse.needsChoice, false);
  assert.equal(s.docparse.text.status, 'parsing');
  assert.equal(canExtract(s), false);
  assert.equal(docParseLabel(s.docparse.status, s.docparse.text), '解析中 · 第 1/2 段');
  await assert.rejects(c.runExtraction(), /PAPER_TEXT_NOT_READY/);
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
  s = c.getSnapshot();
  assert.equal(canExtract(s), true);
  assert.equal(docParseLabel(s.docparse.status, s.docparse.text), '解析完成');
  const reads = f.count('GET /papers/paper/text');
  await settle();
  assert.equal(f.count('GET /papers/paper/text'), reads, 'polling stops on a terminal state');
  await c.runExtraction();
  assert.equal(c.getSnapshot().extractionPhase, 'ready');
});

test('a parse the server already enqueued on upload is polled, never re-requested', async (t) => {
  const f = setup({ mode: 'online', paperStatus: 'parsing', texts: [textOf('parsing'), textOf('parsing'), textOf('ready')] });
  t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  assert.equal(c.getSnapshot().docparse.text.status, 'parsing');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
  assert.equal(f.count('POST /papers/paper/parse'), 0);
  assert.equal(c.getSnapshot().docparse.needsChoice, false);
});

test('pdf upload with online mode and no text yet parses automatically', async (t) => {
  const f = setup({ mode: 'online' }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  assert.equal(f.count('POST /papers/paper/parse'), 1);
  assert.equal(f.count('PUT /docparse/mode'), 0, 'an existing mode is used as-is');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
});

test('pdf upload with offline ready parses; offline not installed or broken waits for the user', async (t) => {
  const ready = setup({ mode: 'offline', offline: 'ready' }); t.after(ready.dispose);
  await ready.create().uploadArticle({ consent: true, file: pdf() });
  assert.equal(ready.count('POST /papers/paper/parse'), 1);

  for (const offline of ['not_installed', 'failed', 'broken', 'unsupported']) {
    const f = setup({ mode: 'offline', offline, texts: [textOf('failed', { error_code: 'DOCPARSE_OFFLINE_NOT_READY' })] });
    t.after(f.dispose);
    const c = f.create();
    await c.uploadArticle({ consent: true, file: pdf() });
    await settle();
    assert.equal(f.count('POST /papers/paper/parse'), 0, `${offline}: no parse`);
    assert.equal(f.count('POST /docparse/offline/install'), 0, `${offline}: no automatic install`);
    assert.equal(f.count('PUT /docparse/mode'), 0, `${offline}: no automatic mode switch`);
    assert.equal(c.getSnapshot().docparse.needsChoice, false);
    assert.equal(canExtract(c.getSnapshot()), false);
  }
});

test('pdf upload while the offline component installs waits for ready, then parses exactly once', async (t) => {
  const f = setup({ mode: 'offline', offline: 'installing', installPolls: 3 }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  assert.equal(f.count('POST /papers/paper/parse'), 0, 'not before the installer is ready');
  await until(() => f.count('POST /papers/paper/parse') === 1, 'parse after install is ready');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
  await settle();
  assert.equal(f.count('POST /papers/paper/parse'), 1);
  assert.equal(f.count('PUT /docparse/mode'), 0);
});

test('chooseParseMode(offline) installs and never parses or goes online while the installer is not ready', async (t) => {
  const f = setup({ installPolls: 4 }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await c.chooseParseMode('offline');
  assert.deepEqual(f.calls.filter(x => x[0] === 'PUT /docparse/mode').map(x => x[1]), ['offline']);
  assert.equal(f.count('POST /docparse/offline/install'), 1);
  assert.equal(f.count('POST /papers/paper/parse'), 0, 'no parse while installing');
  let s = c.getSnapshot();
  assert.equal(s.docparse.status.offline.state, 'installing');
  assert.equal(docParseLabel(s.docparse.status, s.docparse.text), '离线组件安装中 · packages');
  await until(() => f.count('POST /papers/paper/parse') === 1, 'parse once the installer reports ready');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
  await settle();
  assert.equal(f.count('POST /papers/paper/parse'), 1, 'parsed exactly once');
  assert.deepEqual(f.calls.filter(x => x[0] === 'PUT /docparse/mode').map(x => x[1]), ['offline'], 'never switched to online');
});

test('chooseParseMode(offline) with a failing install stops without parsing, reinstalling, or switching', async (t) => {
  const f = setup({ installPolls: 2, installEnd: 'failed' }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await c.chooseParseMode('offline');
  await until(() => c.getSnapshot().docparse.status.offline.state === 'failed', 'installer reports failed');
  const statusReads = f.count('GET /docparse/status');
  await settle();
  assert.equal(f.count('GET /docparse/status'), statusReads, 'status polling stops on a terminal state');
  assert.equal(f.count('POST /papers/paper/parse'), 0);
  assert.equal(f.count('POST /docparse/offline/install'), 1);
  assert.deepEqual(f.calls.filter(x => x[0] === 'PUT /docparse/mode').map(x => x[1]), ['offline']);
  assert.equal(canExtract(c.getSnapshot()), false);
});

test('chooseParseMode(offline) with an already ready installer parses without reinstalling', async (t) => {
  const f = setup({ offline: 'ready' }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await c.chooseParseMode('offline');
  assert.equal(f.count('POST /docparse/offline/install'), 0);
  assert.equal(f.count('POST /papers/paper/parse'), 1);
});

test('a failed parse stops polling; retryParse mints a new key and keeps the mode', async (t) => {
  const failed = textOf('failed', { error_code: 'DOCPARSE_REMOTE_FAILED' });
  const f = setup({ mode: 'online', afterParse: [textOf('parsing'), failed] }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await until(() => c.getSnapshot().docparse.text.status === 'failed', 'parse fails');
  const reads = f.count('GET /papers/paper/text');
  await settle();
  assert.equal(f.count('GET /papers/paper/text'), reads, 'no polling after failure');
  assert.equal(f.count('POST /papers/paper/parse'), 1, 'failure is never retried automatically');
  assert.equal(docParseLabel(c.getSnapshot().docparse.status, c.getSnapshot().docparse.text), '解析失败');
  f.server.afterParse = [textOf('parsing'), textOf('ready')];
  await c.retryParse();
  const keys = f.calls.filter(x => x[0] === 'POST /papers/paper/parse').map(x => x[1]);
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1], 'a retry is a new parse request');
  assert.equal(f.count('PUT /docparse/mode'), 0, 'retry never changes the mode');
  assert.equal(f.count('POST /docparse/offline/install'), 0);
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'retried parse reaches ready');
});

test('switchParseMode is the explicit way across: online after an offline failure', async (t) => {
  const f = setup({ mode: 'offline', offline: 'broken', texts: [textOf('failed', { error_code: 'DOCPARSE_OFFLINE_NOT_READY' })] });
  t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  assert.equal(f.count('POST /papers/paper/parse'), 0);
  await c.switchParseMode('online');
  assert.deepEqual(f.calls.filter(x => x[0] === 'PUT /docparse/mode').map(x => x[1]), ['online']);
  assert.equal(f.count('POST /papers/paper/parse'), 1);
  assert.equal(f.count('POST /docparse/offline/install'), 0, 'switching to online never installs offline');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'online parse reaches ready');
});

test('text papers never touch docparse and can always be extracted', async (t) => {
  const f = setup(); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, text: 'A real paper text.' });
  assert.deepEqual(f.names().filter(x => x.includes('docparse') || x === 'GET /papers/paper/text' || x.endsWith('/parse')), []);
  assert.equal(canExtract(c.getSnapshot()), true);
  await c.runExtraction();
  assert.equal(c.getSnapshot().extractionPhase, 'ready');
});

test('pollPaperText re-reads the current text on demand', async (t) => {
  const f = setup(); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  f.server.texts = [textOf('ready')];
  await c.pollPaperText();
  assert.equal(c.getSnapshot().docparse.text.status, 'ready');
  assert.equal(canExtract(c.getSnapshot()), true);
});

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return { promise, resolve };
}

test('pollPaperText during upload keeps the choice dialog', async (t) => {
  const held = deferred();
  const f = setup({ gates: { text: [held.promise] } }); t.after(f.dispose);
  const c = f.create();
  const upload = c.uploadArticle({ consent: true, file: pdf() });
  await until(() => f.count('GET /papers/paper/text') === 1, 'upload text read is in flight');
  await c.pollPaperText();
  held.resolve();
  await upload;
  await settle();
  const s = c.getSnapshot();
  assert.equal(s.docparse.needsChoice, true, 'the refresh must not cancel the upload flow');
  assert.equal(s.error, null);
  assert.equal(f.count('POST /papers/paper/parse'), 0);
});

test('pollPaperText during offline chooseParseMode keeps waiting for install', async (t) => {
  const held = deferred();
  const f = setup({ installPolls: 3, gates: { install: [held.promise] } }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  const choose = c.chooseParseMode('offline');
  await until(() => f.count('POST /docparse/offline/install') === 1, 'install request is in flight');
  await c.pollPaperText();
  held.resolve();
  await choose;
  assert.equal(c.getSnapshot().docparse.status.offline.state, 'installing');
  await until(() => f.count('POST /papers/paper/parse') === 1, 'parse once the installer reports ready');
  await until(() => c.getSnapshot().docparse.text.status === 'ready', 'text polling reaches ready');
  await settle();
  const parses = f.calls.filter(x => x[0] === 'POST /papers/paper/parse');
  assert.equal(parses.length, 1, 'parsed exactly once');
  assert.equal(parses[0][2], 'offline', 'parsed under the chosen offline mode');
  assert.deepEqual(f.calls.filter(x => x[0] === 'PUT /docparse/mode').map(x => x[1]), ['offline']);
});

test('dispose stops background polling', async (t) => {
  const f = setup({ mode: 'online', afterParse: [textOf('parsing')] }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await until(() => f.count('GET /papers/paper/text') >= 3, 'polling is running');
  c.dispose();
  await settle();
  const reads = f.count('GET /papers/paper/text');
  await settle();
  assert.equal(f.count('GET /papers/paper/text'), reads);
});

test('a new upload stops the previous paper polling', async (t) => {
  const f = setup({ mode: 'online', afterParse: [textOf('parsing')] }); t.after(f.dispose);
  const c = f.create();
  await c.uploadArticle({ consent: true, file: pdf() });
  await until(() => f.count('GET /papers/paper/text') >= 2, 'polling is running');
  await c.uploadArticle({ consent: true, text: 'now a text paper' });
  await settle();
  const reads = f.count('GET /papers/paper/text');
  await settle();
  assert.equal(f.count('GET /papers/paper/text'), reads, 'the old loop is cancelled');
  assert.equal(c.getSnapshot().docparse.text, null);
  assert.equal(canExtract(c.getSnapshot()), true);
});

test('reducer ignores text readbacks for another paper and clears the choice once a mode exists', () => {
  let s = realFlowReducer(createRealFlowState(), { type: 'paper', value: paperInfo(), pdf: true });
  assert.equal(s.docparse.pdf, true);
  assert.equal(realFlowReducer(s, { type: 'docparse-text', paperId: 'other', value: textOf('ready') }), s);
  s = realFlowReducer(s, { type: 'docparse-needs-choice', value: true });
  assert.equal(s.docparse.needsChoice, true);
  const status = { mode: 'online', offline: { state: 'not_installed', step: null, progress: 0, tier: null, version: null, bytes_on_disk: 0, error: null }, platform: 'darwin-arm64' };
  s = realFlowReducer(s, { type: 'docparse-status', value: status });
  assert.equal(s.docparse.needsChoice, false);
});

test('docParseLabel covers the waiting states in short Chinese copy', () => {
  const st = (mode, state, step = null) => ({ mode, offline: { state, step, progress: 0, tier: null, version: null, bytes_on_disk: 0, error: null }, platform: 'darwin-arm64' });
  const none = textOf('none');
  const notReady = textOf('failed', { error_code: 'DOCPARSE_OFFLINE_NOT_READY' });
  assert.equal(docParseLabel(st(null, 'not_installed'), none), '等待选择解析方式');
  assert.equal(docParseLabel(st('offline', 'installing', 'models'), none), '离线组件安装中 · models');
  assert.equal(docParseLabel(st('offline', 'installing'), none), '离线组件安装中');
  assert.equal(docParseLabel(st('offline', 'not_installed'), notReady), '离线组件未安装');
  assert.equal(docParseLabel(st('online', 'not_installed'), textOf('parsing', { chunks: [] })), '解析中');
  assert.equal(docParseLabel(st('online', 'not_installed'), textOf('failed', { error_code: 'DOCPARSE_REMOTE_FAILED' })), '解析失败');
  assert.equal(docParseLabel(st('online', 'not_installed'), textOf('ready')), '解析完成');
});
