import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DOWNLOAD_NOTE, FLASH_NOTICE, OFFLINE_UNREADY, canParseNow, canUninstallOffline, docParseErrorReason, docParseLabel, formatBytes,
  hasMissingChunks, offlineNotices, parseActionNotes,
} from '../src/features/docparse/docparse.ts';

// Every fixed public docparse error code maps to one specific Chinese reason.
// Anything else (an unknown code, null) falls back to a generic line and never
// echoes raw server text.

const FALLBACK = '解析失败（未知原因）';
const CASES = [
  ['DOCPARSE_OFFLINE_NOT_READY', '离线组件未就绪，请先完成安装'],
  ['DOCPARSE_OFFLINE_FAILED', '离线解析失败'],
  ['DOCPARSE_OFFLINE_UNSUPPORTED', '本机不支持离线解析'],
  ['DOCPARSE_RATE_LIMITED', '在线服务请求过于频繁，请稍后重试'],
  ['DOCPARSE_REMOTE_FAILED', '在线解析服务失败'],
  ['DOCPARSE_TIMEOUT', '解析超时'],
  ['DOCPARSE_PDF_INVALID', 'PDF 无法读取（可能已加密或损坏）'],
  ['DOCPARSE_PAGE_TOO_LARGE', '单页体积过大，在线解析无法处理，可改用离线解析'],
  ['DOCPARSE_UNSAFE_URL', '在线服务返回了不安全或过大的内容，已拒绝'],
  ['DOCPARSE_RESPONSE_TOO_LARGE', '在线服务返回了不安全或过大的内容，已拒绝'],
  ['DOCPARSE_CANCELLED', '解析已取消'],
  ['DOCPARSE_MODE_UNSET', '尚未选择解析方式'],
  ['DOCPARSE_MODE_CHANGED', '解析方式已更改，请重试'],
];

for (const [code, reason] of CASES) {
  test(`docParseErrorReason maps ${code}`, () => {
    assert.equal(docParseErrorReason(code), reason);
  });
}

test('docParseErrorReason falls back for an unknown code and never echoes it', () => {
  assert.equal(docParseErrorReason('DOCPARSE_SOMETHING_NEW'), FALLBACK);
  assert.equal(docParseErrorReason('Traceback: secret token abc123'), FALLBACK);
  assert.ok(!docParseErrorReason('Traceback: secret token abc123').includes('abc123'));
});

test('docParseErrorReason falls back for null and the empty string', () => {
  assert.equal(docParseErrorReason(null), FALLBACK);
  assert.equal(docParseErrorReason(''), FALLBACK);
});

test('docParseErrorReason gives every distinct code a non-empty reason', () => {
  for (const [code] of CASES) assert.ok(docParseErrorReason(code).length > 0);
});

const status = (mode, state) => ({
  mode,
  offline: { state, step: null, progress: 0, tier: null, version: null, bytes_on_disk: 0, error: null },
  platform: 'darwin-arm64',
});

test('formatBytes renders MB below one GB and GB above', () => {
  assert.equal(formatBytes(0), '0 MB');
  assert.equal(formatBytes(1), '1 MB');
  assert.equal(formatBytes(150 * 1024 * 1024), '150 MB');
  assert.equal(formatBytes(1536 * 1024 * 1024), '1.5 GB');
  assert.equal(formatBytes(Number.NaN), '0 MB');
});

