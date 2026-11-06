import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { createAutonomousApi } from '../api-client/autonomous';
import { createPapersApi } from '../api-client/papers';
import { backendHost, getDesktop } from '../desktop';
import { useBackendHealth, useRecentTasks } from './hooks';
import { Sidebar, type View } from './Sidebar';
import { parseRoute } from './routes';
import { Icon } from '../ui/Icon';
import '../ui/ui.css';
import './shell.css';

// App shell (magpie-style sidebar + quiet header). Hash routes:
//   #/            总览（全部项目的自主任务，只读）
//   #/new         新建实验（全幅草稿页，第 01 步；首次上传前不创建服务端对象）
//   #/draft/<id>  草稿工作台（全幅；从当前步继续）
//   #/task/<id>   任务工作台（全幅）
//   #/settings    设置（主题、本地服务、解析模式）
// 路由解析与旧链接重定向见 routes.ts。new/draft/task 不渲染侧栏与面包屑栏。
const api = createAutonomousApi();
const papers = createPapersApi('demo-user');
const OverviewPage = lazy(() => import('../features/overview/OverviewPage'));
const DraftPage = lazy(() => import('../features/flow/DraftPage'));
const TaskPage = lazy(() => import('../features/autonomous/TaskPage'));
const Settings = lazy(() => import('../features/settings/Settings'));

const hashRoute = () => parseRoute(window.location.hash);
const narrow = () => window.matchMedia?.('(max-width: 760px)').matches ?? false;

export default function AppShell() {
  const [{ route, redirect }, setParsed] = useState(hashRoute);
  const [sideOpen, setSideOpen] = useState(() => !narrow());
  const [refreshTick, setRefreshTick] = useState(0);
  const [health, recheckHealth] = useBackendHealth();
  const taskId = route.view === 'task' ? route.taskId : null;
  const recent = useRecentTasks(api, papers, `${route.view}:${taskId ?? ''}:${refreshTick}`);
  const host = backendHost(getDesktop()?.backendUrl, window.location.host);
  useEffect(() => {
    const onHash = () => { setParsed(hashRoute()); if (narrow()) setSideOpen(false); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const refresh = useCallback(() => { setRefreshTick(x => x + 1); recheckHealth(); }, [recheckHealth]);

  // 旧入口 / 旧查询参数：重定向到新路由，并替换历史记录。
  useEffect(() => { if (redirect) window.location.replace(redirect); }, [redirect]);

  if (route.view === 'new' || route.view === 'draft' || route.view === 'task') {
    const projectId = route.view === 'draft' ? route.projectId : null;
    return <div className="shell flow">
      <main className="main" id="main">
        <Suspense fallback={<p role="status" className="page-loading">加载中…</p>}>
          {route.view === 'task'
            ? <TaskPage key={route.taskId} taskId={route.taskId} api={api} />
            : <DraftPage key={projectId ?? 'new'} projectId={projectId} api={api} papers={papers} />}
        </Suspense>
      </main>
    </div>;
  }

  const view: View = route.view;
  const crumbs: [string, string] = view === 'overview' ? ['工作区', '总览'] : ['Paper2Run', '设置'];

  return <div className={`shell${sideOpen ? '' : ' side-closed'}`}>
    {sideOpen && <Sidebar view={view} taskId={null} recent={recent.tasks} recentFailed={recent.failed} health={health} host={host} onHide={() => setSideOpen(false)} />}
    {sideOpen && <button type="button" className="side-scrim" aria-label="关闭侧栏" tabIndex={-1} onClick={() => setSideOpen(false)} />}
    <main className="main" id="main">
      <header className="main-head drag">
        {!sideOpen && <button type="button" className="btn btn-icon" aria-label="显示侧栏" title="显示侧栏" aria-controls="app-sidebar" aria-expanded="false" onClick={() => setSideOpen(true)}><Icon name="sidebar" /></button>}
        <nav className="crumbs" aria-label="面包屑">
          <span>{crumbs[0]}</span><i aria-hidden="true">/</i><b aria-current="page" title={crumbs[1]}>{crumbs[1]}</b>
        </nav>
        <div className="main-actions">
          <a className="btn btn-sm btn-secondary" href="#/new"><Icon name="plus" size={14} />新建实验</a>
          {view === 'overview' && <button type="button" className="btn btn-icon" aria-label="刷新" title="刷新" onClick={refresh}><Icon name="refresh" /></button>}
        </div>
      </header>
      <div className="main-body">
        <div className={`page page-${view}`}>
          {view === 'overview' && <Suspense fallback={<p role="status" className="page-loading">加载总览…</p>}>
            <OverviewPage api={api} papers={papers} />
          </Suspense>}
          {view === 'settings' && <Suspense fallback={<p role="status" className="page-loading">加载设置…</p>}>
            <Settings health={health} host={host} api={papers} />
          </Suspense>}
        </div>
      </div>
    </main>
  </div>;
}
