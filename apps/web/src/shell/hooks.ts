import { useEffect, useState } from 'react';
import type { AutonomousApi } from '../api-client/autonomous';
import type { PapersApi } from '../api-client/papers';

export type Health = 'checking' | 'up' | 'down';

// Backend reachability for the sidebar footer and settings. Any HTTP response
// from /healthz with ok status means the server is up; a network failure or a
// non-ok status counts as offline.
export function useBackendHealth(): [Health, () => void] {
  const [state, setState] = useState<Health>('checking');
  const [rev, setRev] = useState(0);
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
  }, [rev]);
  return [state, () => setRev(x => x + 1)];
}

export const RECENT_LIMIT = 6;

export interface RecentTask {
  id: string;
  projectId: string;
  /** 论文名（项目名）；总览用它做标题。 */
  projectName?: string;
  objective: string;
  state: string;
  /** 程序判定的结局；null 表示尚未判定。 */
  outcome: string | null;
  updatedAt: string;
}

/** 草稿：读取成功但还没有任何自主任务的项目。 */
export interface RecentDraft { projectId: string; name: string; updatedAt: string }

// 读取全部项目的自主任务，按更新时间倒序。单个项目失败不拖垮整页：
// 失败的项目跳过，并在返回值里计数。任务数为 0 的项目作为草稿返回。
export async function loadAutonomousTasks(api: AutonomousApi, papers: PapersApi, signal?: AbortSignal): Promise<{ tasks: RecentTask[]; drafts: RecentDraft[]; failedProjects: number }> {
  const projects = await papers.listProjects(signal);
  const tasks: RecentTask[] = [];
  const drafts: RecentDraft[] = [];
  let failedProjects = 0;
  await Promise.all(projects.map(async project => {
    try {
      const list = await api.tasks(project.id);
      if (signal?.aborted) return;
      if (list.length === 0) drafts.push({ projectId: project.id, name: project.name, updatedAt: project.updated_at ?? '' });
      for (const task of list) {
        tasks.push({ id: task.id, projectId: task.project_id || project.id, projectName: project.name, objective: task.objective, state: task.state, outcome: task.outcome ?? null, updatedAt: task.updated_at ?? task.created_at ?? '' });
      }
    } catch {
      failedProjects += 1;
    }
  }));
  tasks.sort((a, b) => (b.updatedAt || '').localeCompare(a.updatedAt || ''));
  drafts.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  return { tasks, drafts, failedProjects };
}

// Most recent tasks for the sidebar. Read-only; reloads when `signal` changes
// (route change / explicit refresh) and every 20s.
export function useRecentTasks(api: AutonomousApi, papers: PapersApi, signal: unknown): { tasks: RecentTask[] | null; failed: boolean } {
  const [tasks, setTasks] = useState<RecentTask[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const ctl = new AbortController();
    const load = () => loadAutonomousTasks(api, papers, ctl.signal).then(result => {
      if (ctl.signal.aborted) return;
      setTasks(result.tasks.slice(0, RECENT_LIMIT));
      setFailed(false);
    }).catch(() => { if (!ctl.signal.aborted) setFailed(true); });
    void load();
    const id = window.setInterval(() => { if (document.visibilityState !== 'hidden') void load(); }, 20_000);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [api, papers, signal]);
  return { tasks, failed };
}
