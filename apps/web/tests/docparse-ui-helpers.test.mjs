import test from 'node:test';
import assert from 'node:assert/strict';
import { docParseErrorReason, formatBytes, otherParseMode } from '../src/features/docparse/docparse.ts';

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

test('otherParseMode offers the opposite mode only when it is available', () => {
  assert.equal(otherParseMode(null), null);
  assert.equal(otherParseMode(status(null, 'ready')), null);
  assert.equal(otherParseMode(status('online', 'ready')), 'offline');
  assert.equal(otherParseMode(status('online', 'not_installed')), 'offline');
  assert.equal(otherParseMode(status('online', 'unsupported')), null);
  assert.equal(otherParseMode(status('offline', 'unsupported')), 'online');
  assert.equal(otherParseMode(status('offline', 'failed')), 'online');
});
