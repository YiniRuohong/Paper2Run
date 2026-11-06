import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { createAutonomousApi } from '../api-client/autonomous';
import { createPapersApi } from '../api-client/papers';
import { backendHost, getDesktop } from '../desktop';
import { useBackendHealth, useRecentTasks } from './hooks';
import { Sidebar, type View } from './Sidebar';
import { Icon } from '../ui/Icon';
import '../ui/ui.css';
import './shell.css';

// App shell (magpie-style sidebar + quiet header). Hash routes:
//   #/            总览（全部项目的自主任务，只读）
//   #/new         新建实验（论文上传解析 + 分层配置 + 授权启动，同一流程）
//   #/task/<id>   任务工作台（阶段时间线 + 实时页签 + 报告 + 用量）
//   #/reader[/<paperId>]  论文阅读器（PDF 预览 + 批注，书架 = 已上传的 PDF 论文）
//   #/settings    设置（主题、本地服务、解析模式）
// 旧链接 #/autonomous 重定向：带 ?task=<id> 去任务页，否则去新建实验。
const api = createAutonomousApi();
const papers = createPapersApi('demo-user');
const OverviewPage = lazy(() => import('../features/overview/OverviewPage'));
const NewExperimentPage = lazy(() => import('../features/autonomous/NewExperimentPage'));
const TaskPage = lazy(() => import('../features/autonomous/TaskPage'));
const Settings = lazy(() => import('../features/settings/Settings'));
const ReaderPage = lazy(() => import('../features/reader/ReaderPage'));

interface Route { view: View | 'legacy'; taskId: string | null; project: string; paper: string }

const queryOf = (hash: string) => new URLSearchParams(hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : '');
const routeFromHash = (): Route => {
  const h = window.location.hash;
  const none = { taskId: null, project: '', paper: '' };
  if (h.startsWith('#/autonomous')) return { ...none, view: 'legacy', taskId: queryOf(h).get('task') };
  if (h.startsWith('#/new')) return { ...none, view: 'new', project: queryOf(h).get('project') ?? '', paper: queryOf(h).get('paper') ?? '' };
  if (h.startsWith('#/task/')) return { ...none, view: 'task', taskId: decodeURIComponent(h.slice('#/task/'.length).split('?')[0]) };
  if (h.startsWith('#/reader')) {
    const rest = h.slice('#/reader'.length).split('?')[0].replace(/^\//, '');
    let paper = '';
    try { paper = decodeURIComponent(rest); } catch { paper = ''; }
    return { ...none, view: 'reader', paper };
  }
  if (h.startsWith('#/settings')) return { ...none, view: 'settings' };
  return { ...none, view: 'overview' };
};
const narrow = () => window.matchMedia?.('(max-width: 760px)').matches ?? false;

export default function AppShell() {
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [sideOpen, setSideOpen] = useState(() => !narrow());
  const [refreshTick, setRefreshTick] = useState(0);
  const [health, recheckHealth] = useBackendHealth();
  const recent = useRecentTasks(api, papers, `${route.view}:${route.taskId ?? ''}:${refreshTick}`);
  const host = backendHost(getDesktop()?.backendUrl, window.location.host);
  useEffect(() => {
    const onHash = () => { setRoute(routeFromHash()); if (narrow()) setSideOpen(false); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const refresh = useCallback(() => { setRefreshTick(x => x + 1); recheckHealth(); }, [recheckHealth]);

  // 旧入口 #/autonomous 已删除：重定向到新路由，并替换历史记录。
  useEffect(() => {
    if (route.view === 'legacy') {
      window.location.replace(route.taskId ? `#/task/${encodeURIComponent(route.taskId)}` : '#/new');
    }
  }, [route]);

  // 新建实验页持有内存中的 API Key 与解析进度；任务页持有重注入的密钥。
  // 打开过后保持挂载（hidden），切走再切回不丢状态。
  const [newOpened, setNewOpened] = useState(false);
  const [openedTasks, setOpenedTasks] = useState<string[]>([]);
  useEffect(() => {
    if (route.view === 'new') setNewOpened(true);
    if (route.view === 'task' && route.taskId) {
      const id = route.taskId;
      setOpenedTasks(old => (old.includes(id) ? old : [...old, id]));
    }
  }, [route]);

  const view: View = route.view === 'legacy' ? 'overview' : route.view;
  const taskTitle = view === 'task'
    ? recent.tasks?.find(t => t.id === route.taskId)?.objective ?? '任务详情'
    : '';
  const crumbs: [string, string] = view === 'overview' ? ['工作区', '总览']
    : view === 'new' ? ['工作区', '新建实验']
    : view === 'task' ? ['任务', taskTitle]
    : view === 'reader' ? ['工作区', '论文阅读']
    : ['Paper2Run', '设置'];
  // 工作台视图（多栏）用更宽的版面。
  const wide = view === 'new' || view === 'task';
  // 阅读器铺满主区域：不走居中版面和页内滚动，PDF 自己滚。
  const full = view === 'reader';

  return <div className={`shell${sideOpen ? '' : ' side-closed'}${wide ? ' wide' : ''}${full ? ' full' : ''}`}>
    {sideOpen && <Sidebar view={view} taskId={route.taskId} recent={recent.tasks} recentFailed={recent.failed} health={health} host={host} onHide={() => setSideOpen(false)} />}
    {sideOpen && <button type="button" className="side-scrim" aria-label="关闭侧栏" tabIndex={-1} onClick={() => setSideOpen(false)} />}
    <main className="main" id="main">
      <header className="main-head drag">
        {!sideOpen && <button type="button" className="btn btn-icon" aria-label="显示侧栏" title="显示侧栏" aria-controls="app-sidebar" aria-expanded="false" onClick={() => setSideOpen(true)}><Icon name="sidebar" /></button>}
        <nav className="crumbs" aria-label="面包屑">
          <span>{crumbs[0]}</span><i aria-hidden="true">/</i><b aria-current="page" title={crumbs[1]}>{crumbs[1]}</b>
        </nav>
        <div className="main-actions">
          <a className="btn btn-sm btn-secondary" href="#/new"><Icon name="plus" size={14} />新建实验</a>
          {(view === 'overview' || view === 'task') && <button type="button" className="btn btn-icon" aria-label="刷新" title="刷新" onClick={refresh}><Icon name="refresh" /></button>}
        </div>
      </header>
      {view === 'reader' && <div className="main-full">
        <Suspense fallback={<p role="status" className="page-loading">加载阅读器…</p>}>
          <ReaderPage papers={papers} paperId={route.paper} />
        </Suspense>
      </div>}
      <div className="main-body" hidden={full}>
        <div className={`page page-${view}`}>
          {view === 'overview' && <Suspense fallback={<p role="status" className="page-loading">加载总览…</p>}>
            <OverviewPage api={api} papers={papers} />
          </Suspense>}
          {newOpened && <div hidden={view !== 'new'}>
            <Suspense fallback={<p role="status" className="page-loading">加载新建实验…</p>}>
              <NewExperimentPage api={api} papers={papers} initialProject={route.project} initialPaper={route.paper} />
            </Suspense>
          </div>}
          {openedTasks.map(id => <div key={id} hidden={view !== 'task' || route.taskId !== id}>
            <Suspense fallback={<p role="status" className="page-loading">加载任务…</p>}>
              <TaskPage taskId={id} api={api} />
            </Suspense>
          </div>)}
          {view === 'settings' && <Suspense fallback={<p role="status" className="page-loading">加载设置…</p>}>
            <Settings health={health} host={host} api={papers} />
          </Suspense>}
        </div>
      </div>
    </main>
  </div>;
}
