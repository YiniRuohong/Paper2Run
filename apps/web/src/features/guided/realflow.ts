import { GUIDED_PATHS as P, GuidedApiError, validateBootstrap, validateConfirmation, validateExperiment, validateExportAccepted, validateReportBuildAccepted, validateReportStatus, validateRevision, validateRun, validateRunAccepted } from '../../api-client/guided.ts';
import type { GuidedApi } from '../../api-client/guided.ts';
import type { DocParseMode, DocParseStatus, GuidedCandidateExperiment, GuidedSSHAuthInput, PaperText } from '../../api-client/guided-types.ts';
import { canExtract, canParseNow, isParsePending } from '../docparse/docparse.ts';
import { applyTrainingToSpec, candidateBlockedReason, createRealFlowState, findCandidate, isRetryableRunStatus, isSandboxRecipeSelected, parseTrainingForm, realFlowReducer, type RealFlowState, type RealFlowStep } from './realflow-state.ts';

// Controller for the real article flow (paper upload → provider session →
// extraction → spec → environment → run). No storage option exists on
// purpose: nothing about this flow is persisted client-side, so the provider
// api_key lives only in React state and must be re-typed after a refresh.
//
// Idempotency: every write carries an Idempotency-Key. The key is held in
// memory per logical submission ("slot"); if a write ends with an UNKNOWN
// outcome the key + command are kept so an explicit retry replays the exact
// same key and body. A rejected (4xx) or successful submission clears the
// slot, so the next explicit submission mints a fresh key.

// Docparse polling cadence: paper text while parsing, installer status while
// the offline component installs. Overridable per controller for tests.
export const DOCPARSE_TEXT_POLL_MS = 2000;
export const DOCPARSE_STATUS_POLL_MS = 3000;

const PDF_MAX_BYTES = 64 * 1024 * 1024;
const TEXT_MAX_BYTES = 2 * 1024 * 1024;
const object = (v: unknown): Record<string, unknown> => {
  if (!v || typeof v !== 'object' || Array.isArray(v)) throw Error('INVALID_DTO');
  return v as Record<string, unknown>;
};
const status = (e: unknown) => e && typeof e === 'object' ? (e as { status?: number }).status : undefined;
const isUnknownOutcome = (e: unknown): boolean => {
  if (e instanceof GuidedApiError) return e.unknown;
  return !!e && typeof e === 'object' && (e as { unknown?: boolean }).unknown === true;
};
const messageOf = (e: unknown): string => {
  if (e instanceof GuidedApiError) return `${e.code}: ${e.message}`;
  const code = e && typeof e === 'object' ? (e as { code?: unknown }).code : undefined;
  const message = e instanceof Error ? e.message : '操作未完成';
  return typeof code === 'string' && code ? `${code}: ${message}` : message;
};

export interface RealFlowUploadInput {
  consent: boolean;
  file?: File;
  text?: string;
  titleHint?: string;
}

export interface RealFlowProviderForm {
  baseUrl: string;
  modelId: string;
  apiKey: string;
  endpointAlias?: string;
}

export interface RealFlowSshForm {
  host: string;
  port: string;
  user: string;
  workdir: string;
  authType: 'agent' | 'key_reference';
  agentId?: string;
  keyReference?: string;
}

