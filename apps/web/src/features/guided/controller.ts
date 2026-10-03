import type { GuidedApi } from '../../api-client/guided.ts';
import { GUIDED_PATHS as P, validateBootstrap, validateConfirmation, validateExperiment, validateExportAccepted, validateReportBuildAccepted, validateReportStatus, validateRevision, validateRun, validateRunAccepted } from '../../api-client/guided.ts';
import type { WriteIntent } from '../../api-client/guided-types.ts';
import { createGuidedState, guidedReducer, type GuidedState } from './state.ts';
import { prepareIntent, storageKey, type IntentStorage } from './intents.ts';

export interface GuidedControllerOptions { api: GuidedApi; storage?: IntentStorage; actor?: string }
const refKeys = ['projectId', 'paperId', 'extractionId', 'reviewedExtractionId', 'experimentId', 'revisionId', 'confirmationId', 'runId', 'exportId'] as const;
const object = (v: unknown): Record<string, unknown> => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw Error('INVALID_DTO');
  return v as Record<string, unknown>;
};
const status = (e: unknown) => e && typeof e === 'object' ? (e as { status?: number }).status : undefined;

export class GuidedController {
  private state: GuidedState;
  private listeners = new Set<() => void>();
  private disposed = false;
  private busy = false;
  private pending: WriteIntent | null = null;
  private api: GuidedApi;
  private storage?: IntentStorage;
  private actor: string;
  private reads = new AbortController();
  constructor(options: GuidedControllerOptions) {
    this.api = options.api;
    this.actor = options.actor ?? 'demo-user';
    this.storage = options.storage;
    this.state = createGuidedState({ actor: this.actor });
    try {
      const raw = this.storage?.getItem(storageKey(this.actor));
      if (!raw) return;
      if (raw.length > 150_000) throw Error('INVALID_STORAGE');
      const saved = object(JSON.parse(raw));
      if (saved.actor !== this.actor || saved.version !== 1) throw Error('INVALID_STORAGE');
      const refs = object(saved.refs);
      for (const key of refKeys) {
        const value = refs[key];
        if (value !== null && (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(value))) throw Error('INVALID_STORAGE');
        this.state[key] = value as string | null;
      }
      if (this.state.reviewedExtractionId !== this.state.extractionId) this.state.reviewedExtractionId = null;
      if (saved.pending !== null) {
        const i = object(saved.pending);
        if (typeof i.key !== 'string' || !/^[a-zA-Z0-9_-]{1,128}$/.test(i.key) || typeof i.path !== 'string' || typeof i.body !== 'string' || i.body.length > 100_000 || !['prepared', 'unknown'].includes(String(i.state))) throw Error('INVALID_STORAGE');
        object(JSON.parse(i.body));
        this.validator(i.path); // Only current resource paths may be replayed.
        this.pending = { key: i.key, path: i.path, body: i.body, state: 'unknown' };
      }
    } catch {
      this.state = { ...createGuidedState({ actor: this.actor }), error: 'INVALID_STORAGE' };
      this.pending = null;
    }
  }
  getSnapshot = (): GuidedState => this.state;
  getState = (): GuidedState => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private dispatch(action: Parameters<typeof guidedReducer>[1]) {
    if (this.disposed) return;
    this.state = guidedReducer(this.state, action);
    this.listeners.forEach(listener => listener());
  }
  private persist() {
    if (!this.storage) throw Error('LOCAL_STORAGE_UNAVAILABLE');
    const refs = Object.fromEntries(refKeys.map(key => [key, this.state[key]]));
    try { this.storage.setItem(storageKey(this.actor), JSON.stringify({ version: 1, actor: this.actor, refs, pending: this.pending })); }
    catch { throw Error('LOCAL_STORAGE_UNAVAILABLE'); }
  }
  private validator(path: string): (v: unknown) => unknown {
    const s = this.state;
    if (path === P.bootstrap) return validateBootstrap;
    if (s.projectId && path === P.experiments(s.projectId)) return validateExperiment;
    if (s.projectId && path === `/projects/${encodeURIComponent(s.projectId)}/spec-revisions`) return validateRevision;
    if (s.revisionId && path === P.confirm(s.revisionId)) return validateConfirmation;
    if (s.projectId && path === P.runs(s.projectId)) return validateRunAccepted;
    if (s.runId && path === P.reportBuilds(s.runId)) return validateReportBuildAccepted;
    if (s.runId && path === P.exports(s.runId)) return validateExportAccepted;
    throw Error('INVALID_INTENT_PATH');
  }
  private accept(path: string, value: unknown) {
    if (path === P.bootstrap) {
      this.dispatch({ type: 'bootstrap', epoch: this.state.epoch, value: validateBootstrap(value) });
    } else if (path.endsWith('/experiments')) {
      const v = validateExperiment(value);
      if (v.project_id !== this.state.projectId || v.paper_id !== this.state.paperId) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'refs', value: { experimentId: v.experiment_id } });
    } else if (path.endsWith('/spec-revisions')) {
      const v = validateRevision(value);
      if (v.experiment_id !== this.state.experimentId) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'refs', value: { revisionId: v.id } });
    } else if (path.endsWith('/confirm')) {
      const v = validateConfirmation(value);
      if (v.revision_id !== this.state.revisionId || v.confirmed_by !== this.actor) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'refs', value: { confirmationId: v.confirmation_id } });
    } else if (path.endsWith('/runs')) {
      this.dispatch({ type: 'refs', value: { runId: validateRunAccepted(value).run_id } });
    } else if (path.endsWith('/exports')) {
      const id = validateExportAccepted(value).export_id;
      this.dispatch({ type: 'refs', value: { exportId: id, exportIds: [...this.state.exportIds, id] } });
    } else {
      const v = validateReportBuildAccepted(value);
      if (v.run_id !== this.state.runId) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'report', epoch: this.state.epoch, value: { ...v, report_hash: null, error: null } });
    }
  }
  private async send(intent: WriteIntent) {
    const epoch = this.state.epoch;
    this.pending = { ...intent, state: 'unknown' };
    this.persist(); // Durable uncertainty BEFORE any network side effect.
    try {
      const value = await this.api.write({ ...intent, state: 'prepared' }, this.validator(intent.path));
      if (this.disposed || epoch !== this.state.epoch) return;
      this.accept(intent.path, value);
      this.pending = null;
      this.persist();
    } catch (error) {
      if (this.disposed || epoch !== this.state.epoch) throw error;
      // Only explicit client rejection proves there was no accepted write.
      const code = status(error);
      if (code && code >= 400 && code < 500 && !(error as { unknown?: boolean }).unknown) this.pending = null;
      else this.pending = { ...intent, state: 'unknown' };
      this.persist();
      this.dispatch({ type: 'error', message: this.pending ? 'WRITE_UNCONFIRMED' : 'WRITE_REJECTED' });
      throw error;
    }
  }
  private async write(path: string, body: unknown) {
    if (this.pending) {
      await this.checkPending();
      throw Error('WRITE_UNCONFIRMED_RECHECK_STATE');
    }
    await this.send(prepareIntent(path, body));
  }
  private async command(action: () => Promise<void>) {
    if (this.disposed) throw Error('DISPOSED');
    if (this.busy) throw Error('COMMAND_IN_PROGRESS');
    this.busy = true;
    try { await action(); } finally { this.busy = false; }
  }
  async checkPending() {
    const intent = this.pending;
    if (!intent) return true;
    let receipt;
    try { receipt = await this.api.receipt(intent.key, this.reads.signal); }
    catch (error) { if (status(error) === 404) return false; throw error; }
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(intent.body))), b => b.toString(16).padStart(2, '0')).join('');
    if (receipt.target_path !== `/api/v1${intent.path}` || receipt.request_sha256 !== hash) throw Error('RECEIPT_MISMATCH');
    if (receipt.state !== 'completed') throw Error('WRITE_PENDING');
    if (this.disposed || this.pending !== intent) return false;
    this.accept(intent.path, this.validator(intent.path)(receipt.response));
    this.pending = null;
    this.persist();
    return true;
  }
  async replayPending() {
    return this.command(async () => {
      if (!this.pending || await this.checkPending()) return;
      await this.send(this.pending);
    });
  }
  async startExample(consent = false) {
    if (!consent) throw Error('SOURCE_PROCESSING_CONSENT_REQUIRED');
    return this.command(async () => {
      if (this.state.projectId) return;
      await this.write(P.bootstrap, { fixture_id: 'offline-smoke-v1', source_processing_consent: true });
    });
  }
  async refresh() {
    const epoch = this.state.epoch;
    const signal = this.reads.signal;
    await this.api.read(P.demo, v => {
      if (object(v).actor_id !== this.actor) throw Error('ACTOR_MISMATCH');
      return v;
    }, signal);
    if (this.disposed || epoch !== this.state.epoch) return;
    if (this.pending) await this.checkPending();
    if (this.state.runId) { await this.pollRun(); await this.pollReport(); }
  }
  reviewExtraction(extractionId = this.state.extractionId ?? '') { this.dispatch({ type: 'review', extractionId }); this.persist(); }
  viewStep(step: GuidedState['viewStep']) { this.dispatch({ type: 'view', step }); }
  editExperiment() {
    if (this.busy || this.pending) throw Error('WRITE_UNCONFIRMED');
    this.reads.abort(); this.reads = new AbortController();
    this.dispatch({ type: 'edit' }); this.persist();
  }
  async confirmExperiment(acceptBudget = false) {
    if (!this.state.projectId || !this.state.reviewedExtractionId) throw Error('EXTRACTION_REVIEW_REQUIRED');
    if (!acceptBudget) throw Error('BUDGET_CONSENT_REQUIRED');
    return this.command(async () => {
      const epoch = this.state.epoch;
      const current = () => { if (this.disposed || epoch !== this.state.epoch) throw Error('STALE_COMMAND'); };
      if (!this.state.experimentId) await this.write(P.experiments(this.state.projectId!), { paper_id: this.state.paperId, extraction_id: this.state.reviewedExtractionId, candidate_experiment_id: 'offline-smoke', recipe_id: 'offline_fixture', recipe_version: '1' });
      current();
      if (!this.state.revisionId) {
        const draft = await this.api.read(P.draft(this.state.experimentId!), v => {
          const d = object(v);
          if (d.experiment_id !== this.state.experimentId || (d.base_revision_id !== null && typeof d.base_revision_id !== 'string')) throw Error('INVALID_DRAFT');
          object(d.spec);
          if (!Array.isArray(d.issues) || d.issues.some(i => object(i).severity === 'blocking')) throw Error('BLOCKING_ISSUES');
          return d;
        }, this.reads.signal);
        current();
        await this.write(`/projects/${encodeURIComponent(this.state.projectId!)}/spec-revisions`, { experiment_id: this.state.experimentId, base_revision_id: draft.base_revision_id, spec: draft.spec });
      }
      current();
      if (!this.state.confirmationId) {
        const revision = await this.api.read(P.revision(this.state.revisionId!), validateRevision, this.reads.signal);
        current();
        if (revision.id !== this.state.revisionId || revision.experiment_id !== this.state.experimentId) throw Error('RESOURCE_MISMATCH');
        await this.write(P.confirm(revision.id), { spec_hash: revision.spec_hash, policy_hash: revision.policy_hash, accept_budget: true });
      }
      current(); this.viewStep('run');
    });
  }
  async startRun() {
    if (!this.state.projectId || !this.state.revisionId || !this.state.confirmationId) throw Error('CONFIRMATION_REQUIRED');
    return this.command(async () => {
      if (!this.state.runId) await this.write(P.runs(this.state.projectId!), { spec_revision_id: this.state.revisionId, confirmation_id: this.state.confirmationId });
      if (!this.disposed) await this.pollRun();
    });
  }
  async retryReport() {
    if (this.state.run?.status !== 'completed' || !this.state.runId) throw Error('COMPLETED_RUN_REQUIRED');
    return this.command(async () => {
      await this.pollReport();
      const s = this.state.report;
      if (!s || (s.status !== null && s.status !== 'failed')) return;
      await this.write(P.reportBuilds(this.state.runId!), s.status === 'failed' ? { retry_of: s.build_id } : {});
    });
  }
  async createExports(formats: string[] = ['report_json', 'report_markdown', 'bundle_zip']) {
    if (!this.state.runId || this.state.reportStatus !== 'ready') throw Error('REPORT_NOT_READY');
    return this.command(async () => { await this.write(P.exports(this.state.runId!), { formats, include_checkpoint: false }); });
  }
  async pollRun() {
    const { runId, epoch } = this.state;
    if (!runId) return;
    const run = await this.api.read(P.run(runId), validateRun, this.reads.signal);
    if (run.run_id !== runId || run.project_id !== this.state.projectId || run.experiment_id !== this.state.experimentId || run.revision_id !== this.state.revisionId) throw Error('RESOURCE_MISMATCH');
    this.dispatch({ type: 'run', epoch, value: run });
    if (this.disposed || epoch !== this.state.epoch) return;
    const after = Math.max(0, ...this.state.events.map(e => e.seq));
    const events = await this.api.runEvents(runId, after, this.reads.signal);
    this.dispatch({ type: 'events', epoch, value: events });
  }
  async pollReport() {
    const { runId, epoch } = this.state;
    if (!runId) return;
    const report = await this.api.read(P.reportStatus(runId), validateReportStatus, this.reads.signal);
    this.dispatch({ type: 'report', epoch, value: report });
  }
  dispose() { this.disposed = true; this.reads.abort(); this.listeners.clear(); }
}
