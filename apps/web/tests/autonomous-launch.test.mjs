import test from 'node:test';
import assert from 'node:assert/strict';
import { launchBlockReason, paperIsPdf } from '../src/features/autonomous/launch.ts';

const textStatus = status => ({ status, backend: null, page_count: 0, pages_parsed: 0, chunks: [], warnings: [], error_code: null, preview: null });
const ready = { projectId: 'p', paperVersion: 'v', authorized: true, mainProfile: 'm', objective: 'goal' };

test('paper kind comes from the stored source kind, not from the text status', () => {
  assert.equal(paperIsPdf({ source_kind: 'text', media_type: 'text/plain' }), false);
  assert.equal(paperIsPdf({ source_kind: 'pdf', media_type: 'application/pdf' }), true);
  assert.equal(paperIsPdf({ media_type: 'application/pdf' }), true, 'media type is the fallback');
  assert.equal(paperIsPdf({}), false, 'unknown kind does not invent a parse requirement; the server still gates content');
});

test('a pasted-text paper with text status none does not block launch', () => {
  const pdf = paperIsPdf({ source_kind: 'text', media_type: 'text/plain' });
  assert.equal(launchBlockReason({ ...ready, pdf, text: textStatus('none') }), '');
});

test('an unparsed or failed PDF still blocks launch with a reason', () => {
  const pdf = paperIsPdf({ source_kind: 'pdf', media_type: 'application/pdf' });
  assert.match(launchBlockReason({ ...ready, pdf, text: textStatus('none') }), /尚未解析/);
  assert.match(launchBlockReason({ ...ready, pdf, text: textStatus('parsing') }), /解析中/);
  assert.match(launchBlockReason({ ...ready, pdf, text: textStatus('failed') }), /解析失败/);
  assert.match(launchBlockReason({ ...ready, pdf, text: null }), /尚未解析/);
  assert.equal(launchBlockReason({ ...ready, pdf, text: textStatus('ready') }), '');
});

test('launch reasons keep their order: project, paper, parse, authorization, model, objective', () => {
  const base = { ...ready, pdf: false, text: null };
  assert.equal(launchBlockReason({ ...base, projectId: '' }), '请先选择或创建项目');
  assert.equal(launchBlockReason({ ...base, paperVersion: '' }), '请先上传或选择论文');
  assert.equal(launchBlockReason({ ...base, authorized: false }), '请先勾选授权');
  assert.equal(launchBlockReason({ ...base, mainProfile: '' }), '请先选择主模型');
  assert.equal(launchBlockReason({ ...base, objective: '  ' }), '请填写实验目标');
});
