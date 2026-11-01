import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { Badge, CardHeader, SectionHead, StatusWord, type Tone } from '../../ui/Card';
import { Segmented } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { describeError, loadSnapshot, type LoadIssue, type Snapshot } from './data';
import { ACTIVE_RUN, buildTasks, MODE_LABEL, taskBucket, taskStatus, when, type Task, type TaskFilter } from './tasks';

// Read-only control panel. GET requests only: creating anything happens in the
// guided new-task flow. Loading is fault tolerant: a failing project or
// resource degrades to a warning instead of blanking the whole page.
//
//   <Dashboard api={api} onNew={() => …} refreshSignal={tick} onBusy={setBusy} />

type Phase = 'loading' | 'ready' | 'error';
type Layout = 'grid' | 'list';
const AUTO_RETRY_MS = 8000;
const FILTERS = [{ value: 'all', label: '全部' }, { value: 'active', label: '进行中' }, { value: 'done', label: '已完成' }, { value: 'failed', label: '失败' }] as const;
const LAYOUTS = [{ value: 'list', icon: 'list', ariaLabel: '列表视图' }, { value: 'grid', icon: 'grid', ariaLabel: '网格视图' }] as const;

export default function Dashboard({ api, onNew, refreshSignal = 0, onBusy }: { api: GuidedApi; onNew: () => void; refreshSignal?: number; onBusy?: (busy: boolean) => void }) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [fetching, setFetching] = useState(true);
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [issues, setIssues] = useState<LoadIssue[]>([]);
  const [fatal, setFatal] = useState<ReturnType<typeof describeError> | null>(null);
  const [rev, setRev] = useState(0);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<TaskFilter>('all');
  const [layout, setLayout] = useState<Layout>('list');
  const [alertsOpen, setAlertsOpen] = useState<boolean | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const refresh = useCallback(() => setRev(x => x + 1), []);

  useEffect(() => {
    const ctl = new AbortController();
    setPhase(p => (p === 'ready' ? p : 'loading'));
    setFetching(true);
    loadSnapshot(api, ctl.signal).then(result => {
      if (ctl.signal.aborted) return;
      setSnap(result.snapshot); setIssues(result.issues); setFatal(null); setPhase('ready'); setFetching(false);
    }).catch(error => {
      if (ctl.signal.aborted) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setFatal(describeError(error)); setPhase('error'); setFetching(false);
    });
    return () => ctl.abort();
  }, [api, rev, refreshSignal]);

  useEffect(() => { onBusy?.(fetching); }, [fetching, onBusy]);
  useEffect(() => () => onBusy?.(false), [onBusy]);

  // Auto-retry while the backend is unreachable, and keep active runs fresh.
  const hasActive = !!snap?.runs.some(r => ACTIVE_RUN.has(r.status));
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
      if (e.key === '/' && !e.metaKey && !e.ctrlKey && !(el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable))) { e.preventDefault(); searchRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const tasks = useMemo(() => (snap ? buildTasks(snap) : []), [snap]);
  const counts = useMemo(() => {
    const runs = snap?.runs ?? [];
    const done = runs.filter(r => r.status === 'completed').length;
    const failed = runs.filter(r => r.status === 'failed').length;
    const active = runs.filter(r => ACTIVE_RUN.has(r.status)).length;
    return { papers: snap?.papers.length ?? 0, runs: runs.length, done, failed, active, rate: runs.length ? Math.round((done / runs.length) * 100) : 0 };
  }, [snap]);
  const visible = tasks.filter(t => {
    if (filter !== 'all' && taskBucket(t) !== filter) return false;
    return !query.trim() || t.title.toLowerCase().includes(query.trim().toLowerCase());
  });
  const expToPaper = useMemo(() => new Map((snap?.experiments ?? []).map(e => [e.id, e.paperId])), [snap]);
  const failedRuns = (snap?.runs ?? []).filter(r => r.status === 'failed').sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? '')).slice(0, 5);
  const loading = phase === 'loading' && !snap;
  const summary: { text: string; tone: Tone } = phase === 'error'
    ? { text: fatal?.kind === 'offline' ? '未连接' : '读取失败', tone: 'bad' }
    : loading ? { text: '读取中', tone: 'idle' }
    : counts.active > 0 ? { text: `${counts.active} 个运行进行中`, tone: 'run' } : { text: '就绪', tone: 'ok' };

  return <div className="dash">
    <section className="ui-card dash-summary" aria-busy={loading}>
      <CardHeader as="h1" icon="layers" title="工作区" status={summary}
        subtitle={loading ? '正在读取论文与运行…' : phase === 'error' ? '暂时无法读取论文与运行，恢复后自动显示。' : `${counts.papers} 篇论文 · ${counts.runs} 次运行 · 完成率 ${counts.runs ? `${counts.rate}%` : '—'}`}
        actions={<button type="button" className="btn btn-primary" onClick={onNew}><Icon name="plus" size={15} />新建任务</button>} />
      {phase !== 'error' && <div className="dash-stats">
        <Stat label="进行中" value={counts.active} tone={counts.active ? 'run' : undefined} loading={loading} />
        <Stat label="已完成" value={counts.done} loading={loading} />
        <Stat label="失败" value={counts.failed} tone={counts.failed ? 'bad' : undefined} loading={loading} />
        <div className="dash-stat dash-meter" aria-label={`运行完成率 ${counts.rate}%`}>
          <span>运行完成率</span>
          <b>{loading ? <span className="ui-skel w40" /> : counts.runs ? `${counts.rate}%` : '—'}</b>
          <div className="dash-bar" aria-hidden="true"><i style={{ width: `${counts.rate}%` }} /></div>
        </div>
      </div>}
    </section>

    {phase === 'error' && fatal && <section className="ui-card dash-error" role="alert">
      <CardHeader icon="alert" title={fatal.title} status={{ text: fatal.kind === 'auth' ? '需要处理' : '自动重试中', tone: 'bad' }} subtitle={fatal.hint}
        actions={<button type="button" className="btn btn-secondary" onClick={refresh}>立即重试</button>} />
      <div className="ui-card-section">
        {fatal.steps.length > 0 && <ol className="dash-steps">{fatal.steps.map(s => <li key={s}>{s}</li>)}</ol>}
        {fatal.kind !== 'auth' && <p className="ui-help">每 {AUTO_RETRY_MS / 1000} 秒自动重试</p>}
        <Disclosure variant="sub" title="错误详情"><code className="ui-mono">{fatal.detail}</code></Disclosure>
      </div>
    </section>}

    {phase === 'ready' && issues.length > 0 && <div className="ui-callout tone-warn dash-warn" role="status">
      <Icon name="alert" />
      <div>
        部分数据未能读取（{issues.length} 项），已显示其余内容。
        <Disclosure variant="sub" title="详情" count={issues.length}><ul className="dash-issues">{issues.map((i, n) => <li key={n}><code className="ui-mono">{i.what}</code> — {i.reason}</li>)}</ul></Disclosure>
      </div>
    </div>}

    {phase !== 'error' && <section className="dash-tasks" aria-labelledby="dash-tasks-h" aria-busy={loading}>
      <SectionHead id="dash-tasks-h" title="任务" count={loading ? undefined : visible.length} action={<>
        <label className="dash-search">
          <Icon name="search" size={14} />
          <input ref={searchRef} type="search" placeholder="搜索论文标题" aria-label="搜索论文" value={query} onChange={e => setQuery(e.target.value)} />
          {!query && <kbd aria-hidden="true">/</kbd>}
        </label>
        <Segmented label="状态筛选" value={filter} options={FILTERS} onChange={setFilter} />
        <Segmented label="布局" value={layout} options={LAYOUTS} onChange={setLayout} />
      </>} />
      <p className="ui-help">每篇论文是一个任务；点击查看最近一次运行、指标与证据。</p>
      {loading && <ul className="ui-list" aria-hidden="true">{[0, 1, 2].map(i => <li key={i} className="ui-row"><span className="ui-skel w40" /><span className="ui-skel w20" /></li>)}</ul>}
      {phase === 'ready' && tasks.length === 0 && <div className="ui-card ui-empty">
        <span className="ui-tile"><Icon name="doc" size={18} /></span>
        <h3>还没有复现任务</h3>
        <p>上传一篇论文，按引导完成抽取、审核、环境检查和执行，就能得到一份可核验的复现报告。</p>
        <button type="button" className="btn btn-primary" onClick={onNew}><Icon name="plus" size={15} />开始第一个任务</button>
      </div>}
      {phase === 'ready' && tasks.length > 0 && visible.length === 0 && <div className="ui-card ui-empty">
        <h3>没有匹配的任务</h3><p>换个关键词或筛选条件试试。</p>
        <button type="button" className="btn btn-secondary" onClick={() => { setQuery(''); setFilter('all'); }}>清除筛选</button>
      </div>}
      {visible.length > 0 && (layout === 'list'
        ? <ul className="ui-list dash-list">{visible.map(t => <li key={t.paperId}><TaskRow task={t} /></li>)}</ul>
        : <ul className="dash-grid">{visible.map(t => <li key={t.paperId}><TaskCard task={t} /></li>)}</ul>)}
    </section>}

    {phase === 'ready' && <Disclosure title="提醒" count={counts.failed} open={alertsOpen ?? counts.failed > 0} onToggle={setAlertsOpen}
      meta={counts.failed > failedRuns.length ? <span>显示最近 {failedRuns.length} 条</span> : undefined}>
      {failedRuns.length === 0
        ? <p className="ui-help dash-alert-empty"><Icon name="bell" size={14} />失败的运行会显示在这里。</p>
        : <ul className="ui-list">{failedRuns.map(r => {
          const paperId = r.experimentId ? expToPaper.get(r.experimentId) : null;
          const body = <>
            <Icon name="alert" className="dash-alert-ico" />
            <span className="ui-row-main"><span className="ui-row-title ui-mono">{r.id.slice(0, 16)}</span><span className="ui-row-sub">{r.errorCode ?? '运行失败'}</span></span>
            <span className="ui-row-end"><time>{when(r.createdAt)}</time>{paperId && <Icon name="chevron-right" size={14} />}</span>
          </>;
          return <li key={r.id}>{paperId ? <a className="ui-row" href={`#/task/${encodeURIComponent(paperId)}`}>{body}</a> : <div className="ui-row">{body}</div>}</li>;
        })}</ul>}
    </Disclosure>}
  </div>;
}

