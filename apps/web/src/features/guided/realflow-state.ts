import type {
  DocParseStatus,
  GuidedCandidateExperiment,
  GuidedEnvironmentCheck,
  GuidedExtractionReadback,
  GuidedPaperInfo,
  GuidedReportStatus,
  GuidedRun,
  GuidedRunEvent,
  GuidedSSHVerification,
  GuidedTrainingForm,
  PaperText,
} from '../../api-client/guided-types.ts';

// Real article flow state. Deliberately in-memory only: nothing here is
// persisted, so a refresh loses the provider session (api_key must be
// re-typed) and the unsaved flow position — no new localStorage key exists.

export type RealFlowStep = 'input' | 'provider' | 'extract' | 'spec' | 'environment' | 'codegen' | 'execute';
export type RealFlowPhase = 'idle' | 'loading' | 'error' | 'ready';

// The only recipe that runs model-generated code in the local sandbox. For
// this recipe the flow gains a「生成复现代码」step and Run creation is gated
// on generated code existing; every other recipe keeps the original flow.
export const SANDBOX_PYTHON_RECIPE = 'sandbox_python';

export interface RealFlowProviderSession {
  providerSessionId: string;
  endpointAlias: string;
  modelId: string;
  baseUrl: string;
  secretLast4: string;
}

// The latest generated reproduction code for the selected experiment (recipe
// sandbox_python). Read-only display material: the code is shown verbatim and
// is never executed client-side.
export interface RealFlowCodegen {
  attemptId: string;
  attemptIndex: number;
  code: string;
  codeSha256: string;
  notes: string | null;
}

// MinerU PDF parsing for the current paper. status is the global parse setting
// (mode + offline installer) and survives a new upload; pdf/needsChoice/text
// belong to the current paper. needsChoice is only set when a PDF waits for
// the user to pick online/offline — the controller never picks for them.
export interface RealFlowDocParse {
  pdf: boolean;
  needsChoice: boolean;
  status: DocParseStatus | null;
  text: PaperText | null;
}

export interface RealFlowState {
  step: RealFlowStep;
  error: string | null;
  busy: boolean;
  note: string | null;
  projectId: string | null;
  paper: GuidedPaperInfo | null;
  provider: RealFlowProviderSession | null;
  extractionPhase: RealFlowPhase;
  extraction: GuidedExtractionReadback | null;
  selectedCandidateId: string | null;
  training: GuidedTrainingForm;
  experimentId: string | null;
  revisionId: string | null;
  confirmationId: string | null;
  envPhase: RealFlowPhase;
  envCheck: GuidedEnvironmentCheck | null;
  ssh: GuidedSSHVerification | null;
  codegenPhase: RealFlowPhase;
  codegen: RealFlowCodegen | null;
  runId: string | null;
  run: GuidedRun | null;
  report: GuidedReportStatus | null;
  reportStatus: GuidedReportStatus['status'] | null;
  exportId: string | null;
  events: GuidedRunEvent[];
  docparse: RealFlowDocParse;
}

export type RealFlowAction =
  | { type: 'busy'; value: boolean }
  | { type: 'error'; message: string | null }
  | { type: 'note'; message: string | null }
  | { type: 'view'; step: RealFlowStep }
  | { type: 'project'; projectId: string }
  | { type: 'paper'; value: GuidedPaperInfo; pdf?: boolean }
  | { type: 'docparse-status'; value: DocParseStatus }
  | { type: 'docparse-text'; paperId: string; value: PaperText }
  | { type: 'docparse-needs-choice'; value: boolean }
  | { type: 'provider'; value: RealFlowProviderSession }
  | { type: 'provider-clear' }
  | { type: 'extraction-phase'; phase: RealFlowPhase }
  | { type: 'extraction'; value: GuidedExtractionReadback }
  | { type: 'select'; candidateId: string }
  | { type: 'training'; value: Partial<GuidedTrainingForm> }
  | { type: 'refs'; value: Partial<Pick<RealFlowState, 'experimentId' | 'revisionId' | 'confirmationId' | 'runId' | 'exportId'>> }
  | { type: 'env-phase'; phase: RealFlowPhase }
  | { type: 'env'; value: GuidedEnvironmentCheck }
  | { type: 'ssh'; value: GuidedSSHVerification }
  | { type: 'codegen-phase'; phase: RealFlowPhase }
  | { type: 'codegen'; value: RealFlowCodegen }
  | { type: 'run'; value: GuidedRun }
  | { type: 'report'; value: GuidedReportStatus }
  | { type: 'events'; value: GuidedRunEvent[] }
  | { type: 'run-reset' }
  | { type: 'export'; exportId: string };

export const REALFLOW_TRAINING_FIELDS = ['seed', 'max_epochs', 'learning_rate', 'batch_size'] as const;
export const REALFLOW_TRAINING_LABELS: Record<keyof GuidedTrainingForm, string> = {
  seed: 'seed（随机种子，整数）',
  max_epochs: 'max_epochs（最大轮数，整数）',
  learning_rate: 'learning_rate（学习率，小数）',
  batch_size: 'batch_size（批大小，整数）',
};

