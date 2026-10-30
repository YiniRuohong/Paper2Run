import { useEffect, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { Icon } from './Icon';
import { buildTasks, MODE_LABEL, RUN_LABEL, VERDICT_LABEL, when, type Task } from './Dashboard';
import { describeError, loadReport, loadSnapshot, type ErrorInfo, type ReportView } from './data';

// Read-only detail of one task (paper): latest run status and its report.
const EVIDENCE_TEXT: Record<string, string> = {
  real_local_cpu: '真实本机 CPU 实验',
  controlled_smoke: '受控教学 smoke（不是论文复现）',
  sandboxed_model_code: '沙箱内执行模型生成代码（断网、限额、仅标准库）：近似复现，非论文原代码原数据',
  unclassified: '未分类',
};

export default function TaskDetail({ api, paperId, onBack }: { api: GuidedApi; paperId: string; onBack: () => void }) {
  const [task, setTask] = useState<Task | null | undefined>(undefined);
  const [report, setReport] = useState<ReportView | null>(null);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [rev, setRev] = useState(0);

  useEffect(() => {
    const ctl = new AbortController();
    setError(null);
    (async () => {
      const { snapshot } = await loadSnapshot(api, ctl.signal);
      const found = buildTasks(snapshot).find(t => t.paperId === paperId) ?? null;
      const rep = found?.run ? await loadReport(api, found.run.id, ctl.signal) : null;
      if (!ctl.signal.aborted) { setTask(found); setReport(rep); }
    })().catch(e => {
      if (ctl.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) return;
      setError(describeError(e));
    });
    return () => ctl.abort();
  }, [api, paperId, rev]);

  const status = task?.run ? RUN_LABEL[task.run.status] ?? { text: task.run.status, tone: 'idle' as const } : null;
  return <div className="detail">
    <button className="guided-link" onClick={onBack}>← 返回总览</button>
    {error && <section className="dash-error" role="alert">
      <div className="dash-error-head"><Icon name="alert" /><b>{error.title}</b></div>
      <p>{error.hint}</p><code>{error.detail}</code>
      <div className="dash-error-actions"><button className="btn-black sm" onClick={() => setRev(x => x + 1)}>重试</button></div>
    </section>}
    {task === undefined && !error && <div className="card detail-card"><span className="skel w60" /><span className="skel w40" /></div>}
    {task === null && <div className="card empty"><h3>找不到这个任务</h3><p>它可能已被移除，或属于其他用户。</p><button className="btn-ghost" onClick={onBack}>返回总览</button></div>}
    {task && <>
      <header className="detail-head">
        <h1>{task.title}</h1>
        <div className="detail-meta">
          {status ? <span className={`pill ${status.tone}`}>{status.text}</span> : <span className="pill">尚未运行</span>}
          <code>{task.paperId}</code><span>创建于 {when(task.createdAt)}</span>
        </div>
      </header>
      {!task.run && <div className="card empty"><h3>还没有运行</h3><p>这篇论文已上传，但还没有创建运行。到"新建任务"里继续引导流程。</p></div>}
      {task.run && <div className="detail-cols">
        <section className="card detail-card">
          <h2>运行</h2>
          <dl className="kv">
            <dt>方式</dt><dd>{MODE_LABEL[task.run.mode ?? ''] ?? task.run.mode ?? '—'}</dd>
            <dt>结论</dt><dd>{task.run.verdict ? VERDICT_LABEL[task.run.verdict] ?? task.run.verdict : '—'}</dd>
            <dt>运行次数</dt><dd>{task.runCount}</dd>
            <dt>最近运行</dt><dd><code>{task.run.id}</code></dd>
            <dt>时间</dt><dd>{when(task.run.createdAt)}</dd>
          </dl>
        </section>
        <section className="card detail-card">
          <h2>指标</h2>
          {report && report.metrics.length > 0
            ? <div className="metrics">{report.metrics.map(([k, v]) => <div key={k}><span>{k}</span><b>{v}</b></div>)}</div>
            : <p className="dash-muted">{report ? '报告中没有指标。' : '报告尚未生成。'}</p>}
        </section>
        {report && <section className="card detail-card wide">
          <h2>证据与可信度</h2>
          <dl className="kv">
            <dt>证据等级</dt><dd>{report.evidenceClass ? EVIDENCE_TEXT[report.evidenceClass] ?? report.evidenceClass : '—'}</dd>
            <dt>Recipe</dt><dd><code>{report.recipe ?? '—'}</code></dd>
            <dt>评测器</dt><dd><code>{report.evaluator ?? '—'}</code></dd>
            <dt>Manifest</dt><dd><code>{report.manifestHash ? report.manifestHash.slice(0, 16) + '…' : '—'}</code></dd>
          </dl>
          {report.limitations.length > 0 && <><h3>局限性</h3><ul className="limits">{report.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul></>}
          {report.evidence.length > 0 && <><h3>证据文件</h3><ul className="limits">{report.evidence.map(e => <li key={e.sha256}><code>{e.sha256.slice(0, 16)}…</code> · {e.mediaType ?? '—'}{e.bytes !== null ? ` · ${e.bytes} B` : ''}</li>)}</ul></>}
        </section>}
      </div>}
    </>}
  </div>;
}