function Stat({ label, value, tone, loading }: { label: string; value: number; tone?: Tone; loading: boolean }) {
  return <div className="dash-stat"><span>{label}</span><b className={tone ? `tone-${tone}` : undefined}>{loading ? <span className="ui-skel w40" /> : value}</b></div>;
}

function taskLine(task: Task): string {
  if (!task.run) return '尚未创建运行';
  return MODE_LABEL[task.run.mode ?? ''] ?? task.run.mode ?? '运行';
}

function TaskRow({ task }: { task: Task }) {
  const status = taskStatus(task);
  return <a className="ui-row task-row" href={`#/task/${encodeURIComponent(task.paperId)}`}>
    <span className="task-ico" aria-hidden="true"><Icon name="doc" /></span>
    <span className="ui-row-main"><span className="ui-row-title" title={task.title}>{task.title}</span><span className="ui-row-sub">{taskLine(task)}</span></span>
    <span className="ui-row-end">
      <code className="ui-pill task-id">{task.paperId.replace(/^pap_/, '').slice(0, 8)}</code>
      {task.runCount > 0 ? <Badge tone="accent">{task.runCount} 次运行</Badge> : <Badge>未运行</Badge>}
      <StatusWord tone={status.tone}>{status.text}</StatusWord>
      <time className="task-time">{when(task.run?.createdAt ?? task.createdAt)}</time>
      <Icon name="chevron-right" size={14} />
    </span>
  </a>;
}

function TaskCard({ task }: { task: Task }) {
  const status = taskStatus(task);
  return <a className="ui-card task-card" href={`#/task/${encodeURIComponent(task.paperId)}`}>
    <span className="task-card-head">
      <span className="task-ico" aria-hidden="true"><Icon name="doc" /></span>
      <span className="ui-row-main"><span className="ui-row-title" title={task.title}>{task.title}</span><span className="ui-row-sub">{taskLine(task)}</span></span>
    </span>
    <span className="task-card-foot">
      <StatusWord tone={status.tone}>{status.text}</StatusWord>
      {task.runCount > 0 ? <Badge tone="accent">{task.runCount} 次运行</Badge> : <Badge>未运行</Badge>}
      <time className="task-time">{when(task.run?.createdAt ?? task.createdAt)}</time>
    </span>
  </a>;
}
