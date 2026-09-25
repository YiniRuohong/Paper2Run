import { Link } from 'react-router-dom';
import type { Paper, Run } from '../api-client/types';
import { RunStatusTag } from './StatusTag';

export function SideNav({
  current,
  paper,
  run,
}: {
  current: 'projects' | 'review' | 'run' | 'report';
  paper?: Paper | null;
  run?: Run | null;
}) {
  const runTerminal = run && !['queued', 'generating', 'executing', 'repairing', 'verifying', 'cancel_requested'].includes(run.status);
  return (
    <aside className="sidebar">
      <Link to="/projects" className={`nav-item ${current === 'projects' ? 'on' : ''}`}>
        <span className="idx">01</span>工作台
      </Link>
      {paper ? (
        <Link to={`/papers/${paper.paper_id}/review`} className={`nav-item ${current === 'review' ? 'on' : ''}`}>
          <span className="idx">02</span>论文解析
        </Link>
      ) : (
        <button type="button" className={`nav-item ${current === 'review' ? 'on' : ''}`} disabled title="先上传论文后可用">
          <span className="idx">02</span>论文解析
        </button>
      )}
      {run ? (
        <Link to={`/runs/${run.id}`} className={`nav-item ${current === 'run' ? 'on' : ''}`}>
          <span className="idx">03</span>生成与执行
        </Link>
      ) : (
        <button type="button" className={`nav-item ${current === 'run' ? 'on' : ''}`} disabled title="尚无运行任务">
          <span className="idx">03</span>生成与执行
        </button>
      )}
      {run && runTerminal && run.report_status === 'ready' ? (
        <Link to={`/runs/${run.id}/report`} className={`nav-item ${current === 'report' ? 'on' : ''}`}>
          <span className="idx">04</span>复现报告
        </Link>
      ) : (
        <button type="button" className={`nav-item ${current === 'report' ? 'on' : ''}`} disabled title="尚无就绪报告">
          <span className="idx">04</span>复现报告
        </button>
      )}

      <div className="sidebar-sep"></div>

      <div className="side-card">
        <div className="sc-eyebrow">当前项目</div>
        <div className="sc-title">{paper?.title ?? '无活动论文'}</div>
        <div className="sc-status">{run ? <RunStatusTagInline run={run} /> : '未在运行'}</div>
      </div>

      <div className="sidebar-note">
        {run ? `${run.id} · attempt ${run.attempt_index ?? '-'} · 修复 ${run.repairs_used}/${run.repairs_limit}` : 'Mock 场景可从右上角切换'}
      </div>
    </aside>
  );
}

function RunStatusTagInline({ run }: { run: Run }) {
  return (
    <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
      <RunStatusTag status={run.status} />
    </span>
  );
}