export function createRealFlowState(): RealFlowState {
  return {
    step: 'input', error: null, busy: false, note: null, projectId: null, paper: null, provider: null,
    extractionPhase: 'idle', extraction: null, selectedCandidateId: null,
    training: { seed: '', max_epochs: '', learning_rate: '', batch_size: '' },
    experimentId: null, revisionId: null, confirmationId: null,
    envPhase: 'idle', envCheck: null, ssh: null,
    codegenPhase: 'idle', codegen: null,
    runId: null, run: null, report: null, reportStatus: null, exportId: null, events: [],
    docparse: { pdf: false, needsChoice: false, status: null, text: null },
  };
}

export function candidateKey(candidate: GuidedCandidateExperiment): string {
  return candidate.id ?? candidate.candidate_id ?? candidate.label;
}

export function candidateBlockedReason(candidate: GuidedCandidateExperiment): string | null {
  if (candidate.execution_support === 'supported') return null;
  if (candidate.execution_support === 'requires_review' || candidate.support_status === 'review_required') {
    return 'REVIEW_REQUIRED_CANDIDATE';
  }
  return 'UNSUPPORTED_CANDIDATE';
}

export function findCandidate(extraction: GuidedExtractionReadback | null, key: string | null): GuidedCandidateExperiment | null {
  if (!extraction || key === null) return null;
  return extraction.candidate_experiments.find(candidate => candidateKey(candidate) === key) ?? null;
}

export function isSandboxRecipeSelected(s: Pick<RealFlowState, 'extraction' | 'selectedCandidateId'>): boolean {
  return findCandidate(s.extraction, s.selectedCandidateId)?.recipe_id === SANDBOX_PYTHON_RECIPE;
}

export const REALFLOW_STEP_LABELS: Record<RealFlowStep, string> = {
  input: '文章', provider: 'Provider', extract: '抽取', spec: '方案',
  environment: '环境', codegen: '复现代码', execute: '执行',
};

// The step rail always includes the codegen step so code reproduction is
// available for all recipe types, not just sandbox_python.
export function realFlowSteps(_s: Pick<RealFlowState, 'extraction' | 'selectedCandidateId'>): RealFlowStep[] {
  return ['input', 'provider', 'extract', 'spec', 'environment', 'codegen', 'execute'];
}

// Parse the controlled training form. Empty string means "leave the recipe
// default" (null); a filled value must parse as a number of the right kind or
// the spec is refused before any write happens.
export function parseTrainingForm(form: GuidedTrainingForm): {
  seed: number | null;
  max_epochs: number | null;
  learning_rate: number | null;
  batch_size: number | null;
} {
  const parsed: Record<string, number | null> = {};
  for (const field of REALFLOW_TRAINING_FIELDS) {
    const raw = form[field].trim();
    if (raw === '') { parsed[field] = null; continue; }
    if (!/^[0-9]+(\.[0-9]+)?$/.test(raw)) throw Error('REALFLOW_TRAINING_INVALID');
    const value = Number(raw);
    if (!Number.isFinite(value)) throw Error('REALFLOW_TRAINING_INVALID');
    if ((field === 'seed' || field === 'max_epochs' || field === 'batch_size') && !Number.isSafeInteger(value)) throw Error('REALFLOW_TRAINING_INVALID');
    parsed[field] = value;
  }
  return { seed: parsed.seed ?? null, max_epochs: parsed.max_epochs ?? null, learning_rate: parsed.learning_rate ?? null, batch_size: parsed.batch_size ?? null };
}

// Merge the (optional) training overrides into the server draft spec. Only
// filled fields are written; null stays null so the registered recipe default
// applies. The draft object is never mutated.
export function applyTrainingToSpec(spec: Record<string, unknown>, form: GuidedTrainingForm): Record<string, unknown> {
  const training = parseTrainingForm(form);
  const base = (spec && typeof spec === 'object' && !Array.isArray(spec) && typeof spec.training === 'object' && spec.training && !Array.isArray(spec.training))
    ? spec.training as Record<string, unknown>
    : {};
  return { ...spec, training: { ...base, ...training } };
}

export function canRealViewStep(s: RealFlowState, step: RealFlowStep): boolean {
  if (step === 'input') return true;
  if (step === 'provider' || step === 'extract') return s.paper !== null;
  if (step === 'spec') return s.extractionPhase === 'ready';
  // 生成复现代码 needs the confirmed experiment (the codegen write targets
  // POST /experiments/{eid}/codegen); the rail only offers it for sandbox recipes.
  if (step === 'environment' || step === 'codegen') return s.confirmationId !== null;
  return s.envCheck?.decision === 'ready' || s.ssh !== null;
}

// Terminal outcomes that may be re-executed as a new Run (e.g. after regenerating code).
export function isRetryableRunStatus(status: string | null | undefined): boolean { return status === 'failed' || status === 'cancelled'; }
function sameRun(s: RealFlowState, value: GuidedRun): boolean { return s.runId === null || s.runId === value.run_id; }

