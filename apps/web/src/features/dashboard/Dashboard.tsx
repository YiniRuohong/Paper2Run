import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { Icon } from './Icon';
import { describeError, loadSnapshot, type LoadIssue, type Snapshot, type TaskRun } from './data';

// Read-only control panel. GET requests only: creating anything happens in the
// guided new-task flow. Loading is fault tolerant: a failing project or
// resource degrades to a warning instead of blanking the whole page.

type Phase = 'loading' | 'ready' | 'error';
type Filter = 'all' | 'active' | 'done' | 'failed';
type Layout = 'grid' | 'list';

export const RUN_LABEL: Record<string, { text: string; tone: 'ok' | 'run' | 'bad' | 'idle' }> = {
  completed: { text: '已完成', tone: 'ok' },
  running: { text: '执行中', tone: 'run' },
  evaluating: { text: '评估中', tone: 'run' },
  queued: { text: '排队中', tone: 'run' },
  created: { text: '已登记', tone: 'idle' },
  failed: { text: '失败', tone: 'bad' },
  cancelled: { text: '已取消', tone: 'idle' },
  cancel_requested: { text: '取消中', tone: 'idle' },
  awaiting_revision: { text: '待修订', tone: 'idle' },
};
const PAPER_LABEL: Record<string, { text: string; tone: 'ok' | 'run' | 'bad' | 'idle' }> = {
  uploaded: { text: '已上传', tone: 'idle' },
  processing: { text: '处理中', tone: 'run' },
  ready: { text: '就绪', tone: 'idle' },
  failed: { text: '失败', tone: 'bad' },
};
export const MODE_LABEL: Record<string, string> = { 'local-cpu': '本机 CPU', 'local-fixture': '教学 fixture', sandbox: '沙箱', 'sandbox-python': '沙箱' };
export const VERDICT_LABEL: Record<string, string> = { matched: '与论文吻合', mismatched: '与论文不符', inconclusive: '无法判定', not_comparable: '不可直接比较' };
const ACTIVE = new Set(['queued', 'running', 'evaluating', 'cancel_requested']);
const AUTO_RETRY_MS = 8000;

export function when(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export interface Task {
  paperId: string;
  title: string;
  paperStatus: string;
  createdAt: string | null;
  run: TaskRun | null;
  runCount: number;
}

export function buildTasks(snap: Snapshot): Task[] {
  const expToPaper = new Map(snap.experiments.map(e => [e.id, e.paperId]));
  const byPaper = new Map<string, TaskRun[]>();
  for (const run of snap.runs) {
    const paperId = run.experimentId ? expToPaper.get(run.experimentId) : undefined;
    if (!paperId) continue;
    byPaper.set(paperId, [...(byPaper.get(paperId) ?? []), run]);
  }
  return snap.papers.map(p => {
    const runs = (byPaper.get(p.id) ?? []).sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));
    return { paperId: p.id, title: p.title ?? '未命名论文', paperStatus: p.status, createdAt: p.createdAt, run: runs[0] ?? null, runCount: runs.length };
  }).sort((a, b) => (b.run?.createdAt ?? b.createdAt ?? '').localeCompare(a.run?.createdAt ?? a.createdAt ?? ''));
}

function taskBucket(t: Task): Filter {
  if (!t.run) return 'all';
  if (ACTIVE.has(t.run.status)) return 'active';
  if (t.run.status === 'completed') return 'done';
  if (t.run.status === 'failed') return 'failed';
  return 'all';
}

