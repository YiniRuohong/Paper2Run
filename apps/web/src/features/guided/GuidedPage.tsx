import { createGuidedApi } from '../../api-client/guided';
import RealFlowPanel from './RealFlowPanel';
import './guided.css';

// The product surface is the real flow only: upload a paper, extract with a
// real provider, review the spec, pass the environment gate, execute in the
// controlled runner/sandbox, and export verifiable artifacts. The offline
// teaching fixture remains a backend regression path, not a UI entry.
const api = createGuidedApi('demo-user');

export default function GuidedPage() {
  return <main className="guided-page">
    <header className="guided-header">
      <div className="guided-brand">
        <span className="guided-mark" aria-hidden="true" />
        <div>
          <span className="guided-kicker">PAPER2RUN · 实操工作台</span>
          <h1>从一篇论文，到可核验的复现结果</h1>
        </div>
      </div>
      <span className="guided-actor">actor · demo-user</span>
    </header>
    <p className="guided-lede">
      上传论文 → Provider 结构化抽取 → 人工审核方案 → 环境检查门禁 → 本机受控执行 / 沙箱复现 → 证据分级报告与导出。
      每一步真实可查：API 密钥只驻内存，报告如实标注证据等级。
    </p>
    <RealFlowPanel api={api} />
  </main>;
}
