import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { parseBlocks, parseInline } from '../src/features/autonomous/reportMarkdown.ts';
import { parseDocument, parseReport, REPORT_SECTIONS } from '../src/features/autonomous/report.ts';

test('report markdown: figures, tables, lists, headings and paragraphs become blocks', () => {
  const blocks = parseBlocks([
    '## 主要结果', '', '三种激活函数的最终准确率如下表。', '续行。', '',
    '| 激活 | 准确率 |', '|---|---:|', '| GELU | 0.9763 |', '| ReLU | 0.9762 |', '',
    '{{figure:accuracy.png}}', '', '- 第一点', '- 第二点', '', '1. 甲', '2. 乙', '', '> 注意', '', '```', 'x = 1', '```',
  ].join('\n'));
  assert.deepEqual(blocks.map(b => b.type), ['heading', 'para', 'table', 'figure', 'list', 'list', 'quote', 'code']);
  assert.equal(blocks[1].text, '三种激活函数的最终准确率如下表。 续行。');
  assert.deepEqual(blocks[2].head, ['激活', '准确率']);
  assert.deepEqual(blocks[2].rows, [['GELU', '0.9763'], ['ReLU', '0.9762']]);
  assert.equal(blocks[3].name, 'accuracy.png');
  assert.equal(blocks[4].ordered, false);
  assert.equal(blocks[5].ordered, true);
  assert.equal(blocks[7].text, 'x = 1');
});

test('report markdown inline: emphasis, code, math and figure references; no HTML is produced', () => {
  const parts = parseInline('如 {{figure:a.png}} 所示，**GELU** 的 `acc` 为 $x\\Phi(x)$，*略高*。<script>');
  assert.deepEqual(parts.map(p => p.t), ['text', 'figref', 'text', 'strong', 'text', 'code', 'text', 'math', 'text', 'em', 'text']);
  assert.equal(parts.at(-1).v, '。<script>');
});

const SECTIONS = Object.fromEntries(REPORT_SECTIONS.map(name => [name, `${name} text`]));
const DOCUMENT = { sections: SECTIONS, sha256: 'd'.repeat(64), figures: [
  { name: 'fig.png', number: 1, caption: '对比', derived_from: ['plot.py', 'results.json'], sha256: 'f'.repeat(64), bytes: 10 }] };

test('parseDocument reads the frozen document and rejects a malformed one', () => {
  const doc = parseDocument(DOCUMENT);
  assert.equal(doc.figures[0].derivedFrom[1], 'results.json');
  assert.equal(doc.sections.conclusion, 'conclusion text');
  assert.equal(parseDocument({ ...DOCUMENT, sections: { ...SECTIONS, results: 3 } }), null);
  assert.equal(parseDocument(null), null);
});

test('the task page opens the document reader only for reports that carry a document', async () => {
  const page = await readFile(new URL('../src/features/autonomous/TaskPage.tsx', import.meta.url), 'utf8');
  assert.match(page, /parsedReport\?\.document \? parsedReport : null/);
  assert.match(page, /<ReportDocument report=\{reportDoc\}/);
  const css = await readFile(new URL('../src/features/autonomous/reportDocument.css', import.meta.url), 'utf8');
  assert.match(css, /@page \{ size: A4/);
  assert.match(css, /html\.rd-printing body > \*:not\(\.rd-overlay\) \{ display: none !important; \}/);
});

test('an old report without a document still parses', () => {
  assert.equal(parseDocument(undefined), null);
  assert.equal(typeof parseReport, 'function');
});