test('PDFs are parsed only locally: there is no mode to choose', async () => {
  // The server reports offline whenever the platform can run it; anything else cannot be parsed here.
  assert.equal(canParseNow(status('offline', 'ready')), true);
  assert.equal(canParseNow(status('offline', 'not_installed')), false);
  assert.equal(canParseNow(status('offline', 'installing')), false);
  assert.equal(canParseNow(status('online', 'ready')), false);
  assert.equal(canParseNow(status(null, 'unsupported')), false);
  assert.deepEqual([...OFFLINE_UNREADY].sort(), ['broken', 'failed', 'not_installed']);
  assert.equal(docParseLabel(status('offline', 'not_installed'), null), '首次使用需下载本地解析组件');
  assert.equal(docParseLabel(status(null, 'unsupported'), null), '本机不支持本地解析');
  const { readFile, access } = await import('node:fs/promises');
  const src = path => readFile(new URL(`../src/features/${path}`, import.meta.url), 'utf8');
  await assert.rejects(access(new URL('../src/features/docparse/ModeDialog.tsx', import.meta.url)), 'the mode dialog is gone');
  const draft = await src('flow/DraftPage.tsx');
  assert.doesNotMatch(draft, /ModeDialog|setDocParseMode|'online'/);
  // First use downloads the component, and the paper is parsed once it is ready.
  assert.match(draft, /papers\.startOfflineInstall\(\)/);
  assert.match(draft, /parseAfterInstall\.current = paperId/);
  assert.match(draft, /status\.offline\.state === 'installing' && isParsePending\(next\.paperText\)/);
  // Leaving "installing" ends the install effect; the parse, its polling and the re-read must still
  // happen, so the finish is guarded by the page being mounted, and the status flips after the parse.
  const finishAt = draft.indexOf('const finish = async');
  const finish = finishAt < 0 ? '' : draft.slice(finishAt, draft.indexOf('const tick = async', finishAt));
  assert.ok(finish.length > 0, 'install finish step exists');
  assert.doesNotMatch(finish, /\balive\b/);
  assert.ok(finish.indexOf('parsePaper(') < finish.indexOf('setParseStatus(next)'), 'parse is requested before the status ends the effect');
  assert.match(finish, /if \(!mounted\.current\) return;/);
  assert.match(finish, /setPolling\(target\)/);
  assert.match(finish, /await reload\(\)/);
  assert.doesNotMatch(await src('docparse/ParseProgress.tsx'), /改用在线解析|改用本地解析|选择解析方式/);
  assert.doesNotMatch(await src('docparse/DocParseSettings.tsx'), /Segmented|setDocParseMode/);
});

const chunk = (first, last, st) => ({ first_page: first, last_page: last, status: st });
const textOf = (st, chunks) => ({ status: st, chunks, warnings: [], page_count: 40, pages_parsed: 20 });

test('hasMissingChunks is true only for a ready text with a missing range', () => {
  assert.equal(hasMissingChunks(null), false);
  assert.equal(hasMissingChunks(textOf('ready', [chunk(1, 20, 'done'), chunk(21, 40, 'done')])), false);
  assert.equal(hasMissingChunks(textOf('ready', [chunk(1, 20, 'done'), chunk(21, 40, 'missing')])), true);
  assert.equal(hasMissingChunks(textOf('parsing', [chunk(1, 20, 'missing')])), false);
});

const offline = (state, extra = {}) => ({ ...status('offline', state).offline, ...extra });

test('canUninstallOffline offers removal for leftovers but not while installing', () => {
  assert.equal(canUninstallOffline(offline('ready')), true);
  assert.equal(canUninstallOffline(offline('failed', { bytes_on_disk: 70 })), true);
  assert.equal(canUninstallOffline(offline('not_installed', { bytes_on_disk: 5 })), true);
  assert.equal(canUninstallOffline(offline('failed')), false);
  assert.equal(canUninstallOffline(offline('installing', { bytes_on_disk: 70 })), false);
  assert.equal(canUninstallOffline(offline('unsupported', { bytes_on_disk: 70 })), false);
});

test('offlineNotices shows the install failure reason and the flash notice', () => {
  assert.deepEqual(offlineNotices(offline('failed', { error: '磁盘空间不足（需要约 6 GB）' })), ['磁盘空间不足（需要约 6 GB）']);
  assert.deepEqual(offlineNotices(offline('ready', { tier: 'flash', error: 'stale' })), [FLASH_NOTICE]);
  assert.deepEqual(offlineNotices(offline('ready', { tier: 'standard' })), []);
  assert.deepEqual(offlineNotices(offline('failed', { tier: 'flash', error: '下载模型失败' })), ['下载模型失败', FLASH_NOTICE]);
});

test('parseActionNotes warns about the first-use download', () => {
  assert.deepEqual(parseActionNotes({ install: true }), [DOWNLOAD_NOTE]);
  assert.deepEqual(parseActionNotes({ install: false }), []);
});
