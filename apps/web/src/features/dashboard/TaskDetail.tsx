import { useEffect, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { CardHeader, KV, KVList } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { describeError, loadReport, loadSnapshot, type ErrorInfo, type ReportView } from './data';
import { buildTasks, MODE_LABEL, RUN_LABEL, VERDICT_LABEL, when, type Task } from './tasks';

// Read-only detail of one task (paper): latest run status and its report.
//   <TaskDetail api={api} paperId="pap_…" onBack={…} refreshSignal={tick} />
const EVIDENCE_TEXT: Record<string, string> = {
  real_local_cpu: '真实本机 CPU 实验',
  controlled_smoke: '受控教学 smoke（不是论文复现）',
  sandboxed_model_code: '沙箱内执行模型生成代码（断网、限额、仅标准库）：近似复现，非论文原代码原数据',
  unclassified: '未分类',
};

export default function TaskDetail({ api, paperId, onBack, refreshSignal = 0 }: { api: GuidedApi; paperId: string; onBack: () => void; refreshSignal?: number }) {
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
  }, [api, paperId, rev, refreshSignal]);

  const status = task?.run ? RUN_LABEL[task.run.status] ?? { text: task.run.status, tone: 'idle' as const } : null;
  const back = <button type="button" className="btn btn-secondary" onClick={onBack}><Icon name="chevron-left" size={14} />返回总览</button>;
  return <div className="detail">
    {error && <section className="ui-card dash-error" role="alert">
      <CardHeader icon="alert" title={error.title} status={{ text: '读取失败', tone: 'bad' }} subtitle={error.hint}
        actions={<button type="button" className="btn btn-secondary" onClick={() => setRev(x => x + 1)}>重试</button>} />
      <div className="ui-card-section"><Disclosure variant="sub" title="错误详情"><code className="ui-mono">{error.detail}</code></Disclosure></div>
    </section>}
    {task === undefined && !error && <div className="ui-card detail-skel" aria-hidden="true"><span className="ui-skel w60" /><span className="ui-skel w40" /></div>}
    {task === null && <div className="ui-card ui-empty"><span className="ui-tile"><Icon name="doc" size={18} /></span><h3>找不到这个任务</h3><p>它可能已被移除，或属于其他用户。</p>{back}</div>}
    {task && <>
      <section className="ui-card">
        <CardHeader as="h1" icon="doc" title={task.title} status={status ?? { text: '尚未运行', tone: 'idle' }}
          subtitle={`创建于 ${when(task.createdAt)} · ${task.runCount > 0 ? `${task.runCount} 次运行` : '未运行'}`} actions={back} />
        <div className="ui-card-section"><KVList><KV label="论文 ID" value={task.paperId} copy /></KVList></div>
      </section>
      {!task.run && <div className="ui-card ui-empty"><h3>还没有运行</h3><p>这篇论文已上传，但还没有创建运行。到"新建任务"里继续引导流程。</p></div>}
      {task.run && <>
        <Disclosure variant="card" title="运行" defaultOpen meta={status ? <span className={`ui-status tone-${status.tone}`}>{status.text}</span> : undefined}>
          <KVList>
            <KV label="方式" mono={false} value={MODE_LABEL[task.run.mode ?? ''] ?? task.run.mode ?? '—'} />
            <KV label="结论" mono={false} value={report?.verdict ? VERDICT_LABEL[report.verdict] ?? report.verdict : '报告尚未生成'} />
            <KV label="运行次数" value={task.runCount} />
            <KV label="最近运行" value={task.run.id} copy />
            <KV label="时间" value={when(task.run.createdAt)} />
          </KVList>
        </Disclosure>
        <Disclosure variant="card" title="指标" count={report && report.metrics.length > 0 ? report.metrics.length : null} defaultOpen>
          {report && report.metrics.length > 0
            ? <div className="metrics">{report.metrics.map(([k, v]) => <div key={k}><span>{k}</span><b>{v}</b></div>)}</div>
            : <p className="ui-help detail-none">{report ? '报告中没有指标。' : '报告尚未生成。'}</p>}
        </Disclosure>
        {report && <Disclosure variant="card" title="证据与可信度" defaultOpen>
          <KVList>
            <KV label="证据等级" mono={false} value={report.evidenceClass ? EVIDENCE_TEXT[report.evidenceClass] ?? report.evidenceClass : '—'} />
            <KV label="Recipe" value={report.recipe ?? '—'} />
            <KV label="评测器" value={report.evaluator ?? '—'} />
            <KV label="Manifest" value={report.manifestHash ? report.manifestHash.slice(0, 16) + '…' : '—'} copy={report.manifestHash ?? false} />
          </KVList>
          <div className="detail-nested">
            <Disclosure title="局限性" count={report.limitations.length} defaultOpen={report.limitations.length > 0}>
              {report.limitations.length > 0 ? <ul className="limits">{report.limitations.map((l, i) => <li key={i}>{l}</li>)}</ul> : <p className="ui-help">报告未列出局限性。</p>}
            </Disclosure>
            <Disclosure title="证据文件" count={report.evidence.length}>
              {report.evidence.length > 0
                ? <ul className="ui-list">{report.evidence.map(e => <li key={e.sha256} className="ui-row">
                  <Icon name="doc" className="ico-lead" />
                  <span className="ui-row-main"><span className="ui-row-title ui-mono">{e.sha256.slice(0, 16)}…</span><span className="ui-row-sub">{e.mediaType ?? '—'}{e.bytes !== null ? ` · ${e.bytes} B` : ''}</span></span>
                  <span className="ui-row-end"><CopyButton value={e.sha256} label="复制 SHA-256" /></span>
                </li>)}</ul>
                : <p className="ui-help">报告中没有证据文件。</p>}
            </Disclosure>
          </div>
        </Disclosure>}
      </>}
    </>}
  </div>;
}