export default function Dashboard({ api, onNew }: { api: GuidedApi; onNew: () => void }) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [issues, setIssues] = useState<LoadIssue[]>([]);
  const [fatal, setFatal] = useState<ReturnType<typeof describeError> | null>(null);
  const [rev, setRev] = useState(0);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [layout, setLayout] = useState<Layout>('grid');
  const searchRef = useRef<HTMLInputElement>(null);
  const refresh = useCallback(() => setRev(x => x + 1), []);

  useEffect(() => {
    const ctl = new AbortController();
    setPhase(p => (p === 'ready' ? p : 'loading'));
    loadSnapshot(api, ctl.signal).then(result => {
      if (ctl.signal.aborted) return;
      setSnap(result.snapshot); setIssues(result.issues); setFatal(null); setPhase('ready');
    }).catch(error => {
      if (ctl.signal.aborted) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setFatal(describeError(error)); setPhase('error');
    });
    return () => ctl.abort();
  }, [api, rev]);

  // Auto-retry while the backend is unreachable, and keep active runs fresh.
  const hasActive = !!snap?.runs.some(r => ACTIVE.has(r.status));
  useEffect(() => {
    const delay = phase === 'error' && fatal?.kind !== 'auth' ? AUTO_RETRY_MS : hasActive ? 5000 : 0;
    if (!delay) return;
    const id = window.setInterval(refresh, delay);
    return () => window.clearInterval(id);
  }, [phase, fatal, hasActive, refresh]);

  // "/" focuses search, like the reference dashboards.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (e.key === '/' && !(el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) { e.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const tasks = useMemo(() => (snap ? buildTasks(snap) : []), [snap]);
  const counts = useMemo(() => {
    const runs = snap?.runs ?? [];
    const done = runs.filter(r => r.status === 'completed').length;
    const failed = runs.filter(r => r.status === 'failed').length;
    const active = runs.filter(r => ACTIVE.has(r.status)).length;
    return { papers: snap?.papers.length ?? 0, runs: runs.length, done, failed, active, rate: runs.length ? Math.round((done / runs.length) * 100) : 0 };
  }, [snap]);
  const visible = tasks.filter(t => {
    if (filter !== 'all' && taskBucket(t) !== filter) return false;
    return !query.trim() || t.title.toLowerCase().includes(query.trim().toLowerCase());
  });
  const failedRuns = (snap?.runs ?? []).filter(r => r.status === 'failed').slice(0, 3);
  const loading = phase === 'loading' && !snap;

  return <div className="dash">
    <div className="dash-bar">
      <input ref={searchRef} className="dash-search" type="search" placeholder="搜索论文标题" aria-label="搜索论文" value={query} onChange={e => setQuery(e.target.value)} />
      <div className="seg" role="group" aria-label="状态筛选">
        {([['all', '全部'], ['active', '进行中'], ['done', '已完成'], ['failed', '失败']] as const).map(([key, label]) =>
          <button key={key} className={filter === key ? 'on' : ''} aria-pressed={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      <div className="seg icons" role="group" aria-label="布局">
        <button className={layout === 'grid' ? 'on' : ''} aria-label="网格视图" aria-pressed={layout === 'grid'} onClick={() => setLayout('grid')}><Icon name="grid" /></button>
        <button className={layout === 'list' ? 'on' : ''} aria-label="列表视图" aria-pressed={layout === 'list'} onClick={() => setLayout('list')}><Icon name="list" /></button>
      </div>
      <button className="btn-icon" aria-label="刷新" title="刷新" onClick={refresh} disabled={phase === 'loading' && !!snap}><Icon name="refresh" spin={phase === 'loading'} /></button>
      <button className="btn-black" onClick={onNew}><Icon name="plus" />新建任务</button>
    </div>

    {phase === 'error' && fatal && <section className="dash-error" role="alert">
      <div className="dash-error-head"><Icon name="alert" /><b>{fatal.title}</b></div>
      <p>{fatal.hint}</p>
      {fatal.steps.length > 0 && <ol>{fatal.steps.map(s => <li key={s}>{s}</li>)}</ol>}
      <code>{fatal.detail}</code>
      <div className="dash-error-actions">
        <button className="btn-black sm" onClick={refresh}>立即重试</button>
        {fatal.kind !== 'auth' && <span className="dash-muted">每 {AUTO_RETRY_MS / 1000} 秒自动重试</span>}
      </div>
    </section>}

    {phase === 'ready' && issues.length > 0 && <div className="dash-warn" role="status">
      <Icon name="alert" /> 部分数据未能读取（{issues.length} 项），已显示其余内容。
      <details><summary>详情</summary><ul>{issues.map((i, n) => <li key={n}><code>{i.what}</code> — {i.reason}</li>)}</ul></details>
    </div>}

    {phase !== 'error' && <div className="dash-cols">
      <aside className="dash-side">
        <h2>概况</h2>
        <div className="card usage">
          {loading ? [0, 1, 2, 3].map(i => <div key={i} className="usage-row"><span className="skel w40" /><span className="skel w16" /></div>) : <>
            <div className="usage-row"><span>论文</span><b>{counts.papers}</b></div>
            <div className="usage-row"><span>运行</span><b>{counts.runs}</b></div>
            <div className="usage-row"><span>进行中</span><b className={counts.active ? 'blue' : ''}>{counts.active}</b></div>
            <div className="usage-row"><span>失败</span><b className={counts.failed ? 'red' : ''}>{counts.failed}</b></div>
            <div className="usage-meter" aria-label={`运行完成率 ${counts.rate}%`}>
              <div className="usage-meter-top"><span>运行完成率</span><b>{counts.runs ? `${counts.rate}%` : '—'}</b></div>
              <div className="bar"><i style={{ width: `${counts.rate}%` }} /></div>
            </div>
          </>}
        </div>
        <h2>提醒</h2>
        <div className="card alerts">
          {failedRuns.length === 0
            ? <div className="empty-mini"><span className="ring"><Icon name="bell" /></span><p>失败的运行会显示在这里。</p></div>
            : <ul>{failedRuns.map(r => <li key={r.id}><span className="dot bad" /><div><b>{r.id.slice(0, 12)}</b><small>{r.errorCode ?? '运行失败'} · {when(r.createdAt)}</small></div></li>)}</ul>}
        </div>
      </aside>

      <section className="dash-main" aria-busy={loading}>
        <h2>任务 <small>{visible.length}</small></h2>
        {loading && <div className="grid">{[0, 1, 2, 3].map(i => <div key={i} className="card task skel-card"><span className="skel w60" /><span className="skel w40" /><span className="skel w30" /></div>)}</div>}
        {phase === 'ready' && tasks.length === 0 && <div className="card empty">
          <span className="ring big"><Icon name="doc" /></span>
          <h3>还没有复现任务</h3>
          <p>上传一篇论文，按引导完成抽取、审核、环境检查和执行，就能得到一份可核验的复现报告。</p>
          <button className="btn-black" onClick={onNew}><Icon name="plus" />开始第一个任务</button>
        </div>}
        {phase === 'ready' && tasks.length > 0 && visible.length === 0 && <div className="card empty"><h3>没有匹配的任务</h3><p>换个关键词或筛选条件试试。</p>
          <button className="btn-ghost" onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</button></div>}
        {visible.length > 0 && <div className={layout === 'grid' ? 'grid' : 'rows'}>{visible.map(t => <TaskCard key={t.paperId} task={t} />)}</div>}
      </section>
    </div>}
  </div>;
}

function TaskCard({ task }: { task: Task }) {
  const status = task.run ? RUN_LABEL[task.run.status] ?? { text: task.run.status, tone: 'idle' as const } : PAPER_LABEL[task.paperStatus] ?? { text: task.paperStatus, tone: 'idle' as const };
  const open = () => { window.location.hash = `#/task/${encodeURIComponent(task.paperId)}`; };
  return <article className="card task clickable" onClick={open}>
    <header>
      <span className="task-icon"><Icon name="doc" /></span>
      <div className="task-title"><b title={task.title}><a href={`#/task/${encodeURIComponent(task.paperId)}`} onClick={e => e.stopPropagation()}>{task.title}</a></b><small>{task.paperId.replace(/^pap_/, '').slice(0, 8)}</small></div>
      <span className={`pill ${status.tone}`}>{status.text}</span>
    </header>
    <p className="task-line">
      {task.run
        ? <><Icon name="check" /> {MODE_LABEL[task.run.mode ?? ''] ?? task.run.mode ?? '运行'}{task.run.verdict ? ` · ${VERDICT_LABEL[task.run.verdict] ?? task.run.verdict}` : ''}</>
        : <>尚未创建运行</>}
    </p>
    <footer><span>{task.runCount > 0 ? `${task.runCount} 次运行` : '未运行'}</span><span>{when(task.run?.createdAt ?? task.createdAt)}</span></footer>
  </article>;
}
