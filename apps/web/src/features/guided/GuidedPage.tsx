import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { createGuidedApi } from '../../api-client/guided';
import RealFlowPanel from './RealFlowPanel';
import { Icon } from '../dashboard/Icon';
import './guided.css';
import '../dashboard/dashboard.css';

// Control-panel shell. Two views share one sidebar:
//   #/        总览 (read-only dashboard of papers and runs)
//   #/new     新建复现任务 (the guided real flow: upload a paper, extract with a
//             real provider, review, pass the environment gate, execute in the
//             controlled runner/sandbox, export verifiable artifacts).
// The offline teaching fixture remains a backend regression path, not a UI entry.
const api = createGuidedApi('demo-user');
const Dashboard = lazy(() => import('../dashboard/Dashboard'));
const TaskDetail = lazy(() => import('../dashboard/TaskDetail'));

type View = 'overview' | 'new' | 'task';
const viewFromHash = (): View => {
  const h = window.location.hash;
  return h.startsWith('#/new') ? 'new' : h.startsWith('#/task/') ? 'task' : 'overview';
};
const taskIdFromHash = (): string => decodeURIComponent(window.location.hash.slice('#/task/'.length));

// Backend reachability, shown in the sidebar footer. Any HTTP response from
// /healthz means the server is up; only a network failure counts as offline.
function useBackendHealth(): 'checking' | 'up' | 'down' {
  const [state, setState] = useState<'checking' | 'up' | 'down'>('checking');
  useEffect(() => {
    let live = true;
    const probe = async () => {
      try {
        const res = await fetch('/healthz', { cache: 'no-store' });
        if (live) setState(res.ok ? 'up' : 'down');
      } catch { if (live) setState('down'); }
    };
    void probe();
    const id = window.setInterval(probe, 10_000);
    return () => { live = false; window.clearInterval(id); };
  }, []);
  return state;
}

export default function GuidedPage() {
  const [view, setView] = useState<View>(viewFromHash);
  const [, setTick] = useState(0);
  const health = useBackendHealth();
  useEffect(() => {
    const onHash = () => { setView(viewFromHash()); setTick(x => x + 1); };
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);
  const go = useCallback((next: View) => {
    window.location.hash = next === 'new' ? '#/new' : '#/';
  }, []);

  return <div className="guided-page app-shell">
    <aside className="app-side">
      <div className="app-brand">
        <span className="guided-mark" aria-hidden="true" />
        <b>Paper2Run</b>
        <span className="app-tag">本地</span>
      </div>
      <nav className="app-nav" aria-label="主导航">
        <span className="app-group">工作区</span>
        <button className={view !== 'new' ? 'on' : ''} aria-current={view !== 'new' ? 'page' : undefined} onClick={() => go('overview')}><Icon name="home" />总览</button>
        <button className={view === 'new' ? 'on' : ''} aria-current={view === 'new' ? 'page' : undefined} onClick={() => go('new')}><Icon name="play" />新建任务</button>
      </nav>
      <div className="app-status" role="status">
        <span className={`dot ${health === 'up' ? 'ok' : health === 'down' ? 'bad' : 'idle'}`} />
        {health === 'up' ? '后端已连接' : health === 'down' ? '后端未连接' : '正在检查后端'}
      </div>
    </aside>
    <main className="app-main">
      <div className="app-top">
        <nav aria-label="面包屑"><span>{view === 'new' ? '任务' : '工作区'}</span><i>/</i><b>{view === 'overview' ? '总览' : view === 'task' ? '任务详情' : '新建'}</b></nav>
      </div>
      {view === 'task'
        ? <Suspense fallback={<p role="status" className="dash-muted">加载任务…</p>}><TaskDetail key={taskIdFromHash()} api={api} paperId={taskIdFromHash()} onBack={() => go('overview')} /></Suspense>
        : view === 'overview'
        ? <Suspense fallback={<p role="status" className="dash-muted">加载总览…</p>}><Dashboard api={api} onNew={() => go('new')} /></Suspense>
        : <div className="wizard">
            <header className="wizard-head">
              <button className="guided-link" onClick={() => go('overview')}>← 返回总览</button>
              <h1>新建复现任务</h1>
              <p>跟随下面的步骤，从上传论文一路走到可核验的复现报告。</p>
            </header>
            <RealFlowPanel api={api} />
          </div>}
    </main>
  </div>;
}
