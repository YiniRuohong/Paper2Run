import { useCallback, useEffect, useMemo, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import { validatePaperInfo, validateRun } from '../../api-client/guided';
import type { GuidedPaperInfo, GuidedRun, GuidedRunStatus } from '../../api-client/guided-types';

// Read-only control panel: aggregates papers and runs across the actor's
// projects. It performs GET requests only; creating anything happens in the
// guided new-task flow.

interface ProjectLite { id: string; name: string }

const projectValidator = (value: unknown): ProjectLite => {
  const item = (value ?? {}) as Record<string, unknown>;
  if (typeof item.id !== 'string') throw new Error('invalid project');
  return { id: item.id, name: typeof item.name === 'string' ? item.name : item.id };
};

type Phase = 'loading' | 'ready' | 'error';

interface Snapshot {
  projects: ProjectLite[];
  papers: GuidedPaperInfo[];
  runs: GuidedRun[];
}

const RUN_LABEL: Record<string, { text: string; tone: 'ok' | 'run' | 'bad' | 'idle' }> = {
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
const ACTIVE: GuidedRunStatus[] = ['queued', 'running', 'evaluating', 'cancel_requested'] as GuidedRunStatus[];

const PAPER_STATUS: Record<string, string> = { uploaded: '已上传', processing: '处理中', ready: '就绪', failed: '失败' };

function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function Dashboard({ api, onNew }: { api: GuidedApi; onNew: () => void }) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [data, setData] = useState<Snapshot | null>(null);
  const [rev, setRev] = useState(0);

  const refresh = useCallback(() => setRev(x => x + 1), []);

  useEffect(() => {
    const ctl = new AbortController();
    setPhase(p => (p === 'ready' ? p : 'loading'));
    (async () => {
      const projects = (await api.readArray('/projects', projectValidator, ctl.signal)).items;
      const papers: GuidedPaperInfo[] = [];
      const runs: GuidedRun[] = [];
      for (const project of projects) {
        const pid = encodeURIComponent(project.id);
        const [p, r] = await Promise.all([
          api.readArray(`/projects/${pid}/papers`, validatePaperInfo, ctl.signal),
          api.readArray(`/projects/${pid}/runs`, validateRun, ctl.signal),
        ]);
        papers.push(...p.items);
        runs.push(...r.items);
      }
      if (!ctl.signal.aborted) { setData({ projects, papers, runs }); setPhase('ready'); }
    })().catch(error => {
      if (ctl.signal.aborted) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      setPhase('error');
    });
    return () => ctl.abort();
  }, [api, rev]);

  // Keep active runs fresh without hammering the API.
  const hasActive = !!data?.runs.some(r => ACTIVE.includes(r.status));
  useEffect(() => {
    if (!hasActive) return;
    const id = window.setInterval(refresh, 5000);
    return () => window.clearInterval(id);
  }, [hasActive, refresh]);

  const stats = useMemo(() => {
    const runs = data?.runs ?? [];
    return {
      papers: data?.papers.length ?? 0,
      runs: runs.length,
      done: runs.filter(r => r.status === 'completed').length,
      active: runs.filter(r => ACTIVE.includes(r.status)).length,
      failed: runs.filter(r => r.status === 'failed').length,
    };
  }, [data]);

  const paperTitle = useMemo(() => {
    const map = new Map<string, string>();
    for (const p of data?.papers ?? []) map.set(p.paper_id, p.title ?? '未命名论文');
    return map;
  }, [data]);
  const recentRuns = (data?.runs ?? []).slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 8);
  const recentPapers = (data?.papers ?? []).slice().sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 6);

  return <div className="dash">
    <header className="dash-head">
      <div>
        <h1>总览</h1>
        <p>所有复现任务的运行状态与最近活动。</p>
      </div>
      <div className="dash-actions">
        <button className="dash-ghost" onClick={refresh} disabled={phase === 'loading'}>刷新</button>
        <button className="dash-cta" onClick={onNew}>新建复现任务</button>
      </div>
    </header>

    {phase === 'error' && <div className="dash-alert" role="alert">
      <b>无法读取任务数据。</b> 请确认后端服务已启动，然后重试。
      <button className="dash-ghost" onClick={refresh}>重试</button>
    </div>}

    <section className="dash-stats" aria-label="统计概览" aria-busy={phase === 'loading'}>
      {([
        ['论文', stats.papers, ''],
        ['运行', stats.runs, ''],
        ['已完成', stats.done, 'ok'],
        ['进行中', stats.active, 'run'],
        ['失败', stats.failed, stats.failed > 0 ? 'bad' : ''],
      ] as const).map(([label, value, tone]) => <div key={label} className={`dash-stat ${tone}`}>
        <span className="dash-stat-label">{label}</span>
        {phase === 'loading' && !data ? <span className="dash-skel" aria-hidden="true" /> : <span className="dash-stat-value">{value}</span>}
      </div>)}
    </section>

    {phase === 'ready' && data && data.papers.length === 0 && data.runs.length === 0 && <section className="dash-empty">
      <h2>还没有复现任务</h2>
      <p>上传一篇论文，按引导完成抽取、审核、环境检查和执行，几分钟内得到一份可核验的复现报告。</p>
      <button className="dash-cta" onClick={onNew}>开始第一个任务</button>
    </section>}

    {data && (data.papers.length > 0 || data.runs.length > 0) && <div className="dash-grid">
      <section className="dash-panel">
        <h2>最近运行</h2>
        {recentRuns.length === 0 ? <p className="dash-muted">论文已上传，但还没有创建运行。继续完成引导流程即可。</p> : <table className="dash-table">
          <thead><tr><th>运行</th><th>状态</th><th>创建时间</th></tr></thead>
          <tbody>{recentRuns.map(run => {
            const label = RUN_LABEL[run.status] ?? { text: run.status, tone: 'idle' as const };
            return <tr key={run.run_id}>
              <td><code>{run.run_id.slice(0, 10)}</code></td>
              <td><span className={`dash-pill ${label.tone}`}>{label.text}</span></td>
              <td className="dash-num">{when(run.created_at)}</td>
            </tr>;
          })}</tbody>
        </table>}
      </section>
      <section className="dash-panel">
        <h2>最近论文</h2>
        <ul className="dash-list">{recentPapers.map(paper => <li key={paper.paper_id}>
          <span className="dash-list-title">{paperTitle.get(paper.paper_id)}</span>
          <span className="dash-list-meta">{PAPER_STATUS[paper.status] ?? paper.status} · {when(paper.created_at)}</span>
        </li>)}</ul>
      </section>
    </div>}
  </div>;
}
