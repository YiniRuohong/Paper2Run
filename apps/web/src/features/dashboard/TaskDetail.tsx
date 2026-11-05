import { useEffect, useMemo, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { validateAttempt } from '../../api-client/guided';
import type { GuidedRunEvent } from '../../api-client/guided-types';
import { CardHeader, KV, KVList } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { Workbench, type StageMark, type WbContent, type WbFile, type WbStage } from '../../ui/workbench/Workbench';
import { describeError, loadReport, loadSnapshot, type ErrorInfo, type ReportView } from './data';
import { ACTIVE_RUN, buildTasks, MODE_LABEL, RUN_LABEL, VERDICT_LABEL, when, type Task } from './tasks';

// Read-only detail of one task (paper) as a workbench: the left timeline is the
// task's lifecycle (paper → run → report → evidence), the centre shows the
// summary, the right lists the run's real files — report, event log, the code
// of every attempt and every evidence file — previewed straight from the server.
//   <TaskDetail api={api} paperId="pap_…" onBack={…} refreshSignal={tick} />
const EVIDENCE_TEXT: Record<string, string> = {
  real_local_cpu: '真实本机 CPU 实验',
  controlled_smoke: '受控教学 smoke（不是论文复现）',
  sandboxed_model_code: '沙箱内执行模型生成代码（断网、限额、仅标准库）：近似复现，非论文原代码原数据',
  unclassified: '未分类',
};
const MEDIA_EXT: Record<string, string> = {
  'application/json': 'json', 'text/plain': 'txt', 'text/markdown': 'md', 'text/csv': 'csv', 'application/pdf': 'pdf',
  'image/png': 'png', 'image/jpeg': 'jpg', 'image/svg+xml': 'svg', 'application/zip': 'zip', 'text/x-python': 'py',
};
const EVENTS_TIMEOUT_MS = 8000;

interface RunFiles { files: WbFile[]; text: Map<string, string>; artifacts: Map<string, string>; events: GuidedRunEvent[]; failed: string[] }
const NO_FILES: RunFiles = { files: [], text: new Map(), artifacts: new Map(), events: [], failed: [] };

export default function TaskDetail({ api, paperId, onBack, refreshSignal = 0 }: { api: GuidedApi; paperId: string; onBack: () => void; refreshSignal?: number }) {
  const [task, setTask] = useState<Task | null | undefined>(undefined);
  const [report, setReport] = useState<ReportView | null>(null);
  const [rawReport, setRawReport] = useState<unknown>(null);
  const [runFiles, setRunFiles] = useState<RunFiles | null>(null);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [rev, setRev] = useState(0);

  useEffect(() => {
    const ctl = new AbortController();
    setError(null); setRunFiles(null);
    (async () => {
      const { snapshot } = await loadSnapshot(api, ctl.signal);
      const found = buildTasks(snapshot).find(t => t.paperId === paperId) ?? null;
      const rep = found?.run ? await loadReport(api, found.run.id, ctl.signal) : null;
      if (ctl.signal.aborted) return;
      setTask(found); setReport(rep);
      if (!found?.run) { setRawReport(null); setRunFiles(NO_FILES); return; }
      const [raw, files] = await Promise.all([
        rep ? api.read(`/runs/${encodeURIComponent(found.run.id)}/report`, (v: unknown) => v, ctl.signal).catch(() => null) : Promise.resolve(null),
        loadRunFiles(api, found.run.id, found.run.status, rep, ctl.signal),
      ]);
      if (!ctl.signal.aborted) { setRawReport(raw); setRunFiles(files); }
    })().catch(e => {
      if (ctl.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) return;
      setError(describeError(e));
    });
    return () => ctl.abort();
  }, [api, paperId, rev, refreshSignal]);

  const files = useMemo(() => {
    if (!runFiles) return null;
    const list = [...runFiles.files];
    const text = new Map(runFiles.text);
    if (rawReport) { const body = JSON.stringify(rawReport, null, 2); text.set('报告/report.json', body); list.push({ path: '报告/report.json', bytes: new TextEncoder().encode(body).length, mediaType: 'application/json', badge: '记录' }); }
    return { list, text };
  }, [runFiles, rawReport]);

  const status = task?.run ? RUN_LABEL[task.run.status] ?? { text: task.run.status, tone: 'idle' as const } : null;
  const back = <button type="button" className="btn btn-secondary btn-sm" onClick={onBack}><Icon name="chevron-left" size={14} />返回总览</button>;

  if (error) return <section className="ui-card dash-error" role="alert">
    <CardHeader icon="alert" title={error.title} status={{ text: '读取失败', tone: 'bad' }} subtitle={error.hint}
      actions={<button type="button" className="btn btn-secondary" onClick={() => setRev(x => x + 1)}>重试</button>} />
    <div className="ui-card-section"><Disclosure variant="sub" title="错误详情"><code className="ui-mono">{error.detail}</code></Disclosure></div>
  </section>;
  if (task === undefined) return <div className="ui-card detail-skel" aria-hidden="true"><span className="ui-skel w60" /><span className="ui-skel w40" /></div>;
  if (task === null) return <div className="ui-card ui-empty"><span className="ui-tile"><Icon name="doc" size={18} /></span><h3>找不到这个任务</h3><p>它可能已被移除，或属于其他用户。</p>{back}</div>;

  const run = task.run;
  const runMark: StageMark = !run ? 'open' : run.status === 'completed' ? 'done' : run.status === 'failed' ? 'failed' : ACTIVE_RUN.has(run.status) ? 'running' : 'attention';
  const evidenceFiles = (runFiles?.files ?? []).filter(f => f.badge === '证据');
  const codeFiles = (runFiles?.files ?? []).filter(f => f.badge === '代码');
  const events = runFiles?.events ?? [];
  const stages: WbStage[] = [
    { id: 'paper', title: '上传论文', mark: 'done', summary: `创建于 ${when(task.createdAt)}`,
      items: [{ id: 'pid', label: task.title, detail: task.paperId }] },
    { id: 'run', title: '运行实验', mark: runMark,
      summary: run ? `${status?.text ?? run.status} · ${MODE_LABEL[run.mode ?? ''] ?? run.mode ?? '—'} · 共 ${task.runCount} 次` : '还没有运行',
      items: run ? [
        { id: 'run', label: `最近运行 ${when(run.createdAt)}`, detail: run.id, tone: status?.tone === 'run' ? 'run' : status?.tone === 'bad' ? 'bad' : status?.tone === 'ok' ? 'ok' : undefined },
        ...codeFiles.slice(0, 4).map(f => ({ id: f.path, label: f.path.split('/').slice(-1)[0], detail: '实验代码', openPath: f.path })),
        ...events.slice(-8).reverse().map(e => ({ id: `e${e.seq}`, label: e.type, detail: `#${e.seq}`, time: clockOf(e.occurred_at), openPath: '运行/events.log' })),
      ] : [] },
    { id: 'report', title: '生成报告', mark: report ? 'done' : run?.status === 'completed' ? 'attention' : 'locked',
      summary: report ? (report.verdict ? VERDICT_LABEL[report.verdict] ?? report.verdict : '报告已生成') : run?.status === 'completed' ? '运行已完成，报告尚未生成' : '等待运行完成',
      items: report ? [
        ...(report.evidenceClass ? [{ id: 'ec', label: EVIDENCE_TEXT[report.evidenceClass] ?? report.evidenceClass, openPath: files?.text.has('报告/report.json') ? '报告/report.json' : undefined }] : []),
        ...report.metrics.slice(0, 5).map(([k, v]) => ({ id: `m-${k}`, label: `${k} = ${v}` })),
      ] : [] },
    { id: 'evidence', title: '证据文件', mark: evidenceFiles.length > 0 ? 'done' : report ? 'attention' : 'locked',
      summary: report ? `${report.evidence.length} 个证据文件 · ${report.limitations.length} 条局限性` : '报告生成后列出',
      items: evidenceFiles.slice(0, 8).map(f => ({ id: f.path, label: f.path.split('/').slice(-1)[0], detail: f.mediaType, tone: 'ok' as const, openPath: f.path })) },
  ];
  const activeStage = !run ? 'run' : !report ? (run.status === 'completed' ? 'report' : 'run') : 'evidence';
  const loadFile = async (file: WbFile, signal: AbortSignal): Promise<WbContent> => {
    const text = files?.text.get(file.path);
    if (text !== undefined) return { text };
    const artifact = runFiles?.artifacts.get(file.path);
    if (artifact) return { blob: await api.readBlob(`/artifacts/${encodeURIComponent(artifact)}/raw`, signal) };
    throw new Error('该文件已不在当前任务中');
  };

  return <Workbench
    eyebrow={<>任务详情 · 只读</>}
    title={task.title}
    status={status ? { text: status.text, tone: status.tone } : { text: '尚未运行', tone: 'idle' }}
    meta={<>
      <span>创建于 {when(task.createdAt)}</span>
      <span>{task.runCount > 0 ? `${task.runCount} 次运行` : '未运行'}</span>
      {report?.verdict && <span>结论 {VERDICT_LABEL[report.verdict] ?? report.verdict}</span>}
    </>}
    actions={back}
    progress={`进度 · ${stages.filter(s => s.mark === 'done').length} / ${stages.length}`}
    stages={stages}
    activeStageId={activeStage}
    files={files?.list ?? null}
    filesEmpty={run ? '这次运行还没有可读取的文件。' : '还没有运行，没有产出文件。'}
    filesNote={runFiles && runFiles.failed.length > 0 ? <>部分文件读取失败：{runFiles.failed.join('；')}</> : <>代码与证据按 SHA-256 从服务器读取，与报告登记一致。</>}
    onRefreshFiles={() => setRev(x => x + 1)}
    loadFile={loadFile}
  >
    <div className="detail">
      <section className="ui-card">
        <div className="ui-card-section"><KVList><KV label="论文 ID" value={task.paperId} copy /></KVList></div>
      </section>
      {!run && <div className="ui-card ui-empty"><h3>还没有运行</h3><p>这篇论文已上传，但还没有创建运行。到"新建任务"里继续引导流程。</p></div>}
      {run && <>
        <div className="wb-stat-grid">
          <div className="wb-stat"><span>运行状态</span><b>{status?.text ?? run.status}</b></div>
          <div className="wb-stat"><span>方式</span><b>{MODE_LABEL[run.mode ?? ''] ?? run.mode ?? '—'}</b></div>
          <div className="wb-stat"><span>结论</span><b>{report?.verdict ? VERDICT_LABEL[report.verdict] ?? report.verdict : '—'}</b></div>
          <div className="wb-stat"><span>证据文件</span><b>{report ? report.evidence.length : '—'}</b></div>
        </div>
        <Disclosure variant="card" title="运行" defaultOpen meta={status ? <span className={`ui-status tone-${status.tone}`}>{status.text}</span> : undefined}>
          <KVList>
            <KV label="方式" mono={false} value={MODE_LABEL[run.mode ?? ''] ?? run.mode ?? '—'} />
            <KV label="结论" mono={false} value={report?.verdict ? VERDICT_LABEL[report.verdict] ?? report.verdict : '报告尚未生成'} />
            <KV label="运行次数" value={task.runCount} />
            <KV label="最近运行" value={run.id} copy />
            <KV label="时间" value={when(run.createdAt)} />
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
    </div>
  </Workbench>;
}

function clockOf(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** Read every real file the run registered. Each source fails independently. */
async function loadRunFiles(api: GuidedApi, runId: string, status: string, report: ReportView | null, signal: AbortSignal): Promise<RunFiles> {
  const out: RunFiles = { files: [], text: new Map(), artifacts: new Map(), events: [], failed: [] };
  const enc = encodeURIComponent;
  const tasks: Promise<void>[] = [];
  // Events: only once the run is terminal — an active run's endpoint may stream.
  if (!ACTIVE_RUN.has(status)) tasks.push((async () => {
    const timeout = new AbortController();
    const onAbort = () => timeout.abort();
    signal.addEventListener('abort', onAbort);
    const timer = setTimeout(() => timeout.abort(), EVENTS_TIMEOUT_MS);
    try {
      const events = await api.runEvents(runId, 0, timeout.signal);
      out.events = events;
      if (events.length) {
        const body = events.map(e => `#${e.seq}  ${e.occurred_at}  ${e.type}  ${JSON.stringify(e.payload)}`).join('\n');
        out.text.set('运行/events.log', body);
        out.files.push({ path: '运行/events.log', bytes: new TextEncoder().encode(body).length, mediaType: 'text/plain', badge: '记录' });
      }
    } catch { if (!signal.aborted) out.failed.push('运行事件'); }
    finally { clearTimeout(timer); signal.removeEventListener('abort', onAbort); }
  })());
  // Code of every attempt (registered manifest files, hash-checked server side).
  tasks.push((async () => {
    try {
      const attempts = (await api.readArray(`/runs/${enc(runId)}/attempts`, validateAttempt, signal)).items;
      for (const attempt of attempts) {
        let cursor: string | null = null;
        for (let page = 0; page < 20; page++) {
          const res: { items: Array<{ path: string; artifact_id: string; bytes: number; sha256: string; media_type: string }>; next_cursor: string | null } =
            await api.readArray(`/attempts/${enc(attempt.attempt_id)}/files?limit=100${cursor ? `&cursor=${enc(cursor)}` : ''}`, v => v as { path: string; artifact_id: string; bytes: number; sha256: string; media_type: string }, signal);
          for (const f of res.items) {
            const path = `代码/attempt-${attempt.attempt_index}/${f.path}`;
            if (out.artifacts.has(path)) continue;
            out.artifacts.set(path, f.artifact_id);
            out.files.push({ path, bytes: f.bytes, mediaType: f.media_type, sha256: f.sha256, badge: '代码' });
          }
          cursor = res.next_cursor;
          if (!cursor) break;
        }
      }
    } catch { if (!signal.aborted) out.failed.push('实验代码'); }
  })());
  await Promise.all(tasks);
  // Evidence registered by the report.
  for (const e of report?.evidence ?? []) {
    const ext = MEDIA_EXT[(e.mediaType ?? '').split(';', 1)[0].trim()] ?? 'bin';
    const path = `证据/${e.sha256.slice(0, 12)}.${ext}`;
    if (out.artifacts.has(path)) continue;
    out.artifacts.set(path, e.sha256);
    out.files.push({ path, bytes: e.bytes, mediaType: e.mediaType, sha256: e.sha256, badge: '证据' });
  }
  return out;
}
