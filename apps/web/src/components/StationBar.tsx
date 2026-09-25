// 装置条（签名元素）：六站状态只从真实快照派生，不按当前页面点亮。
import type { Paper, ReportStatus, Run } from '../api-client/types';

type StationState = 'pending' | 'active' | 'done' | 'failed' | 'skipped';

const STATION_LABELS = ['上传论文', '解析提取', '生成代码', '沙箱执行', '修复循环', '复现报告'];

function inProgress(run: Run): boolean {
  return ['queued', 'generating', 'executing', 'repairing', 'verifying', 'cancel_requested'].includes(run.status);
}

export function deriveStations(paper: Paper | null | undefined, run: Run | null | undefined): StationState[] {
  const st: StationState[] = ['pending', 'pending', 'pending', 'pending', 'pending', 'pending'];
  if (!paper) return st;
  st[0] = 'done';
  if (paper.status === 'parsing' || paper.status === 'uploaded') {
    st[1] = 'active';
    return st;
  }
  if (paper.status === 'failed' || paper.status === 'unsupported') {
    st[1] = 'failed';
    return st;
  }
  st[1] = 'done';
  if (!run) return st;

  const terminal = !inProgress(run);
  // 生成代码
  if (run.status === 'generating') st[2] = 'active';
  else if (run.status === 'queued') return st; // queued：未开始运行，不点亮任何站
  else st[2] = 'done';

  // 沙箱执行
  if (run.status === 'executing' || run.status === 'verifying' || run.status === 'cancel_requested') st[3] = 'active';
  else if (run.status === 'completed' || run.status === 'cancelled' || run.status === 'awaiting_revision') st[3] = 'done';
  else if (run.status === 'failed') st[3] = run.repairs_used > 0 ? 'done' : (run.stage === 'code_generate' || run.stage === 'code_validate' ? 'pending' : 'failed');
  else st[3] = 'pending';

  // 修复循环：条件分支，未发生时"不需要"
  if (run.status === 'repairing') st[4] = 'active';
  else if (run.status === 'failed' && run.repairs_used > 0) st[4] = 'failed';
  else if (run.repairs_used > 0) st[4] = 'done';
  else st[4] = 'skipped';

  // 复现报告：由 report_status 决定
  const rs: ReportStatus | null = terminal ? run.report_status : null;
  if (rs === 'ready') st[5] = 'done';
  else if (rs === 'failed') st[5] = 'failed';
  else if (rs === 'pending' || rs === 'generating') st[5] = 'active';
  return st;
}

export function StationBar({ paper, run }: { paper?: Paper | null; run?: Run | null }) {
  const states = deriveStations(paper ?? null, run ?? null);
  return (
    <nav className="apparatus" aria-label="复现流水线状态">
      {STATION_LABELS.map((label, i) => (
        <FragmentRow key={label} label={label} state={states[i]} last={i === STATION_LABELS.length - 1} />
      ))}
    </nav>
  );
}

function FragmentRow({ label, state, last }: { label: string; state: StationState; last: boolean }) {
  return (
    <>
      <div className={`station ${state}`}>
        <span className="station-dot"></span>
        <span className="station-label">{label}</span>
      </div>
      {!last && <span className={state === 'done' ? 'station-link done' : 'station-link'}></span>}
    </>
  );
}
