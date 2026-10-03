import type { GuidedApi } from '../../api-client/guided.ts';
import { GUIDED_PATHS as P, validateExport } from '../../api-client/guided.ts';
import type { GuidedExport, GuidedReportStatus } from '../../api-client/guided-types.ts';

// Read-only read-backs for the guided page. Each helper owns one AbortController,
// reports failure exactly once through onError, and returns a stop function that
// aborts superseded work. They are restartable: the page re-invokes them with
// unchanged ids after an explicit read-only refresh (readRev). They only ever
// issue GETs — no writes, no automatic replay of any intent.

export type ReadBackStop = () => void;

// Structural contract shared by the offline GuidedController and the real-flow
// controller: run/report polling only needs these three members.
export interface RunPollingSource {
  pollRun(): Promise<void>;
  pollReport(): Promise<void>;
  getSnapshot(): { runId: string | null; run: { status: string } | null; reportStatus: GuidedReportStatus['status'] | null };
}

export function requireRecord(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('INVALID_DTO');
  return value as Record<string, unknown>;
}

export function readExtraction(args: {
  api: GuidedApi;
  paperId: string;
  extractionId: string | null;
  onValue: (value: Record<string, unknown>) => void;
  onError: () => void;
}): ReadBackStop {
  const abort = new AbortController();
  void args.api.read(`/papers/${encodeURIComponent(args.paperId)}/extraction`, value => {
    const x = requireRecord(value);
    if (x.paper_id !== args.paperId || x.extraction_id !== args.extractionId || !Array.isArray(x.source_refs)) throw Error('RESOURCE_MISMATCH');
    return x;
  }, abort.signal).then(value => { if (!abort.signal.aborted) args.onValue(value); }).catch(() => { if (!abort.signal.aborted) args.onError(); });
  return () => abort.abort();
}

export function pollRunAndReport(args: {
  controller: RunPollingSource;
  onError: () => void;
  initialDelayMs?: number;
  intervalMs?: number;
}): ReadBackStop {
  let live = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  const poll = async () => {
    if (!live) return;
    try {
      await args.controller.pollRun();
      if (!live) return; // Never start the report read once this generation was stopped.
      await args.controller.pollReport();
    } catch {
      if (live) args.onError();
      return; // Polling stays stopped; an explicit read-only refresh restarts it.
    }
    if (!live) return;
    const state = args.controller.getSnapshot();
    const runActive = !state.run || ['queued', 'running', 'evaluating', 'cancel_requested'].includes(state.run.status);
    const reportActive = ['pending', 'generating'].includes(state.reportStatus ?? '');
    // A completed run with no report awaits an explicit build, not another GET.
    // New build ids and explicit read-only refresh restart the page effect.
    if (state.runId && (runActive || reportActive)) timer = setTimeout(poll, args.intervalMs ?? 1600);
  };
  timer = setTimeout(poll, args.initialDelayMs ?? 1000);
  return () => { live = false; clearTimeout(timer); };
}

export function readReport(args: {
  api: GuidedApi;
  runId: string;
  onValue: (value: Record<string, unknown>) => void;
  onError: () => void;
}): ReadBackStop {
  const abort = new AbortController();
  void args.api.read(P.report(args.runId), requireRecord, abort.signal).then(value => { if (!abort.signal.aborted) args.onValue(value); }).catch(() => { if (!abort.signal.aborted) args.onError(); });
  return () => abort.abort();
}

export function pollExports(args: {
  api: GuidedApi;
  runId: string;
  exportId: string;
  onValue: (items: GuidedExport[]) => void;
  onError: () => void;
  intervalMs?: number;
}): ReadBackStop {
  const abort = new AbortController();
  let timer: ReturnType<typeof setTimeout>;
  const poll = async () => {
    try {
      const page = await args.api.readArray(P.exports(args.runId), validateExport, abort.signal);
      if (abort.signal.aborted) return;
      const selected = page.items.filter(x => x.id === args.exportId && x.run_id === args.runId);
      args.onValue(selected);
      if (!selected.length || selected.some(x => ['queued', 'running'].includes(x.status))) timer = setTimeout(poll, args.intervalMs ?? 1200);
    } catch {
      if (!abort.signal.aborted) args.onError();
    }
  };
  void poll();
  return () => { abort.abort(); clearTimeout(timer); };
}
