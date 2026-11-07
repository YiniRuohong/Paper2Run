import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { AutonomousFile, AutonomousTask } from '../../api-client/autonomous';
import { enterReportMode } from '../../desktop';
import { Icon } from '../../ui/Icon';
import { outcomeLabel, outcomeReasonLabel } from './config';
import type { ExperimentReport, ReportDocumentData, ReportFigure } from './report';
import { parseBlocks, parseInline, type Block } from './reportMarkdown';
import './reportDocument.css';

// 图文报告（全屏阅读视图 + 导出 PDF）。
// 版式按论文组织：抬头 → 程序判定框 → 摘要 → 1–8 章 → 附录（证据清单、独立复核、程序局限）。
// 正文与图表由智能体撰写（模型解读）；判定框、附录、图的来源与哈希全部来自程序记录。
const CHAPTERS: Array<[keyof ReportDocumentData['sections'], string]> = [
  ['background', '背景'], ['setup', '实验设置'], ['method', '方法'], ['deviations', '与论文的偏差'],
  ['results', '结果'], ['discussion', '讨论'], ['limitations', '局限'], ['conclusion', '结论'],
];
const KIND_TEXT: Record<string, string> = {
  code: '代码', environment: '环境', data: '数据', result: '结果', analysis: '分析脚本', figure: '图表', log: '日志',
  checkpoint: '检查点', other: '其他',
};

type Load = (file: AutonomousFile, signal: AbortSignal) => Promise<Blob>;

