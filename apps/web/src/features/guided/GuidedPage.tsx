import { Suspense, lazy, useCallback, useEffect, useRef, useState } from 'react';
import { createGuidedApi } from '../../api-client/guided';
import { backendHost, getDesktop } from '../../desktop';
import { useBackendHealth, useRecentTasks } from '../../shell/hooks';
import { Sidebar, type View } from '../../shell/Sidebar';
import { Icon } from '../../ui/Icon';
import RealFlowPanel from './RealFlowPanel';
import '../../ui/ui.css';
import '../../shell/shell.css';
import './guided.css';
import '../dashboard/dashboard.css';

// App shell (magpie-style sidebar + quiet header). Hash routes:
//   #/            总览 (read-only dashboard of papers and runs)
//   #/new         新建复现任务 (the guided real flow: upload a paper, extract with a
//                 real provider, review, pass the environment gate, execute in the
//                 controlled runner/sandbox, export verifiable artifacts)
//   #/task/<id>   任务详情 (read-only)
//   #/settings    设置 (theme, local service, desktop paths)
// The offline teaching fixture remains a backend regression path, not a UI entry.
const api = createGuidedApi('demo-user');
const Dashboard = lazy(() => import('../dashboard/Dashboard'));
const TaskDetail = lazy(() => import('../dashboard/TaskDetail'));
const Settings = lazy(() => import('../settings/Settings'));

interface Route { view: View; taskId: string | null }
const routeFromHash = (): Route => {
  const h = window.location.hash;
  if (h.startsWith('#/new')) return { view: 'new', taskId: null };
  if (h.startsWith('#/task/')) return { view: 'task', taskId: decodeURIComponent(h.slice('#/task/'.length)) };
  if (h.startsWith('#/settings')) return { view: 'settings', taskId: null };
  return { view: 'overview', taskId: null };
};
const narrow = () => window.matchMedia?.('(max-width: 760px)').matches ?? false;

export default function GuidedPage() {
  const [route, setRoute] = useState<Route>(routeFromHash);
  const [sideOpen, setSideOpen] = useState(() => !narrow());
  const [refreshTick, setRefreshTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const [health, recheckHealth] = useBackendHealth();
  const recent = useRecentTasks(api, `${route.view}:${route.taskId ?? ''}:${refreshTick}`);
  const host = backendHost(getDesktop()?.backendUrl, window.location.host);
  useEffect(() => {
    const onHash = () => { setRoute(routeFromHash()); if (narrow()) setSideOpen(false); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const goOverview = useCallback(() => { window.location.hash = '#/'; }, []);
  const goNew = useCallback(() => { window.location.hash = '#/new'; }, []);
  const refresh = useCallback(() => { setRefreshTick(x => x + 1); recheckHealth(); }, [recheckHealth]);
  const { view, taskId } = route;
  // The flow holds its progress (and the provider api_key) only in memory, so it
  // stays mounted once opened; leaving the page must not discard it.
  const flowOpened = useRef(false);
  if (view === 'new') flowOpened.current = true;
  const [flowKey, setFlowKey] = useState(0);
  const restartFlow = useCallback(() => {
    if (window.confirm('放弃当前流程并重新开始？已上传的论文和运行记录会保留，API Key 需要重新输入。')) setFlowKey(x => x + 1);
  }, []);
  const taskTitle = view === 'task' ? recent.tasks?.find(t => t.paperId === taskId)?.title ?? '任务详情' : '';
  const crumbs: [string, string] = view === 'overview' ? ['工作区', '总览']
    : view === 'new' ? ['工作区', '新建任务']
    : view === 'task' ? ['任务', taskTitle]
    : ['Paper2Run', '设置'];
  const canRefresh = view === 'overview' || view === 'task';

  return <div className={`shell${sideOpen ? '' : ' side-closed'}`}>
    {sideOpen && <Sidebar view={view} taskId={taskId} recent={recent.tasks} recentFailed={recent.failed} health={health} host={host} onHide={() => setSideOpen(false)} />}
    {sideOpen && <button type="button" className="side-scrim" aria-label="关闭侧栏" tabIndex={-1} onClick={() => setSideOpen(false)} />}
    <main className="main" id="main">
      <header className="main-head drag">
        {!sideOpen && <button type="button" className="btn btn-icon" aria-label="显示侧栏" title="显示侧栏" aria-controls="app-sidebar" aria-expanded="false" onClick={() => setSideOpen(true)}><Icon name="sidebar" /></button>}
        <nav className="crumbs" aria-label="面包屑">
          <span>{crumbs[0]}</span><i aria-hidden="true">/</i><b aria-current="page" title={crumbs[1]}>{crumbs[1]}</b>
        </nav>
        <div className="main-actions">
          {canRefresh && <button type="button" className="btn btn-icon" aria-label="刷新" title="刷新" aria-busy={busy} onClick={refresh}><Icon name="refresh" spin={busy} /></button>}
          {view === 'new' && <button type="button" className="btn btn-sm" onClick={restartFlow}>重新开始</button>}
        </div>
      </header>
      <div className="main-body">
        <div className={`page page-${view}`}>
          {view === 'task' && taskId !== null
            ? <Suspense fallback={<p role="status" className="page-loading">加载任务…</p>}><TaskDetail key={taskId} api={api} paperId={taskId} onBack={goOverview} refreshSignal={refreshTick} /></Suspense>
            : view === 'overview'
            ? <Suspense fallback={<p role="status" className="page-loading">加载总览…</p>}><Dashboard api={api} onNew={goNew} refreshSignal={refreshTick} onBusy={setBusy} /></Suspense>
            : view === 'settings'
            ? <Suspense fallback={<p role="status" className="page-loading">加载设置…</p>}><Settings health={health} host={host} /></Suspense>
            : null}
          {flowOpened.current && <div key={flowKey} hidden={view !== 'new'}><RealFlowPanel api={api} /></div>}
        </div>
      </div>
    </main>
  </div>;
}
