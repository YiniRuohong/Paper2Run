import { Component, StrictMode, Suspense, lazy, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import './styles/tokens.css';
import './styles/entry.css';
import { applyShell } from './desktop';
import { startTheme } from './theme';

applyShell();
startTheme();

const GuidedPage = lazy(() => import('./features/guided/GuidedPage'));

class SurfaceBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return (
      <main className="entry-screen" role="alert">
        <h1>页面加载失败</h1>
        <p>请刷新页面重试。</p>
        <a href={window.location.pathname}>重新加载页面</a>
      </main>
    );
    return this.props.children;
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <SurfaceBoundary>
      <Suspense fallback={
        <main className="entry-screen" aria-busy="true">
          <p role="status">加载工作台…</p>
          <div className="entry-skeleton" aria-hidden="true" />
        </main>
      }>
        <GuidedPage />
      </Suspense>
    </SurfaceBoundary>
  </StrictMode>,
);
