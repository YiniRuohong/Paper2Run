import type { Snapshot, TaskRun } from './data.ts';

// Pure task derivation shared by the overview, the task detail and the
// sidebar's recent-task list. No I/O here: callers pass a loaded Snapshot.

export type StatusTone = 'ok' | 'run' | 'bad' | 'idle';
export interface StatusLabel { text: string; tone: StatusTone }

export const RUN_LABEL: Record<string, StatusLabel> = {
  completed: { text: '已完成', tone: 'ok' },
  running: { text: '执行中', tone: 'run' },
  evaluating: { text: '评估中', tone: 'run' },
  queued: { text: '排队中', tone: 'run' },
  created: { text: '已登记', tone: 'idle' },
  failed: { text: '失败', tone: 'bad' },
  cancelled: { text: '已取消', tone: 'idle' },
  cancel_requested: { text: '取消中', tone: 'idle' },
  awaiting_revision: { text: '待修订', tone: 'idle' },
};
export const PAPER_LABEL: Record<string, StatusLabel> = {
  uploaded: { text: '已上传', tone: 'idle' },
  processing: { text: '处理中', tone: 'run' },
  ready: { text: '就绪', tone: 'idle' },
  failed: { text: '失败', tone: 'bad' },
};
export const MODE_LABEL: Record<string, string> = { 'local-cpu': '本机 CPU', 'local-fixture': '教学 fixture', sandbox: '沙箱', 'sandbox-python': '沙箱' };
// Labels for the REPORT verdict only. run.verdict comes from the evaluator, which
// never compares against the paper, so it must not be shown as a paper comparison
// (and 'matched' is never labelled as agreeing with the paper).
export const VERDICT_LABEL: Record<string, string> = { matched: '评测通过（未与论文比较）', mismatched: '与论文不符', inconclusive: '无法判定', not_comparable: '不可直接比较' };
export const ACTIVE_RUN = new Set(['queued', 'running', 'evaluating', 'cancel_requested']);

export type TaskFilter = 'all' | 'active' | 'done' | 'failed';

export function when(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export interface Task {
  paperId: string;
  title: string;
  paperStatus: string;
  createdAt: string | null;
  run: TaskRun | null;
  runCount: number;
}

export function buildTasks(snap: Snapshot): Task[] {
  const expToPaper = new Map(snap.experiments.map(e => [e.id, e.paperId]));
  const byPaper = new Map<string, TaskRun[]>();
  for (const run of snap.runs) {
    const paperId = run.experimentId ? expToPaper.get(run.experimentId) : undefined;
    if (!paperId) continue;
    byPaper.set(paperId, [...(byPaper.get(paperId) ?? []), run]);
  }
  return snap.papers.map(p => {
    const runs = (byPaper.get(p.id) ?? []).sort((a, b) => (b.createdAt ?? '').localeCompare(a.createdAt ?? ''));
    return { paperId: p.id, title: p.title ?? '未命名论文', paperStatus: p.status, createdAt: p.createdAt, run: runs[0] ?? null, runCount: runs.length };
  }).sort((a, b) => (b.run?.createdAt ?? b.createdAt ?? '').localeCompare(a.run?.createdAt ?? a.createdAt ?? ''));
}

export function taskBucket(t: Task): TaskFilter {
  if (!t.run) return 'all';
  if (ACTIVE_RUN.has(t.run.status)) return 'active';
  if (t.run.status === 'completed') return 'done';
  if (t.run.status === 'failed') return 'failed';
  return 'all';
}

// Latest run status if any, otherwise the paper status.
export function taskStatus(t: Task): StatusLabel {
  if (t.run) return RUN_LABEL[t.run.status] ?? { text: t.run.status, tone: 'idle' };
  return PAPER_LABEL[t.paperStatus] ?? { text: t.paperStatus, tone: 'idle' };
}

export const taskHref = (paperId: string): string => `#/task/${encodeURIComponent(paperId)}`;