export class RealFlowController {
  private state: RealFlowState = createRealFlowState();
  private listeners = new Set<() => void>();
  private disposed = false;
  private busy = false;
  private api: GuidedApi;
  private retry: { slot: string; key: string; perform: (key: string) => Promise<void> } | null = null;
  private reads = new AbortController();
  // One docparse flow at a time: a new upload, mode choice or retry aborts the
  // previous flow's reads and timers; dispose() aborts it too. `driving` marks
  // a flow whose background poll loop is still running.
  // `settling` is true from creation until the starter (upload, mode choice,
  // retry, or a refresh that began the flow) leaves its setup reads/writes,
  // whether it succeeded or failed; only then may pollPaperText drive the flow.
  private docParseFlow: { abort: AbortController; driving: boolean; settling: boolean } | null = null;
  private docParsePoll: { text: number; status: number };
  constructor(options: { api: GuidedApi; docParsePollMs?: { text?: number; status?: number } }) {
    this.api = options.api;
    this.docParsePoll = {
      text: options.docParsePollMs?.text ?? DOCPARSE_TEXT_POLL_MS,
      status: options.docParsePollMs?.status ?? DOCPARSE_STATUS_POLL_MS,
    };
  }
  getSnapshot = (): RealFlowState => this.state;
  getState = (): RealFlowState => this.state;
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  private dispatch(action: Parameters<typeof realFlowReducer>[1]) {
    if (this.disposed) return;
    this.state = realFlowReducer(this.state, action);
    this.listeners.forEach(listener => listener());
  }
  private async submit(slot: string, perform: (key: string) => Promise<void>): Promise<void> {
    if (this.retry) throw Error('WRITE_UNCONFIRMED_RECHECK_STATE');
    await this.submitWithKey(slot, crypto.randomUUID(), perform);
  }
  private async submitWithKey(slot: string, key: string, perform: (key: string) => Promise<void>): Promise<void> {
    if (this.disposed) throw Error('DISPOSED');
    if (this.busy) throw Error('COMMAND_IN_PROGRESS');
    this.busy = true;
    this.dispatch({ type: 'busy', value: true });
    try {
      await perform(key);
    } catch (error) {
      if (this.disposed) throw error;
      if (isUnknownOutcome(error)) {
        this.retry = { slot, key, perform };
        this.dispatch({ type: 'error', message: 'WRITE_UNCONFIRMED' });
      } else {
        this.dispatch({ type: 'error', message: messageOf(error) });
      }
      throw error;
    } finally {
      this.busy = false;
      if (!this.disposed) this.dispatch({ type: 'busy', value: false });
    }
  }
  hasRetry(): boolean { return this.retry !== null; }
  // Commands whose only side effect before submit() is a state phase flip must
  // check this first: a blocked command must not destroy current readbacks.
  private requireNoPendingRetry(): void {
    if (this.retry) throw Error('WRITE_UNCONFIRMED_RECHECK_STATE');
  }
  async retryWrite(): Promise<void> {
    const pending = this.retry;
    if (!pending) throw Error('NO_UNCONFIRMED_WRITE');
    this.retry = null;
    await this.submitWithKey(pending.slot, pending.key, pending.perform);
  }
  viewStep(step: RealFlowStep) { this.dispatch({ type: 'view', step }); }

  // --- 1) article input ----------------------------------------------------

  private async ensureProject(key: string): Promise<string> {
    if (this.state.projectId) return this.state.projectId;
    try {
      const project = await this.api.createProject('真实文章复现', key);
      this.dispatch({ type: 'project', projectId: project.id });
      return project.id;
    } catch (error) {
      // The demo runtime provisions its user lazily on first bootstrap. A 403
      // on project creation means nobody ever bootstrapped: provision once
      // through the same fixture endpoint, then retry with a fresh key (the
      // rejected key must never be reused for a different body).
      if (status(error) !== 403) throw error;
      await this.api.write({ key: crypto.randomUUID(), path: P.bootstrap, body: JSON.stringify({ fixture_id: 'offline-smoke-v1', source_processing_consent: true }), state: 'prepared' }, validateBootstrap);
      this.dispatch({ type: 'note', message: '本地用户未初始化：已自动执行一次离线 bootstrap 完成初始化（仅创建教学 fixture，不涉及你的文章）。' });
      const project = await this.api.createProject('真实文章复现');
      this.dispatch({ type: 'project', projectId: project.id });
      return project.id;
    }
  }

