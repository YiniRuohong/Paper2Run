import type { GuidedRun, GuidedRunEvent, GuidedReportStatus, Step } from '../../api-client/guided-types.ts';

export type GuidedViewStep = Step | 'paper';
export type GuidedStep = 'paper' | 'extraction' | 'experiment' | 'run' | 'report' | 'export';
export interface GuidedState {
  actor: string;
  exampleId: string;
  viewStep: GuidedStep;
  step: GuidedStep;
  epoch: number;
  projectId: string | null;
  paperId: string | null;
  extractionId: string | null;
  reviewedExtractionId: string | null;
  experimentId: string | null;
  revisionId: string | null;
  confirmationId: string | null;
  runId: string | null;
  run: GuidedRun | null;
  report: GuidedReportStatus | null;
  reportStatus: GuidedReportStatus['status'] | null;
  exportId: string | null;
  exportIds: string[];
  events: GuidedRunEvent[];
  intents: Record<string, { key: string; path: string; body: string; state: string }>;
  history: Array<{ revisionId: string | null; runId: string | null; exportIds: string[] }>;
  error: string | null;
}

export type GuidedAction =
  | { type: 'bootstrap'; epoch: number; value: { project_id: string; paper_id: string; extraction_id: string } }
  | { type: 'refs'; value: Partial<Pick<GuidedState, 'projectId' | 'paperId' | 'extractionId' | 'experimentId' | 'revisionId' | 'confirmationId' | 'runId' | 'exportId' | 'exportIds'>> }
  | { type: 'review'; extractionId: string }
  | { type: 'view'; step: GuidedStep }
  | { type: 'edit' }
  | { type: 'run'; epoch: number; value: GuidedRun }
  | { type: 'report'; epoch: number; value: GuidedReportStatus }
  | { type: 'events'; epoch: number; value: GuidedRunEvent[] }
  | { type: 'error'; message: string | null };

export function createGuidedState(options: { actor?: string } = {}): GuidedState {
  return { actor: options.actor ?? 'demo-user', exampleId: 'offline-smoke', viewStep: 'paper', step: 'paper', epoch: 0, projectId: null, paperId: null, extractionId: null, reviewedExtractionId: null, experimentId: null, revisionId: null, confirmationId: null, runId: null, run: null, report: null, reportStatus: null, exportId: null, exportIds: [], events: [], intents: {}, history: [], error: null };
}
export function canViewStep(s: GuidedState, step: GuidedStep): boolean {
  if (step === 'paper') return true;
  if (step === 'extraction') return s.extractionId !== null;
  if (step === 'experiment') return s.reviewedExtractionId !== null;
  if (step === 'run') return s.confirmationId !== null;
  if (step === 'report') return s.run?.status === 'completed';
  return s.reportStatus === 'ready';
}
function sameRun(s: GuidedState, value: GuidedRun): boolean { return s.runId === null || s.runId === value.run_id; }
export function guidedReducer(s: GuidedState, a: GuidedAction): GuidedState {
  if (a.type === 'error') return { ...s, error: a.message };
  if (a.type === 'bootstrap') {
    if (a.epoch !== s.epoch) return s;
    return { ...s, projectId: a.value.project_id, paperId: a.value.paper_id, extractionId: a.value.extraction_id, error: null };
  }
  if (a.type === 'refs') return { ...s, ...a.value, error: null };
  if (a.type === 'review') {
    if (s.extractionId !== a.extractionId) return s;
    return { ...s, reviewedExtractionId: a.extractionId, error: null };
  }
  if (a.type === 'view') {
    if (!canViewStep(s, a.step)) return { ...s, error: a.step === 'experiment' ? 'EXTRACTION_REVIEW_REQUIRED' : a.step === 'run' ? 'CONFIRMATION_REQUIRED' : a.step === 'report' ? 'REPORT_NOT_READY' : 'STEP_LOCKED' };
    return { ...s, viewStep: a.step, step: a.step, error: null };
  }
  if (a.type === 'edit') return { ...s, epoch: s.epoch + 1, viewStep: 'experiment', step: 'experiment', runId: null, run: null, report: null, reportStatus: null, exportId: null, exportIds: [], events: [], revisionId: null, confirmationId: null, history: [...s.history, { revisionId: s.revisionId, runId: s.runId, exportIds: s.exportIds }], error: null };
  if (a.epoch !== s.epoch) return s;
  if (a.type === 'run') {
    if (!sameRun(s, a.value)) return s;
    return { ...s, runId: a.value.run_id, run: a.value, error: null };
  }
  if (a.type === 'report') {
    if (s.runId !== null && a.value.run_id !== s.runId) return s;
    return { ...s, report: a.value, reportStatus: a.value.status, error: null };
  }
  if (a.type === 'events') {
    const merged = new Map(s.events.filter(e => e.run_id === s.runId).map(e => [`${e.run_id}:${e.seq}`, e]));
    for (const event of a.value) if (event.run_id === s.runId) merged.set(`${event.run_id}:${event.seq}`, event);
    return { ...s, events: [...merged.values()].sort((x, y) => x.seq - y.seq) };
  }
  return s;
}