// Everything derived from a candidate choice is invalidated when the choice
// basis changes (new candidate picked, or a genuinely re-run extraction).
// Generated code belongs to the experiment of that choice, so it resets too.
const DOWNSTREAM_RESET = {
  experimentId: null, revisionId: null, confirmationId: null,
  envPhase: 'idle' as const, envCheck: null, ssh: null,
  codegenPhase: 'idle' as const, codegen: null,
  runId: null, run: null, report: null, reportStatus: null, exportId: null, events: [],
};

export function realFlowReducer(s: RealFlowState, a: RealFlowAction): RealFlowState {
  if (a.type === 'busy') return { ...s, busy: a.value };
  if (a.type === 'error') return { ...s, error: a.message };
  if (a.type === 'note') return { ...s, note: a.message };
  if (a.type === 'view') {
    if (!canRealViewStep(s, a.step)) {
      return { ...s, error: a.step === 'spec' ? 'EXTRACTION_NOT_READY' : (a.step === 'environment' || a.step === 'codegen') ? 'SPEC_CONFIRMATION_REQUIRED' : a.step === 'execute' ? 'ENVIRONMENT_GATE_REQUIRED' : 'REALFLOW_STEP_LOCKED' };
    }
    return { ...s, step: a.step, error: null };
  }
  if (a.type === 'project') return { ...s, projectId: a.projectId, error: null };
  if (a.type === 'paper') {
    if (s.projectId !== null && a.value.project_id !== s.projectId) return s;
    // A new paper starts its own parse story; the global setting (status) stays.
    const docparse = { ...s.docparse, pdf: a.pdf ?? false, needsChoice: false, text: null };
    return { ...s, paper: a.value, step: s.step === 'input' ? 'provider' : s.step, error: null, docparse };
  }
  if (a.type === 'docparse-status') {
    const needsChoice = a.value.mode === null ? s.docparse.needsChoice : false;
    return { ...s, docparse: { ...s.docparse, status: a.value, needsChoice } };
  }
  if (a.type === 'docparse-text') {
    if (s.paper?.paper_id !== a.paperId) return s;
    return { ...s, docparse: { ...s.docparse, text: a.value } };
  }
  if (a.type === 'docparse-needs-choice') return { ...s, docparse: { ...s.docparse, needsChoice: a.value } };
  if (a.type === 'provider') return { ...s, provider: a.value, error: null };
  if (a.type === 'provider-clear') return { ...s, provider: null, error: null };
  if (a.type === 'extraction-phase') {
    // A genuinely started re-extraction (requireNoPendingRetry already guarded
    // blocked commands) invalidates the old extraction and everything built
    // on it — the old experiment/revision belong to the old extraction.
    if (a.phase === 'loading') return { ...s, extractionPhase: a.phase, extraction: null, selectedCandidateId: null, ...DOWNSTREAM_RESET };
    return { ...s, extractionPhase: a.phase };
  }
  if (a.type === 'extraction') {
    if (s.paper !== null && a.value.paper_id !== s.paper.paper_id) return s;
    return { ...s, extraction: a.value, extractionPhase: 'ready', selectedCandidateId: null, error: null };
  }
  if (a.type === 'select') {
    const candidate = findCandidate(s.extraction, a.candidateId);
    if (!candidate || candidateBlockedReason(candidate) !== null) return { ...s, error: candidate ? candidateBlockedReason(candidate) : 'CANDIDATE_NOT_FOUND' };
    if (s.selectedCandidateId === a.candidateId) return s.error === null ? s : { ...s, error: null };
    // A different candidate invalidates everything downstream of the choice.
    return { ...s, selectedCandidateId: a.candidateId, error: null, ...DOWNSTREAM_RESET };
  }
  if (a.type === 'training') return { ...s, training: { ...s.training, ...a.value } };
  if (a.type === 'refs') return { ...s, ...a.value, error: null };
  if (a.type === 'env-phase') return { ...s, envPhase: a.phase };
  if (a.type === 'env') {
    if (s.experimentId !== null && a.value.experiment_id !== s.experimentId) return s;
    return { ...s, envCheck: a.value, envPhase: 'ready', ssh: null, error: null };
  }
  if (a.type === 'ssh') {
    if (s.envCheck !== null && a.value.environment_check_id !== s.envCheck.environment_check_id) return s;
    return { ...s, ssh: a.value, error: null };
  }
  // A regeneration keeps the previous attempt visible until the new attempt is
  // accepted (the server-side latest attempt only changes on success); a
  // loading/error phase never fabricates or destroys code by itself.
  if (a.type === 'codegen-phase') return { ...s, codegenPhase: a.phase };
  if (a.type === 'codegen') return { ...s, codegen: a.value, codegenPhase: 'ready', error: null };
  // A terminal failed/cancelled Run is history: drop it so the next submission
  // creates a brand-new Run (new Idempotency-Key, fresh event cursor).
  if (a.type === 'run-reset') return { ...s, runId: null, run: null, report: null, reportStatus: null, exportId: null, events: [], error: null };
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
  if (a.type === 'export') return { ...s, exportId: a.exportId, error: null };
  return s;
}
