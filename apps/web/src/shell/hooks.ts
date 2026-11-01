import { useCallback, useEffect, useState } from 'react';
import type { GuidedApi } from '../api-client/guided';
import { loadSnapshot } from '../features/dashboard/data';
import { buildTasks, type Task } from '../features/dashboard/tasks';

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
  return [state, useCallback(() => setRev(x => x + 1), [])];
}

export const RECENT_LIMIT = 6;

// Most recent tasks for the sidebar. Read-only (GET via loadSnapshot); reloads
// when `signal` changes (route change / explicit refresh) and every 20s.
export function useRecentTasks(api: GuidedApi, signal: unknown): { tasks: Task[] | null; failed: boolean } {
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const ctl = new AbortController();
    const load = () => loadSnapshot(api, ctl.signal).then(result => {
      if (ctl.signal.aborted) return;
      setTasks(buildTasks(result.snapshot).slice(0, RECENT_LIMIT)); setFailed(false);
    }).catch(() => { if (!ctl.signal.aborted) setFailed(true); });
    void load();
    const id = window.setInterval(() => { if (document.visibilityState !== 'hidden') void load(); }, 20_000);
    return () => { ctl.abort(); window.clearInterval(id); };
  }, [api, signal]);
  return { tasks, failed };
}