  async uploadArticle(input: RealFlowUploadInput): Promise<void> {
    if (!input.consent) throw Error('SOURCE_PROCESSING_CONSENT_REQUIRED');
    const hasFile = input.file !== undefined;
    const hasText = input.text !== undefined && input.text.trim().length > 0;
    if (hasFile === hasText) throw Error('REALFLOW_ONE_SOURCE_REQUIRED');
    if (hasFile && !/\.(pdf|txt|json)$/i.test(input.file!.name)) throw Error('REALFLOW_UNSUPPORTED_FILE_TYPE');
    if (hasFile && input.file!.size > PDF_MAX_BYTES) throw Error('REALFLOW_FILE_TOO_LARGE');
    if (hasText && new TextEncoder().encode(input.text!).byteLength > TEXT_MAX_BYTES) throw Error('REALFLOW_TEXT_TOO_LARGE');
    let fileText: string | null = null;
    if (hasFile && !/\.pdf$/i.test(input.file!.name)) fileText = await input.file!.text();
    if (fileText !== null && fileText.trim().length === 0) throw Error('REALFLOW_EMPTY_FILE');
    const isPdf = hasFile && fileText === null;
    return this.submit('paper', async key => {
      // Inside the write so a blocked upload keeps the current paper's polling.
      this.stopDocParseFlow();
      const projectId = await this.ensureProject(key);
      const accepted = isPdf
        ? await this.api.uploadPaperPdf(projectId, input.file!, { titleHint: input.titleHint, key })
        : await this.api.createTextPaper(projectId, { text: fileText ?? input.text!, titleHint: input.titleHint, key });
      if (accepted.paper_id === '') throw Error('INVALID_DTO');
      const paper = await this.api.getPaper(accepted.paper_id, this.reads.signal);
      if (paper.paper_id !== accepted.paper_id || (this.state.projectId !== null && paper.project_id !== this.state.projectId)) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'paper', value: paper, pdf: isPdf });
      if (isPdf) await this.startPdfParse(paper.paper_id);
    });
  }

  // --- 1b) PDF parsing (MinerU) ------------------------------------------
  // Controller rulings: never pick a mode for the user, never install the
  // offline component without an explicit choice, never switch between online
  // and offline on a failure. The server decides what each parse does.

  private pdfPaperId(): string | null {
    return this.state.docparse.pdf && this.state.paper ? this.state.paper.paper_id : null;
  }
  private beginDocParseFlow(): AbortSignal {
    this.docParseFlow?.abort.abort();
    this.docParseFlow = { abort: new AbortController(), driving: false, settling: true };
    return this.docParseFlow.abort.signal;
  }
  // Called from the `finally` of every starter, so a failed setup never leaves
  // a live flow stuck in `settling`.
  private endSetup(signal: AbortSignal): void {
    const flow = this.docParseFlow;
    if (flow && flow.abort.signal === signal) flow.settling = false;
  }
  private stopDocParseFlow(): void {
    this.docParseFlow?.abort.abort();
    this.docParseFlow = null;
  }
  private flowLive(signal: AbortSignal): boolean { return !this.disposed && !signal.aborted; }
  private wait(ms: number, signal: AbortSignal): Promise<boolean> {
    return new Promise(resolve => {
      if (signal.aborted) { resolve(false); return; }
      const onAbort = () => { clearTimeout(timer); resolve(false); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(true); }, ms);
      signal.addEventListener('abort', onAbort, { once: true });
    });
  }
  private async readDocParseStatus(signal: AbortSignal): Promise<DocParseStatus | null> {
    const status = await this.api.getDocParseStatus(signal);
    if (!this.flowLive(signal)) return null;
    this.dispatch({ type: 'docparse-status', value: status });
    return status;
  }
  private async readPaperText(paperId: string, signal: AbortSignal): Promise<PaperText | null> {
    const text = await this.api.getPaperText(paperId, signal);
    if (!this.flowLive(signal)) return null;
    this.dispatch({ type: 'docparse-text', paperId, value: text });
    return text;
  }
  private async requestParse(paperId: string, key: string, signal: AbortSignal): Promise<void> {
    await this.api.parsePaper(paperId, key);
    await this.readPaperText(paperId, signal);
  }

  // Right after a PDF upload: read the global mode and this paper's text, then
  // take at most one step. Errors surface as the flow error but never undo the
  // accepted upload.
  private async startPdfParse(paperId: string): Promise<void> {
    const signal = this.beginDocParseFlow();
    try {
      const status = await this.readDocParseStatus(signal);
      const text = status && await this.readPaperText(paperId, signal);
      if (!status || !text) return;
      let waitFor: DocParseMode | null = null;
      if (isParsePending(text)) {
        if (status.mode === null) this.dispatch({ type: 'docparse-needs-choice', value: true });
        else if (canParseNow(status)) await this.requestParse(paperId, crypto.randomUUID(), signal);
        else if (status.offline.state === 'installing') waitFor = status.mode;
        // offline not_installed/failed/broken/unsupported: left to the UI.
      }
      this.driveDocParse(paperId, signal, waitFor);
    } catch (error) {
      if (this.flowLive(signal)) this.dispatch({ type: 'error', message: messageOf(error) });
    } finally {
      this.endSetup(signal);
    }
  }

  // Background loop of one flow: polls the text while parsing and the
  // installer while installing, both until a terminal state. Started with
  // `waitFor`, it requests the parse exactly once when that same mode becomes
  // usable. It never installs and never changes the mode.
  private driveDocParse(paperId: string | null, signal: AbortSignal, waitFor: DocParseMode | null): void {
    const flow = this.docParseFlow;
    if (!flow || flow.abort.signal !== signal || !this.flowLive(signal)) return;
    flow.driving = true;
    void this.docParseLoop(paperId, signal, waitFor)
      .catch(error => { if (this.flowLive(signal)) this.dispatch({ type: 'error', message: messageOf(error) }); })
      .finally(() => { flow.driving = false; });
  }
  private async docParseLoop(paperId: string | null, signal: AbortSignal, waitFor: DocParseMode | null): Promise<void> {
    let pendingParse = waitFor;
    while (this.flowLive(signal)) {
      const { status, text } = this.state.docparse;
      if (paperId !== null && text?.status === 'parsing') {
        if (!await this.wait(this.docParsePoll.text, signal)) return;
        const next = await this.readPaperText(paperId, signal);
        // A finished failure may stem from the installer: refresh it once for the label.
        if (next?.status === 'failed') await this.readDocParseStatus(signal);
        continue;
      }
      if (status?.mode === 'offline' && status.offline.state === 'installing') {
        if (!await this.wait(this.docParsePoll.status, signal)) return;
        await this.readDocParseStatus(signal);
        continue;
      }
      if (pendingParse !== null && paperId !== null && status !== null && canParseNow(status, pendingParse) && text?.status !== 'ready') {
        pendingParse = null;
        await this.requestParse(paperId, crypto.randomUUID(), signal);
        continue;
      }
      return;
    }
  }

  // Explicit user choice of the parse backend. Offline starts the installer
  // unless it is already ready/installing; the parse is requested as soon as
  // the chosen backend is usable (online: now; offline: once ready).
  async chooseParseMode(mode: DocParseMode): Promise<void> {
    if (mode !== 'online' && mode !== 'offline') throw Error('DOCPARSE_MODE_INVALID');
    return this.submit('docparse-mode', async key => {
      const signal = this.beginDocParseFlow();
      try {
        let status = await this.api.setDocParseMode(mode);
        this.dispatch({ type: 'docparse-status', value: status });
        if (mode === 'offline' && status.offline.state !== 'ready' && status.offline.state !== 'installing') {
          status = await this.api.startOfflineInstall();
          this.dispatch({ type: 'docparse-status', value: status });
        }
        const paperId = this.pdfPaperId();
        const text = this.state.docparse.text;
        const wantParse = paperId !== null && text?.status !== 'parsing' && text?.status !== 'ready';
        let waitFor: DocParseMode | null = null;
        if (wantParse && canParseNow(status, mode)) await this.requestParse(paperId, key, signal);
        else if (wantParse) waitFor = mode;
        this.driveDocParse(paperId, signal, waitFor);
      } finally {
        this.endSetup(signal);
      }
    });
  }

  // Only ever called from an explicit user click: crossing between online and
  // offline is a user decision, never an automatic fallback.
  async switchParseMode(mode: DocParseMode): Promise<void> {
    return this.chooseParseMode(mode);
  }

  // A new parse request (fresh Idempotency-Key) under whatever mode is set.
  async retryParse(): Promise<void> {
    const paperId = this.pdfPaperId();
    if (!paperId) throw Error('REALFLOW_PDF_PAPER_REQUIRED');
    return this.submit('docparse-retry', async key => {
      const signal = this.beginDocParseFlow();
      try {
        await this.requestParse(paperId, key, signal);
        this.driveDocParse(paperId, signal, null);
      } finally {
        this.endSetup(signal);
      }
    });
  }

  // Read-only refresh of the current PDF's text. A live flow is never replaced
  // or aborted: the read uses that flow's signal. A flow still settling (its
  // starter is mid upload, mode choice or retry) will start its own loop, so it
  // is left alone; once settling has ended, successfully or not, a flow whose
  // loop is not running gets polling restarted. Only with no live flow does a
  // refresh begin one, and it ends its own setup even when the read fails.
  async pollPaperText(): Promise<void> {
    const paperId = this.pdfPaperId();
    if (!paperId) return;
    const live = this.docParseFlow && !this.docParseFlow.abort.signal.aborted ? this.docParseFlow : null;
    if (live) {
      const signal = live.abort.signal;
      await this.readPaperText(paperId, signal);
      if (!live.settling && !live.driving) this.driveDocParse(paperId, signal, null);
      return;
    }
    const signal = this.beginDocParseFlow();
    try {
      await this.readPaperText(paperId, signal);
    } finally {
      this.endSetup(signal);
    }
    this.driveDocParse(paperId, signal, null);
  }

  // --- 2) provider session --------------------------------------------------

  async createSession(form: RealFlowProviderForm): Promise<void> {
    const baseUrl = form.baseUrl.trim();
    const modelId = form.modelId.trim();
    const apiKey = form.apiKey.trim();
    const endpointAlias = (form.endpointAlias ?? '').trim() || deriveEndpointAlias(baseUrl);
    if (!baseUrl || !modelId || !apiKey) throw Error('REALFLOW_PROVIDER_FORM_INCOMPLETE');
    return this.submit('provider', async key => {
      const summary = await this.api.createProviderSession({ base_url: baseUrl, model_id: modelId, api_key: apiKey, endpoint_alias: endpointAlias, key });
      this.dispatch({
        type: 'provider',
        value: {
          providerSessionId: summary.provider_session_id,
          endpointAlias: summary.endpoint_alias,
          modelId: summary.model_id,
          baseUrl: summary.base_url,
          secretLast4: summary.secret_last4,
        },
      });
    });
  }

  async clearSession(): Promise<void> {
    const provider = this.state.provider;
    if (!provider) throw Error('REALFLOW_NO_PROVIDER_SESSION');
    return this.submit('provider-clear', async key => {
      await this.api.deleteProviderSession(provider.providerSessionId, key);
      this.dispatch({ type: 'provider-clear' });
    });
  }

  // --- 3) extraction ----------------------------------------------------

  async runExtraction(reason = 'guided-real-flow-article-review'): Promise<void> {
    const paper = this.state.paper;
    if (!paper) throw Error('REALFLOW_PAPER_REQUIRED');
    // A PDF is extracted from its parsed text only; the server answers the same
    // PAPER_TEXT_NOT_READY, this fails fast before any write.
    if (!canExtract(this.state)) throw Error('PAPER_TEXT_NOT_READY');
    const providerSessionId = this.state.provider?.providerSessionId ?? null;
    this.requireNoPendingRetry();
    this.dispatch({ type: 'extraction-phase', phase: 'loading' });
    try {
      await this.submit('extraction', async key => {
        const accepted = await this.api.createExtraction(paper.paper_id, { reason, provider_session_id: providerSessionId, key });
        if (accepted.paper_id !== paper.paper_id) throw Error('RESOURCE_MISMATCH');
        if (accepted.extraction_id === null) {
          this.dispatch({ type: 'extraction-phase', phase: 'error' });
          this.dispatch({ type: 'error', message: `EXTRACTION_NOT_READY: ${accepted.status}` });
          return;
        }
        const extraction = await this.api.getExtraction(paper.paper_id, this.reads.signal);
        if (extraction === null || extraction.extraction_id === null) {
          this.dispatch({ type: 'extraction-phase', phase: 'error' });
          this.dispatch({ type: 'error', message: 'EXTRACTION_NOT_READY: 抽取尚未可读，请显式重试。' });
          return;
        }
        this.dispatch({ type: 'extraction', value: extraction });
      });
    } catch (error) {
      if (this.state.extractionPhase === 'loading') this.dispatch({ type: 'extraction-phase', phase: 'error' });
      throw error;
    }
  }

  // --- 4) candidate selection + experiment/revision/confirm ----------------

  selectCandidate(candidateId: string): void {
    this.dispatch({ type: 'select', candidateId });
  }
  setTraining(value: Partial<RealFlowState['training']>): void {
    this.dispatch({ type: 'training', value });
  }
  selectedCandidate(): GuidedCandidateExperiment | null {
    return findCandidate(this.state.extraction, this.state.selectedCandidateId);
  }
  async confirmSpec(acceptBudget: boolean): Promise<void> {
    const s = this.state;
    if (!s.paper || s.extractionPhase !== 'ready' || !s.extraction) throw Error('EXTRACTION_NOT_READY');
    if (!s.selectedCandidateId) throw Error('CANDIDATE_SELECTION_REQUIRED');
    if (!acceptBudget) throw Error('BUDGET_CONSENT_REQUIRED');
    const candidate = this.selectedCandidate();
    if (!candidate) throw Error('CANDIDATE_NOT_FOUND');
    const blocked = candidateBlockedReason(candidate);
    if (blocked) throw Error(blocked);
    if (!candidate.id || !candidate.recipe_id || !candidate.recipe_version) throw Error('CANDIDATE_WITHOUT_RECIPE');
    if (s.extraction.extraction_id === null) throw Error('EXTRACTION_NOT_READY');
    parseTrainingForm(s.training); // Fail fast: refuse invalid numbers before any write.
    const projectId = s.projectId ?? s.paper.project_id;
    // One submission, three distinct writes: derive a stable per-write key so
    // a global (actor, key) receipt store never sees one key on two paths,
    // while a replay of the same submission still reuses the exact keys.
    return this.submit('spec', async key => {
      if (!this.state.experimentId) {
        const experiment = await this.api.write({ key: `${key}:experiment`, path: P.experiments(projectId), body: JSON.stringify({
          paper_id: this.state.paper!.paper_id,
          extraction_id: this.state.extraction!.extraction_id,
          candidate_experiment_id: candidate.id,
          recipe_id: candidate.recipe_id,
          recipe_version: candidate.recipe_version,
        }), state: 'prepared' }, validateExperiment);
        if (experiment.project_id !== projectId || experiment.paper_id !== this.state.paper!.paper_id) throw Error('RESOURCE_MISMATCH');
        this.dispatch({ type: 'refs', value: { experimentId: experiment.experiment_id } });
      }
      if (!this.state.revisionId) {
        const draftValue = await this.api.read(P.draft(this.state.experimentId!), value => {
          const d = object(value);
          if (d.experiment_id !== this.state.experimentId || (d.base_revision_id !== null && typeof d.base_revision_id !== 'string')) throw Error('INVALID_DRAFT');
          const spec = object(d.spec);
          if (!Array.isArray(d.issues) || d.issues.some(i => object(i).severity === 'blocking')) throw Error('BLOCKING_ISSUES');
          return { base_revision_id: d.base_revision_id as string | null, spec };
        }, this.reads.signal);
        const spec = applyTrainingToSpec(draftValue.spec, this.state.training);
        const revision = await this.api.write({ key: `${key}:revision`, path: `/projects/${encodeURIComponent(projectId)}/spec-revisions`, body: JSON.stringify({
          experiment_id: this.state.experimentId,
          base_revision_id: draftValue.base_revision_id,
          spec,
        }), state: 'prepared' }, validateRevision);
        if (revision.experiment_id !== this.state.experimentId) throw Error('RESOURCE_MISMATCH');
        this.dispatch({ type: 'refs', value: { revisionId: revision.id } });
      }
      if (!this.state.confirmationId) {
        const revision = await this.api.read(P.revision(this.state.revisionId!), validateRevision, this.reads.signal);
        if (revision.id !== this.state.revisionId || revision.experiment_id !== this.state.experimentId) throw Error('RESOURCE_MISMATCH');
        const confirmation = await this.api.write({ key: `${key}:confirm`, path: P.confirm(revision.id), body: JSON.stringify({
          spec_hash: revision.spec_hash,
          policy_hash: revision.policy_hash,
          accept_budget: true,
        }), state: 'prepared' }, validateConfirmation);
        if (confirmation.revision_id !== this.state.revisionId) throw Error('RESOURCE_MISMATCH');
        this.dispatch({ type: 'refs', value: { confirmationId: confirmation.confirmation_id } });
      }
      this.dispatch({ type: 'note', message: null });
      this.viewStep('environment');
    });
  }

  // --- 5) environment check + ssh verification --------------------------

  async checkEnvironment(): Promise<void> {
    const s = this.state;
    if (!s.projectId || !s.experimentId || !s.revisionId || !s.confirmationId) throw Error('SPEC_CONFIRMATION_REQUIRED');
    this.requireNoPendingRetry();
    this.dispatch({ type: 'env-phase', phase: 'loading' });
    try {
      await this.submit('environment', async key => {
        const check = await this.api.createEnvironmentCheck(s.projectId!, s.experimentId!, { revision_id: s.revisionId!, requirements: {}, key });
        this.dispatch({ type: 'env', value: check });
      });
    } catch (error) {
      if (this.state.envPhase === 'loading') this.dispatch({ type: 'env-phase', phase: 'error' });
      throw error;
    }
  }

  async verifySsh(form: RealFlowSshForm): Promise<void> {
    const env = this.state.envCheck;
    if (!env) throw Error('ENVIRONMENT_GATE_REQUIRED');
    if (env.decision !== 'blocked') throw Error('REALFLOW_SSH_NOT_AVAILABLE');
    const host = form.host.trim();
    const user = form.user.trim();
    const workdir = form.workdir.trim();
    const port = Number(form.port.trim() === '' ? '22' : form.port);
    if (!host || !user || !workdir || !Number.isSafeInteger(port) || port < 1 || port > 65535) throw Error('REALFLOW_SSH_FORM_INVALID');
    let auth: GuidedSSHAuthInput;
    if (form.authType === 'agent') {
      const agentId = (form.agentId ?? '').trim();
      if (!agentId) throw Error('REALFLOW_SSH_FORM_INVALID');
      auth = { type: 'agent', agent_id: agentId };
    } else {
      const keyReference = (form.keyReference ?? '').trim();
      if (!keyReference) throw Error('REALFLOW_SSH_FORM_INVALID');
      auth = { type: 'key_reference', key_reference: keyReference };
    }
    return this.submit('ssh', async key => {
      const verification = await this.api.createSshVerification(env.environment_check_id, { host, port, user, workdir, auth, key });
      this.dispatch({ type: 'ssh', value: verification });
    });
  }

  // --- 5b) sandbox code generation (recipe sandbox_python) ------------------
  // POST /experiments/{eid}/codegen with {provider_session_id, reason}. Every
  // fresh submission mints a fresh Idempotency-Key, so「重新生成」is a genuinely
  // new generation; only an unknown outcome keeps the key for an exact replay.

  async generateCode(reason = 'guided-real-flow-codegen'): Promise<void> {
    const s = this.state;
    if (!s.experimentId) throw Error('SPEC_CONFIRMATION_REQUIRED');
    const providerSessionId = s.provider?.providerSessionId ?? null;
    this.requireNoPendingRetry();
    this.dispatch({ type: 'codegen-phase', phase: 'loading' });
    try {
      await this.submit('codegen', async key => {
        const accepted = await this.api.createCodegen(s.experimentId!, { provider_session_id: providerSessionId, reason, key });
        this.dispatch({ type: 'codegen', value: {
          attemptId: accepted.attempt_id,
          attemptIndex: accepted.attempt_index,
          code: accepted.code,
          codeSha256: accepted.code_sha256,
          notes: accepted.notes,
        } });
      });
    } catch (error) {
      if (this.state.codegenPhase === 'loading') this.dispatch({ type: 'codegen-phase', phase: 'error' });
      throw error;
    }
  }

  // Read-only readback of the stored attempt code. A hash mismatch means the
  // server state disagrees with what we generated: fail visibly and keep the
  // generated copy instead of silently swapping the code.
  async readCode(): Promise<void> {
    const s = this.state;
    if (!s.experimentId || !s.codegen) throw Error('SANDBOX_CODE_NOT_GENERATED');
    const value = await this.api.readAttemptCode(s.experimentId, s.codegen.attemptIndex, this.reads.signal);
    if (value.code_sha256 !== s.codegen.codeSha256) throw Error('CODE_READBACK_MISMATCH');
    this.dispatch({ type: 'codegen', value: { ...s.codegen, code: value.code, codeSha256: value.code_sha256, notes: value.notes } });
  }

  // --- 6) run / report / export -------------------------------------------

  async startRun(mode: 'local-cpu' | 'ssh'): Promise<void> {
    const s = this.state;
    if (!s.projectId || !s.revisionId || !s.confirmationId) throw Error('SPEC_CONFIRMATION_REQUIRED');
    if (mode === 'local-cpu' && s.envCheck?.decision !== 'ready') throw Error('ENVIRONMENT_BLOCKED');
    if (mode === 'ssh' && s.ssh === null) throw Error('SSH_VERIFICATION_REQUIRED');
    // Sandbox recipes execute model-generated code: refuse the Run write until
    // code actually exists (the server enforces the same gate; this fails fast
    // before any write). Non-sandbox recipes are untouched.
    if (isSandboxRecipeSelected(s) && s.codegen === null) throw Error('SANDBOX_CODE_REQUIRED');
    const body: Record<string, unknown> = { spec_revision_id: s.revisionId, confirmation_id: s.confirmationId };
    if (mode === 'ssh') body.execution = { kind: 'ssh', ssh_verification_id: s.ssh!.ssh_verification_id, confirmed: true };
    if (isRetryableRunStatus(s.run?.status)) {
      this.requireNoPendingRetry();
      this.dispatch({ type: 'run-reset' });
    }
    return this.submit('run', async key => {
      if (!this.state.runId) {
        const accepted = await this.api.write({ key, path: P.runs(s.projectId!), body: JSON.stringify(mode === 'ssh' ? { ...body, mode: 'ssh' } : body), state: 'prepared' }, validateRunAccepted);
        this.dispatch({ type: 'refs', value: { runId: accepted.run_id } });
      }
      await this.pollRun();
    });
  }

  async pollRun(): Promise<void> {
    const { runId, projectId, experimentId, revisionId } = this.state;
    if (!runId) return;
    const run = await this.api.read(P.run(runId), validateRun, this.reads.signal);
    if (run.run_id !== runId || run.project_id !== projectId || run.experiment_id !== experimentId || run.revision_id !== revisionId) throw Error('RESOURCE_MISMATCH');
    this.dispatch({ type: 'run', value: run });
    if (this.disposed) return;
    const after = Math.max(0, ...this.state.events.map(e => e.seq));
    const events = await this.api.runEvents(runId, after, this.reads.signal);
    this.dispatch({ type: 'events', value: events });
  }

  async pollReport(): Promise<void> {
    const { runId } = this.state;
    if (!runId) return;
    const report = await this.api.read(P.reportStatus(runId), validateReportStatus, this.reads.signal);
    this.dispatch({ type: 'report', value: report });
  }

  async buildReport(): Promise<void> {
    if (this.state.run?.status !== 'completed' || !this.state.runId) throw Error('COMPLETED_RUN_REQUIRED');
    return this.submit('report', async key => {
      await this.pollReport();
      const s = this.state;
      if (!s.report || (s.report.status !== null && s.report.status !== 'failed')) return;
      const value = await this.api.write({ key, path: P.reportBuilds(s.runId!), body: JSON.stringify(s.report.status === 'failed' && s.report.build_id ? { retry_of: s.report.build_id } : {}), state: 'prepared' }, validateReportBuildAccepted);
      if (value.run_id !== s.runId) throw Error('RESOURCE_MISMATCH');
      this.dispatch({ type: 'report', value: { ...value, report_hash: null, error: null } });
    });
  }

  async createExports(formats: string[] = ['report_json', 'report_markdown', 'bundle_zip']): Promise<void> {
    if (!this.state.runId || this.state.reportStatus !== 'ready') throw Error('REPORT_NOT_READY');
    return this.submit('export', async key => {
      const accepted = await this.api.write({ key, path: P.exports(this.state.runId!), body: JSON.stringify({ formats, include_checkpoint: false }), state: 'prepared' }, validateExportAccepted);
      this.dispatch({ type: 'export', exportId: accepted.export_id });
    });
  }

  dispose(): void { this.disposed = true; this.reads.abort(); this.stopDocParseFlow(); this.listeners.clear(); }
}

function deriveEndpointAlias(baseUrl: string): string {
  try {
    const host = new URL(baseUrl).hostname;
    return host || 'guided-endpoint';
  } catch {
    return 'guided-endpoint';
  }
}
