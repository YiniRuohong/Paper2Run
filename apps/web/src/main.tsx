import { Component, StrictMode, Suspense, lazy, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';
import { resolveSurface } from './routing';
import './styles/tokens.css';
import './styles/entry.css';

const Phase01Lab = lazy(() => import('./features/lab/Phase01Lab'));
const MockWorkspace = lazy(() => import('./MockWorkspace'));
const surface = resolveSurface(window.location.pathname, import.meta.env.VITE_DATA_MODE);

class SurfaceBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    if (this.state.failed) return <main className="entry-screen" role="alert"><h1>页面加载失败</h1><p>没有生成执行结果。request_id：未产生 API 请求，请刷新页面重试。</p><a href={window.location.pathname}>重新加载页面</a></main>;
    return this.props.children;
  }
}

function UnavailableProduct() {
  return <main className="entry-screen">
    <span className="entry-label">完整产品 LIVE / REPLAY 未接入</span>
    <h1>选择验证范围</h1>
    <p>论文解析、训练、报告尚未接入真实产品 API。本入口不会请求未实现的 auth 接口，也不会把 Mock 结果标成 live。</p>
    <nav aria-label="验证入口"><a href="/lab">打开 LIVE 实验工作台</a><a href="/mock/projects">查看四视图 MOCK</a></nav>
    <p>Lab 始终调用真实 Session / Cell / Checkpoint / Restore API。X-Actor-ID 仅是本地开发身份；代码在本机运行，非生产 sandbox。</p>
  </main>;
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <SurfaceBoundary>
      <Suspense fallback={<main className="entry-screen" aria-busy="true"><p role="status">加载工作台布局…</p><div className="entry-skeleton" aria-hidden="true" /></main>}>
        {surface.kind === 'lab' ? <Phase01Lab /> : surface.kind === 'mock' ? <MockWorkspace basename={surface.basename} /> : <UnavailableProduct />}
      </Suspense>
    </SurfaceBoundary>
  </StrictMode>,
);