/** Object URLs for the document's figures, matched to frozen artifacts by SHA256. */
function useFigureUrls(figures: ReportFigure[], files: AutonomousFile[], load: Load) {
  const [urls, setUrls] = useState<Record<string, string | 'error'>>({});
  const key = figures.map(f => f.sha256).join('|') + '#' + files.length;
  useEffect(() => {
    const controller = new AbortController();
    const created: string[] = [];
    void Promise.all(figures.map(async figure => {
      const file = files.find(f => f.source === 'artifact' && f.sha256 === figure.sha256);
      if (!file) return [figure.name, 'error'] as const;
      try {
        const blob = await load(file, controller.signal);
        const typed = file.path.toLowerCase().endsWith('.svg') ? new Blob([blob], { type: 'image/svg+xml' }) : blob;
        const url = URL.createObjectURL(typed);
        created.push(url);
        return [figure.name, url] as const;
      } catch {
        return [figure.name, 'error'] as const;
      }
    })).then(pairs => { if (!controller.signal.aborted) setUrls(Object.fromEntries(pairs)); });
    return () => { controller.abort(); created.forEach(url => URL.revokeObjectURL(url)); };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps -- key captures figures and files
  return urls;
}

function Text({ text, numbers }: { text: string; numbers: Map<string, number> }) {
  return <>{parseInline(text).map((part, i) => {
    switch (part.t) {
      case 'strong': return <strong key={i}>{part.v}</strong>;
      case 'em': return <em key={i}>{part.v}</em>;
      case 'code': return <code key={i}>{part.v}</code>;
      case 'math': return <span key={i} className="rd-math">{part.v}</span>;
      case 'figref': return <span key={i} className="rd-figref">图 {numbers.get(part.v) ?? '?'}</span>;
      default: return <Fragment key={i}>{part.v}</Fragment>;
    }
  })}</>;
}

function FigureBlock({ figure, url }: { figure: ReportFigure | undefined; url: string | 'error' | undefined }) {
  if (!figure) return <p className="rd-missing">（此处引用的图表不在冻结清单中）</p>;
  return <figure className="rd-figure">
    <div className="rd-figure-img">
      {url === undefined && <span className="rd-note">读取图表…</span>}
      {url === 'error' && <span className="rd-note">图表文件读取失败：{figure.name}</span>}
      {url && url !== 'error' && <img src={url} alt={figure.caption || figure.name} />}
    </div>
    <figcaption>
      <b>图 {figure.number}</b>　{figure.caption}
      <span className="rd-source">来源：{figure.derivedFrom.join('、')} → {figure.name} · SHA256 {figure.sha256.slice(0, 12)}…</span>
    </figcaption>
  </figure>;
}

function Blocks({ text, doc, urls, numbers }: { text: string; doc: ReportDocumentData; urls: Record<string, string | 'error'>; numbers: Map<string, number> }) {
  const render = (block: Block, i: number): ReactNode => {
    switch (block.type) {
      case 'heading': return block.level <= 2 ? <h4 key={i}><Text text={block.text} numbers={numbers} /></h4> : <h5 key={i}><Text text={block.text} numbers={numbers} /></h5>;
      case 'list': {
        const Tag = block.ordered ? 'ol' : 'ul';
        return <Tag key={i}>{block.items.map((item, j) => <li key={j}><Text text={item} numbers={numbers} /></li>)}</Tag>;
      }
      case 'quote': return <blockquote key={i}><Text text={block.text} numbers={numbers} /></blockquote>;
      case 'code': return <pre key={i}><code>{block.text}</code></pre>;
      case 'table': return <div key={i} className="rd-table-wrap"><table>
        <thead><tr>{block.head.map((cell, j) => <th key={j}><Text text={cell} numbers={numbers} /></th>)}</tr></thead>
        <tbody>{block.rows.map((row, r) => <tr key={r}>{row.map((cell, j) => <td key={j}><Text text={cell} numbers={numbers} /></td>)}</tr>)}</tbody>
      </table></div>;
      case 'figure': return <FigureBlock key={i} figure={doc.figures.find(f => f.name === block.name)} url={urls[block.name]} />;
      default: return <p key={i}><Text text={block.text} numbers={numbers} /></p>;
    }
  };
  return <>{parseBlocks(text).map(render)}</>;
}

const shortHash = (value: string | null | undefined) => (value ? `${value.slice(0, 12)}…` : '—');
const dateText = (iso: string) => { const d = new Date(iso); return Number.isNaN(d.getTime()) ? iso : d.toLocaleString('zh-CN', { hour12: false }); };

export function ReportDocument({ report, task, files, load, onClose }: {
  report: ExperimentReport; task: AutonomousTask; files: AutonomousFile[]; load: Load; onClose: () => void;
}) {
  const doc = report.document!;
  const urls = useFigureUrls(doc.figures, files, load);
  const numbers = new Map(doc.figures.map(f => [f.name, f.number]));
  const closeRef = useRef<HTMLButtonElement>(null);
  const close = useRef(onClose); close.current = onClose;

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close.current(); };
    document.addEventListener('keydown', onKey);
    const leaveReportMode = enterReportMode();
    return () => { document.removeEventListener('keydown', onKey); document.documentElement.classList.remove('rd-printing'); leaveReportMode(); opener?.focus?.(); };
  }, []);

  const exportPdf = () => {
    const root = document.documentElement;
    const title = document.title;
    const done = () => { root.classList.remove('rd-printing'); document.title = title; window.removeEventListener('afterprint', done); };
    root.classList.add('rd-printing');
    document.title = doc.sections.title.replace(/[\\/:*?"<>|]/g, ' ').slice(0, 120);
    window.addEventListener('afterprint', done);
    window.print();
  };

  const outcome = outcomeLabel(report.outcome) ?? '无结局';
  const reason = outcomeReasonLabel(report.outcomeReason);
  const verdict = report.verdict === 'not_comparable' ? '不可比较' : report.verdict;
  const candidate = report.candidate;
  const review = report.reviews.filter(r => r.snapshotSha256 === (report.outcomeSnapshotSha256 ?? candidate?.snapshotSha256)).at(-1)
    ?? report.reviews.at(-1);
  const model = task.config_snapshot?.model?.provider_profile ?? '—';

  return createPortal(<div className="rd-overlay" role="dialog" aria-modal="true" aria-label="图文实验报告">
    <div className="rd-toolbar">
      <span className="rd-toolbar-title"><Icon name="doc" size={16} />图文实验报告</span>
      <button type="button" className="btn btn-primary btn-sm" onClick={exportPdf}><Icon name="download" size={14} />导出 PDF</button>
      <button type="button" ref={closeRef} className="btn btn-icon" aria-label="关闭报告" title="关闭（Esc）" onClick={() => close.current()}><Icon name="x" size={18} /></button>
    </div>
    <article className="rd-paper">
      <header className="rd-head">
        <p className="rd-kicker">Paper2Run · 实验报告</p>
        <h1><Text text={doc.sections.title} numbers={numbers} /></h1>
        <dl className="rd-meta">
          <div><dt>实验目标</dt><dd>{task.objective}</dd></div>
          <div><dt>模型连接</dt><dd>{model}</dd></div>
          <div><dt>生成时间</dt><dd>{dateText(task.updated_at)}</dd></div>
          <div><dt>任务</dt><dd className="rd-mono">{task.id.slice(0, 8)}</dd></div>
          <div><dt>协议</dt><dd className="rd-mono">{shortHash(candidate?.protocolSha256)}</dd></div>
          <div><dt>候选快照</dt><dd className="rd-mono">{shortHash(report.outcomeSnapshotSha256 ?? candidate?.snapshotSha256)}</dd></div>
        </dl>
      </header>

      <section className="rd-verdict" aria-label="程序判定">
        <h2>程序判定</h2>
        <div className="rd-verdict-grid">
          <div><span>结局</span><b>{outcome}</b>{reason && <small>{reason}</small>}</div>
          <div><span>比较结论</span><b>{verdict}</b></div>
          <div><span>完成门槛</span><b>{report.successfulExecutionObserved ? '已观测到入口成功运行' : '未观测到入口成功运行'}</b></div>
        </div>
        {report.evaluation.reasons.length > 0 && <ul>{report.evaluation.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>}
        <p className="rd-note">本框由程序根据冻结证据与独立复核给出。下文正文与图表由智能体撰写（模型解读），不构成执行证据；如有出入，以本框与附录为准。</p>
      </section>

      <section className="rd-abstract">
        <h2>摘要</h2>
        <Blocks text={doc.sections.abstract} doc={doc} urls={urls} numbers={numbers} />
      </section>

      {CHAPTERS.map(([key, title], i) => <section key={key} className="rd-section">
        <h2><span className="rd-num">{i + 1}</span>{title}</h2>
        <Blocks text={doc.sections[key]} doc={doc} urls={urls} numbers={numbers} />
      </section>)}

      <section className="rd-section rd-appendix">
        <h2><span className="rd-num">A</span>证据清单</h2>
        <p className="rd-note">候选快照中冻结的全部产物，哈希由程序计算。</p>
        <div className="rd-table-wrap"><table>
          <thead><tr><th>产物</th><th>类型</th><th>大小</th><th>SHA256</th></tr></thead>
          <tbody>{(candidate?.artifacts ?? report.artifacts).map(a => <tr key={a.name}>
            <td className="rd-mono">{a.name}</td><td>{KIND_TEXT[a.kind] ?? a.kind}</td>
            <td>{a.bytes != null ? `${(a.bytes / 1024).toFixed(a.bytes < 10240 ? 1 : 0)} KB` : '—'}</td>
            <td className="rd-mono">{shortHash(a.sha256)}</td>
          </tr>)}</tbody>
        </table></div>
      </section>

      <section className="rd-section rd-appendix">
        <h2><span className="rd-num">B</span>独立复核</h2>
        {review ? <>
          <p>复核结论：<b>{review.verdict === 'pass' ? '通过' : review.verdict === 'issues' ? '发现问题' : review.verdict}</b>
            {review.round != null && `（第 ${review.round + 1} 轮${review.attempt != null ? `，第 ${review.attempt} 次` : ''}）`}</p>
          {review.findings.length > 0 && <ol>{review.findings.map((f, i) => <li key={i}>{f.description}</li>)}</ol>}
        </> : <p className="rd-note">没有复核记录。</p>}
      </section>

      <section className="rd-section rd-appendix">
        <h2><span className="rd-num">C</span>程序记录的局限</h2>
        <ul>{report.limitations.map((item, i) => <li key={i}>{item}</li>)}</ul>
        <p className="rd-note rd-mono">报告 SHA256 {report.sha256} · 正文 SHA256 {doc.sha256}</p>
      </section>
    </article>
  </div>, document.body);
}
