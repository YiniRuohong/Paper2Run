// 报告正文的 Markdown 子集 → 结构化块（纯函数，不产出 HTML 字符串，渲染时由 React 转义）。
// 支持：标题、段落、有序/无序列表、引用、代码块、管道表格、单独一行的 {{figure:名称}}；
// 行内：**粗体**、*斜体*、`代码`、$公式$、{{figure:名称}}（显示为“图 N”）。

export type Inline =
  | { t: 'text'; v: string } | { t: 'strong'; v: string } | { t: 'em'; v: string }
  | { t: 'code'; v: string } | { t: 'math'; v: string } | { t: 'figref'; v: string };

export type Block =
  | { type: 'heading'; level: number; text: string }
  | { type: 'para'; text: string }
  | { type: 'list'; ordered: boolean; items: string[] }
  | { type: 'quote'; text: string }
  | { type: 'code'; text: string }
  | { type: 'table'; head: string[]; rows: string[][] }
  | { type: 'figure'; name: string };

const FIGURE_LINE = /^\{\{figure:([^{}\s]+)\}\}$/;
const HEADING = /^(#{1,6})\s+(.*)$/;
const BULLET = /^\s*[-*+]\s+(.*)$/;
const NUMBERED = /^\s*\d+[.)]\s+(.*)$/;
const TABLE_SEP = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const cells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());

export function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n?/g, '\n').split('\n');
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();
    if (!trimmed) { i++; continue; }
    const figure = FIGURE_LINE.exec(trimmed);
    if (figure) { blocks.push({ type: 'figure', name: figure[1] }); i++; continue; }
    if (trimmed.startsWith('```')) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith('```')) body.push(lines[i++]);
      i++;
      blocks.push({ type: 'code', text: body.join('\n') });
      continue;
    }
    const heading = HEADING.exec(trimmed);
    if (heading) { blocks.push({ type: 'heading', level: heading[1].length, text: heading[2].trim() }); i++; continue; }
    if (trimmed.startsWith('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1])) {
      const head = cells(trimmed);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(cells(lines[i++]));
      blocks.push({ type: 'table', head, rows: rows.map(r => head.map((_, c) => r[c] ?? '')) });
      continue;
    }
    if (BULLET.test(line) || NUMBERED.test(line)) {
      const ordered = !BULLET.test(line);
      const pattern = ordered ? NUMBERED : BULLET;
      const items: string[] = [];
      while (i < lines.length && pattern.test(lines[i])) items.push(pattern.exec(lines[i++])![1].trim());
      blocks.push({ type: 'list', ordered, items });
      continue;
    }
    if (trimmed.startsWith('>')) {
      const body: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith('>')) body.push(lines[i++].trim().replace(/^>\s?/, ''));
      blocks.push({ type: 'quote', text: body.join(' ') });
      continue;
    }
    const body: string[] = [];
    while (i < lines.length) {
      const next = lines[i].trim();
      if (!next || FIGURE_LINE.test(next) || HEADING.test(next) || next.startsWith('```') || next.startsWith('>')
        || BULLET.test(lines[i]) || NUMBERED.test(lines[i])
        || (next.startsWith('|') && i + 1 < lines.length && TABLE_SEP.test(lines[i + 1]))) break;
      body.push(next);
      i++;
    }
    blocks.push({ type: 'para', text: body.join(' ') });
  }
  return blocks;
}

const INLINE = /(\*\*[^*]+\*\*|`[^`]+`|\$[^$\n]+\$|\{\{figure:[^{}\s]+\}\}|\*[^*\s][^*]*\*)/g;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    const token = match[0];
    const at = match.index ?? 0;
    if (at > last) out.push({ t: 'text', v: text.slice(last, at) });
    if (token.startsWith('**')) out.push({ t: 'strong', v: token.slice(2, -2) });
    else if (token.startsWith('`')) out.push({ t: 'code', v: token.slice(1, -1) });
    else if (token.startsWith('$')) out.push({ t: 'math', v: token.slice(1, -1) });
    else if (token.startsWith('{{')) out.push({ t: 'figref', v: token.slice('{{figure:'.length, -2) });
    else out.push({ t: 'em', v: token.slice(1, -1) });
    last = at + token.length;
  }
  if (last < text.length) out.push({ t: 'text', v: text.slice(last) });
  return out;
}
