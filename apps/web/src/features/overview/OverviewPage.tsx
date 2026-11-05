import { useCallback, useEffect, useState } from 'react';
import { createAutonomousApi, type AutonomousApi } from '../../api-client/autonomous';
import { createPapersApi, type PapersApi } from '../../api-client/papers';
import { Badge, Card, CardHeader, SectionHead, StatusWord } from '../../ui/Card';
import { Segmented } from '../../ui/Controls';
import { Icon } from '../../ui/Icon';
import { taskBucket, taskDisplay } from '../autonomous/config';
import { loadAutonomousTasks, type RecentTask } from '../../shell/hooks';
import { taskHref } from '../../shell/Sidebar';

const defaultApi = createAutonomousApi();
const defaultPapers = createPapersApi('demo-user');

type Phase = 'loading' | 'ready' | 'error';
type Filter = 'all' | 'active' | 'done' | 'failed';
const FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '进行中' },
  { value: 'done', label: '已结束' },
  { value: 'failed', label: '失败' },
] as const;

const when = (iso: string) => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};

// 有结局的任务一律算已结束（安全终止归入失败），即使生命周期状态是 paused / stopped。
const bucket = (task: RecentTask): Filter => taskBucket(task);

// 总览（#/）：全部项目的自主任务列表。只读；新建任务在「新建实验」里完成。
export default function OverviewPage({ api = defaultApi, papers = defaultPapers }: { api?: AutonomousApi; papers?: PapersApi }) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [tasks, setTasks] = useState<RecentTask[]>([]);
  const [failedProjects, setFailedProjects] = useState(0);
  const [rev, setRev] = useState(0);
  const [filter, setFilter] = useState<Filter>('all');
  const refresh = useCallback(() => setRev(x => x + 1), []);

  useEffect(() => {
    const ctl = new AbortController();
    loadAutonomousTasks(api, papers, ctl.signal).then(result => {
      if (ctl.signal.aborted) return;
      setTasks(result.tasks);
      setFailedProjects(result.failedProjects);
      setPhase('ready');
    }).catch(() => {
      if (!ctl.signal.aborted) setPhase('error');
    });
    return () => ctl.abort();
  }, [api, papers, rev]);

  // 有进行中的任务时每 10 秒自动刷新。
  const hasActive = tasks.some(t => bucket(t) === 'active');
  useEffect(() => {
    if (!hasActive) return;
    const id = window.setInterval(refresh, 10_000);
    return () => window.clearInterval(id);
  }, [hasActive, refresh]);

  const visible = tasks.filter(t => filter === 'all' || bucket(t) === filter);
  const counts = {
    active: tasks.filter(t => bucket(t) === 'active').length,
    done: tasks.filter(t => bucket(t) === 'done').length,
    failed: tasks.filter(t => bucket(t) === 'failed').length,
  };
  const loading = phase === 'loading';

  return <div className="overview">
    <Card aria-busy={loading}>
      <CardHeader as="h1" icon="layers" title="工作区"
        status={loading ? { text: '读取中', tone: 'idle' } : phase === 'error' ? { text: '读取失败', tone: 'bad' } : counts.active > 0 ? { text: `${counts.active} 个任务进行中`, tone: 'run' } : { text: '就绪', tone: 'ok' }}
        subtitle={loading ? '正在读取任务…' : phase === 'error' ? '暂时无法读取任务。' : `${tasks.length} 个自主实验任务 · ${counts.done} 个已结束 · ${counts.failed} 个失败`}
        actions={<a className="btn btn-primary" href="#/new"><Icon name="plus" size={15} />新建实验</a>} />
    </Card>

    {phase === 'error' && <Card>
      <CardHeader icon="alert" title="无法读取任务" status={{ text: '读取失败', tone: 'bad' }}
        subtitle="通常是后端未启动；确认后端运行后重试。"
        actions={<button type="button" className="btn btn-secondary" onClick={refresh}>立即重试</button>} />
    </Card>}

    {phase === 'ready' && failedProjects > 0 && <div className="ui-callout tone-warn" role="status">
      <Icon name="alert" /><span>{failedProjects} 个项目的任务未能读取，已显示其余内容。</span>
    </div>}

    {phase !== 'error' && <section aria-labelledby="overview-tasks-h" aria-busy={loading}>
      <SectionHead id="overview-tasks-h" title="任务" count={loading ? undefined : visible.length}
        action={<Segmented label="状态筛选" value={filter} options={FILTERS} onChange={setFilter} />} />
      {loading && <ul className="ui-list" aria-hidden="true">{[0, 1, 2].map(i => <li key={i} className="ui-row"><span className="ui-skel w40" /><span className="ui-skel w20" /></li>)}</ul>}
      {phase === 'ready' && tasks.length === 0 && <div className="ui-card ui-empty">
        <span className="ui-tile"><Icon name="flask" size={18} /></span>
        <h3>还没有自主实验任务</h3>
        <p>上传一篇论文，配置模型与预算后授权启动；AI 会规划、执行并冻结证据，最后给出程序判定的报告。</p>
        <a className="btn btn-primary" href="#/new"><Icon name="plus" size={15} />开始第一个实验</a>
      </div>}
      {phase === 'ready' && tasks.length > 0 && visible.length === 0 && <div className="ui-card ui-empty">
        <h3>没有匹配的任务</h3><p>换个筛选条件试试。</p>
        <button type="button" className="btn btn-secondary" onClick={() => setFilter('all')}>显示全部</button>
      </div>}
      {visible.length > 0 && <ul className="ui-list">
        {visible.map(t => {
          const shown = taskDisplay(t);
          return <li key={t.id}>
          <a className="ui-row" href={taskHref(t.id)}>
            <span className="ui-tile" aria-hidden="true"><Icon name="flask" /></span>
            <span className="ui-row-main">
              <span className="ui-row-title" title={t.objective}>{t.objective || '（未命名实验）'}</span>
              <span className="ui-row-sub ui-mono">{t.id}</span>
            </span>
            <span className="ui-row-end">
              <StatusWord tone={shown.tone}>{shown.text}</StatusWord>
              {shown.lifecycle && <span>{shown.lifecycle}</span>}
              {shown.pausedNotice && <Badge tone="warn">后台作业可能仍在运行</Badge>}
              <time>{when(t.updatedAt)}</time>
              <Icon name="chevron-right" size={14} />
            </span>
          </a>
        </li>;
        })}
      </ul>}
    </section>}
  </div>;
}
