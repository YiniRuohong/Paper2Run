import { Suspense, lazy, useCallback, useEffect, useState } from 'react';
import { createGuidedApi } from '../../api-client/guided';
import RealFlowPanel from './RealFlowPanel';
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

type View = 'overview' | 'new';
const viewFromHash = (): View => (window.location.hash.startsWith('#/new') ? 'new' : 'overview');

export default function GuidedPage() {
  const [view, setView] = useState<View>(viewFromHash);
  useEffect(() => {
    const onHash = () => setView(viewFromHash());
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
        <div><b>Paper2Run</b><small>论文复现控制台</small></div>
      </div>
      <nav className="app-nav" aria-label="主导航">
        <button className={view === 'overview' ? 'on' : ''} aria-current={view === 'overview' ? 'page' : undefined} onClick={() => go('overview')}>总览</button>
        <button className={view === 'new' ? 'on' : ''} aria-current={view === 'new' ? 'page' : undefined} onClick={() => go('new')}>新建任务</button>
      </nav>
      <p className="app-side-foot">API 密钥只驻内存，报告如实标注证据等级。</p>
    </aside>
    <main className="app-main">
      {view === 'overview'
        ? <Suspense fallback={<p role="status">加载总览…</p>}><Dashboard api={api} onNew={() => go('new')} /></Suspense>
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
