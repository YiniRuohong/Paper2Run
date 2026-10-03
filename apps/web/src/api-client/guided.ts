import type {
  GuidedAttempt,
  GuidedAttemptCode,
  GuidedBootstrap,
  GuidedCandidateExperiment,
  GuidedCodegenAccepted,
  GuidedConfirmation,
  GuidedDownloadRegistration,
  GuidedEnvironmentCheck,
  GuidedEnvironmentCheckItem,
  GuidedEnvironmentDecision,
  GuidedExtractionAccepted,
  GuidedExtractionReadback,
  GuidedExperiment,
  GuidedExport,
  GuidedExportAccepted,
  GuidedExportFormat,
  GuidedOperation,
  GuidedPage,
  GuidedPaperAccepted,
  GuidedPaperInfo,
  GuidedProjectAccepted,
  GuidedProviderSessionSummary,
  GuidedReceipt,
  GuidedReportBuildAccepted,
  GuidedReportStatus,
  GuidedRevision,
  GuidedRun,
  GuidedRunAccepted,
  GuidedRunEvent,
  GuidedSSHVerification,
  GuidedSourceRef,
  GuidedTextChunk,
  WriteIntent,
} from './guided-types';

export type GuidedValidator<T> = (value: unknown) => T;
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

export class GuidedApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;
  readonly details: unknown;
  readonly unknown: boolean;

  constructor(message: string, status: number, code: string, requestId?: string, details?: unknown, unknown = false) {
    super(message);
    this.name = 'GuidedApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
    this.unknown = unknown;
  }
}

export interface GuidedApi {
  read<T>(path: string, validator: GuidedValidator<T>, signal?: AbortSignal): Promise<T>;
  readArray<T>(path: string, validator: GuidedValidator<T>, signal?: AbortSignal): Promise<GuidedPage<T>>;
  write<T>(intent: WriteIntent, validator: GuidedValidator<T>): Promise<T>;
  receipt(key: string, signal?: AbortSignal): Promise<GuidedReceipt>;
  download(item: GuidedDownloadRegistration, signal?: AbortSignal): Promise<void>;
  runEvents(runId: string, after: number, signal?: AbortSignal): Promise<GuidedRunEvent[]>;
  // --- real article flow (direct writes: fresh Idempotency-Key per call,
  // bodies never persisted by the caller unless it opts in) ---
  createProject(name: string, key?: string): Promise<GuidedProjectAccepted>;
  uploadPaperPdf(projectId: string, file: File, options?: { titleHint?: string; key?: string }): Promise<GuidedPaperAccepted>;
  createTextPaper(projectId: string, input: { text: string; titleHint?: string; key?: string }): Promise<GuidedPaperAccepted>;
  getPaper(paperId: string, signal?: AbortSignal): Promise<GuidedPaperInfo>;
  createProviderSession(input: { base_url: string; model_id: string; api_key: string; endpoint_alias: string; key?: string }): Promise<GuidedProviderSessionSummary>;
  deleteProviderSession(providerSessionId: string, key?: string): Promise<{ provider_session_id: string; secret_configured: boolean }>;
  createExtraction(paperId: string, input: { reason: string; provider_session_id: string | null; key?: string }): Promise<GuidedExtractionAccepted>;
  getExtraction(paperId: string, signal?: AbortSignal): Promise<GuidedExtractionReadback | null>;
  createEnvironmentCheck(projectId: string, experimentId: string, input: { revision_id: string; requirements?: Record<string, unknown>; key?: string }): Promise<GuidedEnvironmentCheck>;
  createSshVerification(environmentCheckId: string, input: { host: string; port: number; user: string; workdir: string; auth: { type: 'agent'; agent_id: string } | { type: 'key_reference'; key_reference: string }; key?: string }): Promise<GuidedSSHVerification>;
  createCodegen(experimentId: string, input: { provider_session_id: string | null; reason: string; prompt_version?: string; key?: string }): Promise<GuidedCodegenAccepted>;
  readAttemptCode(experimentId: string, attemptIndex: number, signal?: AbortSignal): Promise<GuidedAttemptCode>;
}

export const GUIDED_PATHS = {
  demo: '/demo',
  bootstrap: '/demo/bootstrap',
  demoCapabilities: '/demo',
  projects: '/projects',
  projectPapers: (projectId: string) => `/projects/${encodeURIComponent(projectId)}/papers`,
  projectPapersText: (projectId: string) => `/projects/${encodeURIComponent(projectId)}/papers/text`,
  paper: (paperId: string) => `/papers/${encodeURIComponent(paperId)}`,
  paperExtractions: (paperId: string) => `/papers/${encodeURIComponent(paperId)}/extractions`,
  paperExtraction: (paperId: string) => `/papers/${encodeURIComponent(paperId)}/extraction`,
  providerSessions: '/provider-sessions',
  providerSession: (providerSessionId: string) => `/provider-sessions/${encodeURIComponent(providerSessionId)}`,
  environmentChecks: (projectId: string, experimentId: string) => `/projects/${encodeURIComponent(projectId)}/experiments/${encodeURIComponent(experimentId)}/environment-checks`,
  environmentCheck: (environmentCheckId: string) => `/environment-checks/${encodeURIComponent(environmentCheckId)}`,
  sshVerifications: (environmentCheckId: string) => `/environment-checks/${encodeURIComponent(environmentCheckId)}/ssh-verifications`,
  experiments: (projectId: string) => `/projects/${encodeURIComponent(projectId)}/experiments`,
  experiment: (experimentId: string) => `/experiments/${encodeURIComponent(experimentId)}`,
  draft: (experimentId: string) => `/experiments/${encodeURIComponent(experimentId)}/draft`,
  experimentCodegen: (experimentId: string) => `/experiments/${encodeURIComponent(experimentId)}/codegen`,
  attemptCode: (experimentId: string, attemptIndex: number) => `/experiments/${encodeURIComponent(experimentId)}/attempts/${attemptIndex}/code`,
  revisions: (experimentId: string) => `/experiments/${encodeURIComponent(experimentId)}/revisions`,
  revision: (revisionId: string) => `/spec-revisions/${encodeURIComponent(revisionId)}`,
  confirm: (revisionId: string) => `/spec-revisions/${encodeURIComponent(revisionId)}/confirm`,
  runs: (projectId: string) => `/projects/${encodeURIComponent(projectId)}/runs`,
  run: (runId: string) => `/runs/${encodeURIComponent(runId)}`,
  runEvents: (runId: string) => `/runs/${encodeURIComponent(runId)}/events`,
  cancel: (runId: string) => `/runs/${encodeURIComponent(runId)}/cancel`,
  attempts: (runId: string) => `/runs/${encodeURIComponent(runId)}/attempts`,
  reportStatus: (runId: string) => `/runs/${encodeURIComponent(runId)}/report-status`,
  reportBuilds: (runId: string) => `/runs/${encodeURIComponent(runId)}/report-builds`,
  report: (runId: string) => `/runs/${encodeURIComponent(runId)}/report`,
  exports: (runId: string) => `/runs/${encodeURIComponent(runId)}/exports`,
  operation: (operationId: string) => `/operations/${encodeURIComponent(operationId)}`,
  text: (artifactId: string) => `/artifacts/${encodeURIComponent(artifactId)}/text`,
  download: (artifactId: string) => `/artifacts/${encodeURIComponent(artifactId)}/download`,
} as const;

const BASE = '/api/v1';
const HASH = /^[0-9a-f]{64}$/;
const INTEGER = /^\d+$/;
const RUN_STATUS = new Set([
  // 'created' is the honest status of an SSH run: the verified target was
  // recorded, nothing was enqueued or executed (no remote adapter exists).
  'created', 'queued', 'running', 'evaluating', 'cancel_requested',
  'completed', 'failed', 'cancelled', 'awaiting_revision',
]);
const EXPORT_FORMATS = new Set<GuidedExportFormat>([
  'report_json', 'report_markdown', 'bundle_zip', 'report_pdf',
]);

function invalid(message: string): never {
  throw new GuidedApiError(message, 200, 'INVALID_DTO');
}

function record(value: unknown, message = 'expected an object'): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(message);
  return value as Record<string, unknown>;
}

function str(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) invalid(`${path} must be a non-empty string`);
  return value;
}

function nullableStr(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || value.length === 0) invalid(`${path} must be a non-empty string or null`);
  return value;
}

function nullableInt(value: unknown, path: string): number | null {
  return value === null ? null : nonNegativeInt(value, path);
}

function int(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) invalid(`${path} must be an integer`);
  return value;
}

function nonNegativeInt(value: unknown, path: string): number {
  const parsed = int(value, path);
  if (parsed < 0) invalid(`${path} must be non-negative`);
  return parsed;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') invalid(`${path} must be a boolean`);
  return value;
}

function validateError(value: unknown): { code: string; message: string } | null {
  if (value === null || value === undefined) return null;
  const item = record(value, 'error must be an object or null');
  const code = str(item.code, 'error.code');
  const message = item.message === undefined ? code : str(item.message, 'error.message');
  return { code, message };
}

export function validateRun(value: unknown): GuidedRun {
  const item = record(value, 'run must be an object');
  const status = str(item.status, 'run.status');
  if (!RUN_STATUS.has(status)) invalid('run.status is unsupported');
  return {
    run_id: str(item.run_id, 'run.run_id'),
    project_id: str(item.project_id, 'run.project_id'),
    experiment_id: str(item.experiment_id, 'run.experiment_id'),
    revision_id: str(item.revision_id, 'run.revision_id'),
    status: status as GuidedRun['status'],
    last_event_seq: nonNegativeInt(item.last_event_seq, 'run.last_event_seq'),
    created_at: str(item.created_at, 'run.created_at'),
    updated_at: str(item.updated_at, 'run.updated_at'),
    error: validateError(item.error) as GuidedRun['error'],
  };
}

export function validateBootstrap(value: unknown): GuidedBootstrap {
  const item = record(value, 'bootstrap must be an object');
  return {
    project_id: str(item.project_id, 'bootstrap.project_id'),
    paper_id: str(item.paper_id, 'bootstrap.paper_id'),
    extraction_id: str(item.extraction_id, 'bootstrap.extraction_id'),
  };
}

export function validateExperiment(value: unknown): GuidedExperiment {
  const item = record(value, 'experiment must be an object');
  const status = str(item.status, 'experiment.status');
  if (!new Set(['draft', 'ready', 'active', 'needs_revision', 'closed']).has(status)) invalid('experiment.status is unsupported');
  return {
    experiment_id: str(item.experiment_id, 'experiment.experiment_id'),
    project_id: str(item.project_id, 'experiment.project_id'),
    paper_id: str(item.paper_id, 'experiment.paper_id'),
    status: status as GuidedExperiment['status'],
    head_revision_id: nullableStr(item.head_revision_id, 'experiment.head_revision_id'),
    active_run_id: nullableStr(item.active_run_id, 'experiment.active_run_id'),
    latest_run_id: nullableStr(item.latest_run_id, 'experiment.latest_run_id'),
    repairs_used: nonNegativeInt(item.repairs_used, 'experiment.repairs_used'),
    repairs_limit: nonNegativeInt(item.repairs_limit, 'experiment.repairs_limit'),
    created_at: str(item.created_at, 'experiment.created_at'),
  };
}

export function validateRevision(value: unknown): GuidedRevision {
  const item = record(value, 'revision must be an object');
  const confirmationValue = item.confirmation;
  const confirmation = confirmationValue === null || confirmationValue === undefined ? null : (() => {
    const conf = record(confirmationValue, 'revision.confirmation must be an object or null');
    return {
      confirmation_id: str(conf.confirmation_id, 'revision.confirmation.confirmation_id'),
      revision_id: str(conf.revision_id, 'revision.confirmation.revision_id'),
      spec_hash: str(conf.spec_hash, 'revision.confirmation.spec_hash'),
      confirmed_by: str(conf.confirmed_by, 'revision.confirmation.confirmed_by'),
      confirmed_at: str(conf.confirmed_at, 'revision.confirmation.confirmed_at'),
    };
  })();
  const spec = record(item.spec, 'revision.spec must be an object');
  return {
    id: str(item.id, 'revision.id'),
    experiment_id: str(item.experiment_id, 'revision.experiment_id'),
    revision_no: nonNegativeInt(item.revision_no, 'revision.revision_no'),
    spec_hash: str(item.spec_hash, 'revision.spec_hash'),
    policy_hash: str(item.policy_hash, 'revision.policy_hash'),
    spec,
    validation: record(item.validation, 'revision.validation must be an object'),
    comparison_precheck: item.comparison_precheck,
    budget_estimate: item.budget_estimate,
    confirmation,
    is_current: boolean(item.is_current, 'revision.is_current'),
    created_at: str(item.created_at, 'revision.created_at'),
  };
}

export function validateConfirmation(value: unknown): GuidedConfirmation {
  const item = record(value, 'confirmation must be an object');
  return {
    confirmation_id: str(item.confirmation_id, 'confirmation.confirmation_id'),
    revision_id: str(item.revision_id, 'confirmation.revision_id'),
    spec_hash: str(item.spec_hash, 'confirmation.spec_hash'),
    confirmed_by: str(item.confirmed_by, 'confirmation.confirmed_by'),
    confirmed_at: str(item.confirmed_at, 'confirmation.confirmed_at'),
  };
}

export function validateRunAccepted(value: unknown): GuidedRunAccepted {
  const item = record(value, 'run accepted must be an object');
  const status = str(item.status, 'run accepted.status');
  if (status !== 'queued') invalid('run accepted.status must be queued');
  return {
    run_id: str(item.run_id, 'run accepted.run_id'),
    status: 'queued',
    events_url: str(item.events_url, 'run accepted.events_url'),
    last_event_seq: nonNegativeInt(item.last_event_seq, 'run accepted.last_event_seq'),
  };
}

export function validateOperation(value: unknown): GuidedOperation {
  const item = record(value, 'operation must be an object');
  const status = str(item.status, 'operation.status');
  if (!new Set(['queued', 'running', 'succeeded', 'failed', 'cancelled']).has(status)) invalid('operation.status is unsupported');
  return {
    operation_id: str(item.operation_id, 'operation.operation_id'),
    kind: str(item.kind, 'operation.kind'),
    resource_id: str(item.resource_id, 'operation.resource_id'),
    status: status as GuidedOperation['status'],
    error: validateError(item.error) as GuidedOperation['error'],
  };
}

export function validateAttempt(value: unknown): GuidedAttempt {
  const item = record(value, 'attempt must be an object');
  return {
    attempt_id: str(item.attempt_id, 'attempt.attempt_id'),
    run_id: str(item.run_id, 'attempt.run_id'),
    attempt_index: nonNegativeInt(item.attempt_index, 'attempt.attempt_index'),
    repair_ordinal: nonNegativeInt(item.repair_ordinal, 'attempt.repair_ordinal'),
    source: typeof item.source === 'string' ? item.source : undefined,
    status: str(item.status, 'attempt.status'),
    base_code_hash: typeof item.base_code_hash === 'string' ? item.base_code_hash : null,
    patch_hash: typeof item.patch_hash === 'string' ? item.patch_hash : null,
    created_at: str(item.created_at, 'attempt.created_at'),
    updated_at: str(item.updated_at, 'attempt.updated_at'),
  };
}

export function validateTextChunk(value: unknown): GuidedTextChunk {
  const item = record(value, 'text chunk must be an object');
  const text = item.text;
  if (typeof text !== 'string') invalid('text chunk.text must be a string');
  const offset = nonNegativeInt(item.offset, 'text chunk.offset');
  const nextOffset = nonNegativeInt(item.next_offset, 'text chunk.next_offset');
  if (nextOffset < offset) invalid('text chunk.next_offset must not move backwards');
  const decodedBytes = new TextEncoder().encode(text).byteLength;
  if (offset + decodedBytes !== nextOffset) {
    invalid('text chunk offsets must count UTF-8 bytes, not JavaScript string length');
  }
  return {
    artifact_id: str(item.artifact_id, 'text chunk.artifact_id'),
    text,
    offset,
    next_offset: nextOffset,
    eof: boolean(item.eof, 'text chunk.eof'),
  };
}

export function validateReportStatus(value: unknown): GuidedReportStatus {
  const item = record(value, 'report status must be an object');
  const status = item.status;
  if (status !== null && !new Set(['pending', 'generating', 'ready', 'failed']).has(String(status))) invalid('report status is unsupported');
  return {
    run_id: str(item.run_id, 'report status.run_id'),
    status: status === null ? null : status as GuidedReportStatus['status'],
    build_id: nullableStr(item.build_id, 'report status.build_id'),
    operation_id: nullableStr(item.operation_id, 'report status.operation_id'),
    report_hash: nullableStr(item.report_hash, 'report status.report_hash'),
    error: validateError(item.error) as GuidedReportStatus['error'],
  };
}

export function validateReportBuildAccepted(value: unknown): GuidedReportBuildAccepted {
  const item = record(value, 'report build must be an object');
  const status = str(item.status, 'report build.status');
  if (!new Set(['pending', 'generating', 'ready', 'failed']).has(status)) invalid('report build.status is unsupported');
  return {
    build_id: nullableStr(item.build_id, 'report build.build_id'),
    operation_id: nullableStr(item.operation_id, 'report build.operation_id'),
    run_id: str(item.run_id, 'report build.run_id'),
    status: status as GuidedReportBuildAccepted['status'],
  };
}

function validateExportItem(value: unknown, path: string): GuidedExport['items'][number] {
  const item = record(value, `${path} must be an object`);
  const format = str(item.format, `${path}.format`);
  if (!EXPORT_FORMATS.has(format as GuidedExportFormat)) invalid(`${path}.format is unsupported`);
  const status = str(item.status, `${path}.status`);
  if (!new Set(['pending', 'running', 'ready', 'failed']).has(status)) invalid(`${path}.status is unsupported`);
  if (!('filename' in item)) invalid(`${path}.filename must be present`);
  if (!('bytes' in item)) invalid(`${path}.bytes must be present`);
  if (!('media_type' in item)) invalid(`${path}.media_type must be present`);
  if (!('sha256' in item)) invalid(`${path}.sha256 must be present`);
  const filename = nullableStr(item.filename, `${path}.filename`);
  const bytes = nullableInt(item.bytes, `${path}.bytes`);
  const mediaType = nullableStr(item.media_type, `${path}.media_type`);
  const sha256 = nullableStr(item.sha256, `${path}.sha256`);
  if (sha256 !== null && !HASH.test(sha256)) invalid(`${path}.sha256 must be a 64-character SHA-256 hash`);
  if (bytes !== null && filename === null) invalid(`${path}.filename is required for a ready file`);
  if (bytes !== null && mediaType === null) invalid(`${path}.media_type is required for a ready file`);
  if (bytes !== null && sha256 === null) invalid(`${path}.sha256 is required for a ready file`);
  if (bytes === null && (mediaType !== null || sha256 !== null)) {
    invalid(`${path} file metadata must be null until bytes are registered`);
  }
  return {
    format: format as GuidedExportFormat,
    status: status as GuidedExport['items'][number]['status'],
    artifact_id: nullableStr(item.artifact_id, `${path}.artifact_id`),
    filename,
    bytes,
    media_type: mediaType,
    sha256,
    error: validateError(item.error),
  };
}

export function validateExport(value: unknown): GuidedExport {
  const item = record(value, 'export must be an object');
  const status = str(item.status, 'export.status');
  if (!new Set(['queued', 'running', 'succeeded', 'partial', 'failed']).has(status)) invalid('export.status is unsupported');
  const rawItems = item.items;
  if (!Array.isArray(rawItems)) invalid('export.items must be an array');
  return {
    id: str(item.id, 'export.id'),
    run_id: str(item.run_id, 'export.run_id'),
    status: status as GuidedExport['status'],
    operation_id: nullableStr(item.operation_id, 'export.operation_id'),
    report_hash: nullableStr(item.report_hash, 'export.report_hash'),
    items: rawItems.map((entry, index) => validateExportItem(entry, `export.items[${index}]`)),
    created_at: str(item.created_at, 'export.created_at'),
  };
}

export function validateExportAccepted(value: unknown): GuidedExportAccepted {
  const item = record(value, 'export accepted must be an object');
  const status = str(item.status, 'export accepted.status');
  if (status !== 'queued') invalid('export accepted.status must be queued');
  return {
    export_id: str(item.export_id, 'export accepted.export_id'),
    operation_id: str(item.operation_id, 'export accepted.operation_id'),
    status: 'queued',
  };
}

export function validateReceipt(value: unknown): GuidedReceipt {
  const item = record(value, 'receipt must be an object');
  const state = str(item.state, 'receipt.state');
  if (state !== 'pending' && state !== 'completed') invalid('receipt.state must be pending or completed');
  const targetPath = str(item.target_path, 'receipt.target_path');
  const requestSha256 = str(item.request_sha256, 'receipt.request_sha256');
  if (!HASH.test(requestSha256)) invalid('receipt.request_sha256 must be a 64-character SHA-256 hash');
  if (!('response' in item)) invalid('receipt.response must be present');
  if (state === 'pending' && item.response !== null) invalid('pending receipt.response must be null');
  return {
    state,
    target_path: targetPath,
    request_sha256: requestSha256,
    response: item.response,
  };
}

// --- Real article flow validators -------------------------------------------

export function validateProjectAccepted(value: unknown): GuidedProjectAccepted {
  const item = record(value, 'project accepted must be an object');
  return {
    id: str(item.id, 'project accepted.id'),
    name: str(item.name, 'project accepted.name'),
    role: str(item.role, 'project accepted.role'),
    version: nonNegativeInt(item.version, 'project accepted.version'),
    created_at: str(item.created_at, 'project accepted.created_at'),
  };
}

export function validatePaperAccepted(value: unknown): GuidedPaperAccepted {
  const item = record(value, 'paper accepted must be an object');
  return {
    paper_id: str(item.paper_id, 'paper accepted.paper_id'),
    operation_id: nullableStr(item.operation_id, 'paper accepted.operation_id'),
    status: str(item.status, 'paper accepted.status'),
  };
}

export function validatePaperInfo(value: unknown): GuidedPaperInfo {
  const item = record(value, 'paper info must be an object');
  const pageCount = item.page_count;
  if (pageCount !== null && typeof pageCount !== 'number') invalid('paper info.page_count must be a number or null');
  return {
    paper_id: str(item.paper_id, 'paper info.paper_id'),
    project_id: str(item.project_id, 'paper info.project_id'),
    title: nullableStr(item.title, 'paper info.title'),
    status: str(item.status, 'paper info.status'),
    sha256: str(item.sha256, 'paper info.sha256'),
    page_count: pageCount === null ? null : nonNegativeInt(pageCount, 'paper info.page_count'),
    content_available: boolean(item.content_available, 'paper info.content_available'),
    current_extraction_id: nullableStr(item.current_extraction_id, 'paper info.current_extraction_id'),
    active_operation_id: nullableStr(item.active_operation_id, 'paper info.active_operation_id'),
    error: validateError(item.error) as GuidedPaperInfo['error'],
    created_at: str(item.created_at, 'paper info.created_at'),
  };
}

export function validateProviderSessionSummary(value: unknown): GuidedProviderSessionSummary {
  const item = record(value, 'provider session must be an object');
  return {
    provider_session_id: str(item.provider_session_id, 'provider session.provider_session_id'),
    endpoint_alias: str(item.endpoint_alias, 'provider session.endpoint_alias'),
    model_id: str(item.model_id, 'provider session.model_id'),
    base_url: str(item.base_url, 'provider session.base_url'),
    secret_configured: boolean(item.secret_configured, 'provider session.secret_configured'),
    secret_last4: typeof item.secret_last4 === 'string' ? item.secret_last4 : '',
  };
}

export function validateExtractionAccepted(value: unknown): GuidedExtractionAccepted {
  const item = record(value, 'extraction accepted must be an object');
  return {
    paper_id: str(item.paper_id, 'extraction accepted.paper_id'),
    operation_id: nullableStr(item.operation_id, 'extraction accepted.operation_id'),
    status: str(item.status, 'extraction accepted.status'),
    extraction_id: nullableStr(item.extraction_id, 'extraction accepted.extraction_id'),
  };
}

function validateSourceRef(value: unknown, path: string): GuidedSourceRef {
  const item = record(value, `${path} must be an object`);
  return {
    paper_id: nullableStr(item.paper_id ?? null, `${path}.paper_id`),
    pdf_sha256: nullableStr(item.pdf_sha256 ?? null, `${path}.pdf_sha256`),
    page: nullableInt(item.page ?? null, `${path}.page`),
    section: nullableStr(item.section ?? null, `${path}.section`),
    table: nullableInt(item.table ?? null, `${path}.table`),
    row: nullableInt(item.row ?? null, `${path}.row`),
    quote: nullableStr(item.quote ?? null, `${path}.quote`),
    bbox: item.bbox === undefined ? null : item.bbox,
  };
}

function validateCandidate(value: unknown, index: number): GuidedCandidateExperiment {
  const item = record(value, `candidate[${index}] must be an object`);
  const support = str(item.execution_support, `candidate[${index}].execution_support`);
  if (support !== 'supported' && support !== 'requires_review' && support !== 'unsupported') {
    invalid(`candidate[${index}].execution_support is unsupported`);
  }
  const rawReasons = item.unsupported_reasons;
  const unsupportedReasons = rawReasons === null || rawReasons === undefined ? [] : rawReasons;
  if (!Array.isArray(unsupportedReasons)) invalid(`candidate[${index}].unsupported_reasons must be an array`);
  const rawRefs = item.source_refs;
  const sourceRefs = rawRefs === null || rawRefs === undefined ? [] : rawRefs;
  if (!Array.isArray(sourceRefs)) invalid(`candidate[${index}].source_refs must be an array`);
  return {
    id: nullableStr(item.id ?? null, `candidate[${index}].id`),
    candidate_id: nullableStr(item.candidate_id ?? null, `candidate[${index}].candidate_id`),
    label: typeof item.label === 'string' && item.label.length > 0 ? item.label : (nullableStr(item.id ?? null, `candidate[${index}].id`) ?? ''),
    recipe_id: nullableStr(item.recipe_id ?? null, `candidate[${index}].recipe_id`),
    recipe_version: nullableStr(item.recipe_version ?? null, `candidate[${index}].recipe_version`),
    support_status: nullableStr(item.support_status ?? null, `candidate[${index}].support_status`),
    execution_support: support,
    unsupported_reasons: unsupportedReasons.map((entry, i) => str(entry, `candidate[${index}].unsupported_reasons[${i}]`)),
    parameters: recordOrEmpty(item.parameters, `candidate[${index}].parameters`),
    required_dependencies: arrayOrEmpty(item.required_dependencies, `candidate[${index}].required_dependencies`),
    required_datasets: arrayOrEmpty(item.required_datasets, `candidate[${index}].required_datasets`),
    resource_requirements: recordOrEmpty(item.resource_requirements, `candidate[${index}].resource_requirements`),
    estimated_runtime: item.estimated_runtime ?? null,
    source_refs: sourceRefs.map((entry, i) => validateSourceRef(entry, `candidate[${index}].source_refs[${i}]`)),
    provenance: nullableStr(item.provenance ?? null, `candidate[${index}].provenance`),
  };
}

function recordOrEmpty(value: unknown, path: string): Record<string, unknown> {
  if (value === null || value === undefined) return {};
  return record(value, `${path} must be an object`);
}

function arrayOrEmpty(value: unknown, path: string): unknown[] {
  if (value === null || value === undefined) return [];
  if (!Array.isArray(value)) invalid(`${path} must be an array`);
  return value;
}

export function validateExtractionReadback(value: unknown): GuidedExtractionReadback {
  const item = record(value, 'extraction must be an object');
  const fieldsValue = record(item.fields ?? {}, 'extraction.fields must be an object');
  const fields: Record<string, GuidedExtractionReadback['fields'][string]> = {};
  for (const [name, raw] of Object.entries(fieldsValue)) {
    const field = record(raw, `extraction.fields.${name} must be an object`);
    fields[name] = {
      value: field.value ?? null,
      evidence_status: nullableStr(field.evidence_status, `extraction.fields.${name}.evidence_status`),
      source_refs: arrayOrEmpty(field.source_refs, `extraction.fields.${name}.source_refs`),
    };
  }
  const rawRefs = item.source_refs;
  if (!Array.isArray(rawRefs)) invalid('extraction.source_refs must be an array');
  return {
    extraction_id: nullableStr(item.extraction_id, 'extraction.extraction_id'),
    paper_id: str(item.paper_id, 'extraction.paper_id'),
    schema_version: str(item.schema_version, 'extraction.schema_version'),
    fields,
    methods: arrayOrEmpty(item.methods, 'extraction.methods') as Array<Record<string, unknown>>,
    formulas: arrayOrEmpty(item.formulas, 'extraction.formulas') as Array<Record<string, unknown>>,
    candidate_experiments: arrayOrEmpty(item.candidate_experiments, 'extraction.candidate_experiments').map(validateCandidate),
    source_refs: rawRefs.map((entry, i) => validateSourceRef(entry, `extraction.source_refs[${i}]`)),
    warnings: arrayOrEmpty(item.warnings, 'extraction.warnings').map((entry, i) => str(entry, `extraction.warnings[${i}]`)),
    provider: recordOrEmpty(item.provider, 'extraction.provider'),
  };
}

const CHECK_ITEM_STATUS = new Set<GuidedEnvironmentCheckItem['status']>(['pass', 'fail', 'unknown', 'not_needed']);
const ENVIRONMENT_DECISIONS = new Set<GuidedEnvironmentDecision>(['ready', 'blocked', 'unsupported']);

export function validateEnvironmentCheck(value: unknown): GuidedEnvironmentCheck {
  const item = record(value, 'environment check must be an object');
  const decision = str(item.decision, 'environment check.decision');
  if (!ENVIRONMENT_DECISIONS.has(decision as GuidedEnvironmentDecision)) invalid('environment check.decision is unsupported');
  const rawItems = item.items;
  if (!Array.isArray(rawItems)) invalid('environment check.items must be an array');
  const items = rawItems.map((entry, index) => {
    const checkItem = record(entry, `environment check.items[${index}] must be an object`);
    const status = str(checkItem.status, `environment check.items[${index}].status`);
    if (!CHECK_ITEM_STATUS.has(status as GuidedEnvironmentCheckItem['status'])) invalid(`environment check.items[${index}].status is unsupported`);
    return {
      key: str(checkItem.key, `environment check.items[${index}].key`),
      status: status as GuidedEnvironmentCheckItem['status'],
      required: boolean(checkItem.required ?? true, `environment check.items[${index}].required`),
      observed: checkItem.observed ?? null,
      required_value: checkItem.required_value ?? null,
      suggestion: typeof checkItem.suggestion === 'string' ? checkItem.suggestion : '',
    };
  });
  return {
    environment_check_id: str(item.environment_check_id, 'environment check.environment_check_id'),
    project_id: str(item.project_id, 'environment check.project_id'),
    experiment_id: str(item.experiment_id, 'environment check.experiment_id'),
    revision_id: str(item.revision_id, 'environment check.revision_id'),
    decision: decision as GuidedEnvironmentDecision,
    status: decision as GuidedEnvironmentDecision,
    checker_version: str(item.checker_version, 'environment check.checker_version'),
    requirements: recordOrEmpty(item.requirements, 'environment check.requirements'),
    items,
    repair_suggestions: arrayOrEmpty(item.repair_suggestions, 'environment check.repair_suggestions').map((entry, i) => str(entry, `environment check.repair_suggestions[${i}]`)),
    created_at: str(item.created_at, 'environment check.created_at'),
  };
}

export function validateSSHVerification(value: unknown): GuidedSSHVerification {
  const item = record(value, 'ssh verification must be an object');
  return {
    ssh_verification_id: str(item.ssh_verification_id, 'ssh verification.ssh_verification_id'),
    environment_check_id: str(item.environment_check_id, 'ssh verification.environment_check_id'),
    project_id: str(item.project_id, 'ssh verification.project_id'),
    experiment_id: str(item.experiment_id, 'ssh verification.experiment_id'),
    revision_id: str(item.revision_id, 'ssh verification.revision_id'),
    status: str(item.status, 'ssh verification.status'),
    target: recordOrEmpty(item.target, 'ssh verification.target'),
    summary: recordOrEmpty(item.summary, 'ssh verification.summary'),
    created_at: str(item.created_at, 'ssh verification.created_at'),
    updated_at: str(item.updated_at, 'ssh verification.updated_at'),
  };
}

// Sandbox code generation (recipe sandbox_python). The wire payload is the
// cross-task contract minimum {attempt_id, attempt_index, code, code_sha256};
// notes is optional. code may legitimately be any string (the display shows it
// verbatim), but the hash must be a real 64-hex SHA-256.
export function validateCodegenAccepted(value: unknown): GuidedCodegenAccepted {
  const item = record(value, 'codegen accepted must be an object');
  if (typeof item.code !== 'string') invalid('codegen accepted.code must be a string');
  const codeSha256 = str(item.code_sha256, 'codegen accepted.code_sha256');
  if (!HASH.test(codeSha256)) invalid('codegen accepted.code_sha256 must be a 64-character SHA-256 hash');
  return {
    attempt_id: str(item.attempt_id, 'codegen accepted.attempt_id'),
    attempt_index: nonNegativeInt(item.attempt_index, 'codegen accepted.attempt_index'),
    code: item.code,
    code_sha256: codeSha256,
    notes: nullableStr(item.notes ?? null, 'codegen accepted.notes'),
  };
}

export function validateAttemptCode(value: unknown): GuidedAttemptCode {
  const item = record(value, 'attempt code must be an object');
  if (typeof item.code !== 'string') invalid('attempt code.code must be a string');
  const codeSha256 = str(item.code_sha256, 'attempt code.code_sha256');
  if (!HASH.test(codeSha256)) invalid('attempt code.code_sha256 must be a 64-character SHA-256 hash');
  return { code: item.code, code_sha256: codeSha256, notes: nullableStr(item.notes ?? null, 'attempt code.notes') };
}

function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || (error instanceof DOMException && error.name === 'AbortError')
    || (error instanceof Error && error.name === 'AbortError');
}

function asUnknown(error: unknown, fallbackMessage: string, status: number, code: string, requestId?: string, details?: unknown): GuidedApiError {
  if (error instanceof GuidedApiError) {
    if (error.unknown || error.code === 'NETWORK_ERROR' || error.code === 'INTENT_ACCEPTED' || error.code === 'INTENT_UNKNOWN') return error;
    return new GuidedApiError(error.message, error.status, error.code, error.requestId ?? requestId, error.details ?? details, true);
  }
  return new GuidedApiError(fallbackMessage, status, code, requestId, details, true);
}

function errorEnvelope(payload: unknown, response: Response): GuidedApiError {
  const requestId = response.headers.get('X-Request-ID') ?? undefined;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return new GuidedApiError(`请求失败 (HTTP ${response.status})`, response.status, 'UNKNOWN_ERROR', requestId, undefined, true);
  }
  const envelope = payload as Record<string, unknown>;
  const error = envelope.error && typeof envelope.error === 'object' && !Array.isArray(envelope.error)
    ? envelope.error as Record<string, unknown>
    : null;
  if (!error) return new GuidedApiError(`请求失败 (HTTP ${response.status})`, response.status, 'UNKNOWN_ERROR', requestId, undefined, true);
  const code = typeof error.code === 'string' ? error.code : 'UNKNOWN_ERROR';
  const message = typeof error.message === 'string' ? error.message : `请求失败 (HTTP ${response.status})`;
  const metaId = envelope.meta && typeof envelope.meta === 'object' && !Array.isArray(envelope.meta)
    ? (envelope.meta as Record<string, unknown>).request_id
    : undefined;
  return new GuidedApiError(message, response.status, code, typeof metaId === 'string' ? metaId : requestId, error.details, response.status >= 500);
}

async function responsePayload(response: Response, unknown = false): Promise<unknown> {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    throw new GuidedApiError('API 未返回有效 JSON；请检查代理或后端。', response.status, 'INVALID_JSON', response.headers.get('X-Request-ID') ?? undefined, undefined, unknown);
  }
}

function envelopeData(payload: unknown, status: number, requestId?: string, unknown = false): unknown {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new GuidedApiError('API 成功响应缺少 data。', status, 'INVALID_ENVELOPE', requestId, undefined, unknown);
  }
  const envelope = payload as Record<string, unknown>;
  const meta = envelope.meta && typeof envelope.meta === 'object' && !Array.isArray(envelope.meta)
    ? envelope.meta as Record<string, unknown>
    : undefined;
  const resolvedRequestId = typeof meta?.request_id === 'string' ? meta.request_id : requestId;
  if (!('data' in envelope)) {
    throw new GuidedApiError('API 成功响应缺少 data。', status, 'INVALID_ENVELOPE', resolvedRequestId, undefined, unknown);
  }
  return { data: envelope.data, requestId: resolvedRequestId };
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
}

export function parseRunEvents(text: string, runId: string): GuidedRunEvent[] {
  return text.replace(/\r\n/g, '\n').split(/\n\n+/).flatMap((block) => {
    const data = block.split('\n').filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).replace(/^ /, '')).join('\n');
    if (!data) return [];
    let payload: unknown;
    try {
      payload = JSON.parse(data);
    } catch {
      throw new GuidedApiError('Run 事件格式无效，未显示伪造事件。', 200, 'INVALID_EVENT');
    }
    const event = record(payload, 'Run event must be an object');
    const seq = int(event.seq, 'event.seq');
    if (seq < 0) invalid('event.seq must be non-negative');
    if (str(event.run_id, 'event.run_id') !== runId) invalid('event.run_id does not match requested Run');
    if (!event.payload || typeof event.payload !== 'object' || Array.isArray(event.payload)) invalid('event.payload must be an object');
    return [{
      schema_version: '1.0',
      run_id: runId,
      seq,
      type: str(event.type, 'event.type'),
      occurred_at: str(event.occurred_at, 'event.occurred_at'),
      payload: event.payload as Record<string, unknown>,
    }];
  });
}

export function createGuidedApi(actorId: string, fetcher: Fetcher = fetch): GuidedApi {
  if (!actorId) throw new GuidedApiError('actor ID is required', 0, 'INVALID_ACTOR');

  async function send(path: string, init: RequestInit, signal?: AbortSignal): Promise<Response> {
    const headers = new Headers(init.headers);
    headers.set('X-Actor-ID', actorId);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json, text/event-stream');
    try {
      return await fetcher(`${BASE}${path}`, {
        ...init,
        headers,
        credentials: 'same-origin',
        cache: 'no-store',
        ...(signal ? { signal } : {}),
      });
    } catch (error) {
      if (isAbort(error, signal)) throw error;
      throw new GuidedApiError(
        '网络连接失败；写入结果可能未知，请先查询 receipt，不要自动重试。',
        0,
        'NETWORK_ERROR',
        undefined,
        undefined,
        true,
      );
    }
  }

  async function readEnvelope(path: string, signal?: AbortSignal, init: RequestInit = { method: 'GET' }): Promise<{ data: unknown; requestId?: string }> {
    const response = await send(path, init, signal);
    const payload = await responsePayload(response);
    if (!response.ok) throw errorEnvelope(payload, response);
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    const parsed = envelopeData(payload, response.status, requestId);
    return parsed as { data: unknown; requestId?: string };
  }

  async function read<T>(path: string, validator: GuidedValidator<T>, signal?: AbortSignal): Promise<T> {
    const { data } = await readEnvelope(path, signal);
    return validator(data);
  }

  async function readArray<T>(path: string, validator: GuidedValidator<T>, signal?: AbortSignal): Promise<GuidedPage<T>> {
    const response = await send(path, { method: 'GET' }, signal);
    const payload = await responsePayload(response);
    if (!response.ok) throw errorEnvelope(payload, response);
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    const parsed = envelopeData(payload, response.status, requestId) as { data: unknown; requestId?: string };
    if (!Array.isArray(parsed.data)) {
      throw new GuidedApiError('分页 data 必须是真实数组。', response.status, 'INVALID_ENVELOPE', parsed.requestId);
    }
    const envelope = payload as Record<string, unknown>;
    if (!('page' in envelope)) {
      return { items: parsed.data.map(validator), has_more: false, next_cursor: null };
    }
    const page = record(envelope.page, 'page must be an object');
    const hasMore = boolean(page.has_more, 'page.has_more');
    if (!('next_cursor' in page)) invalid('page.next_cursor must be present');
    const cursor = page.next_cursor;
    if (cursor !== null && typeof cursor !== 'string') invalid('page.next_cursor must be string or null');
    // Opaque means uninterpreted: even an all-digit string is a valid token.
    return { items: parsed.data.map(validator), has_more: hasMore, next_cursor: cursor };
  }

  async function write<T>(intent: WriteIntent, validator: GuidedValidator<T>): Promise<T> {
    if (intent.state === 'accepted') {
      throw new GuidedApiError('accepted intent cannot be written again', 409, 'INTENT_ACCEPTED');
    }
    if (intent.state === 'unknown') {
      throw new GuidedApiError('unknown intent must be queried through receipt before reuse', 409, 'INTENT_UNKNOWN');
    }
    if (intent.key.length === 0 || intent.path.length === 0 || typeof intent.body !== 'string') {
      invalid('write intent must contain key, path and body string');
    }
    const headers = new Headers({ 'Content-Type': 'application/json', 'Idempotency-Key': intent.key });
    const response = await send(intent.path, { method: 'POST', headers, body: intent.body });
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    if (!response.ok) {
      const payload = await responsePayload(response, response.status >= 500);
      throw errorEnvelope(payload, response);
    }
    try {
      const payload = await responsePayload(response, true);
      const parsed = envelopeData(payload, response.status, requestId, true) as { data: unknown };
      return validator(parsed.data);
    } catch (error) {
      throw asUnknown(
        error,
        `写请求已返回成功状态但响应无法验证；写入结果可能未知。请先查询 receipt。(${response.status})`,
        response.status,
        'UNKNOWN_WRITE_RESULT',
        requestId,
      );
    }
  }

  async function receipt(key: string, signal?: AbortSignal): Promise<GuidedReceipt> {
    if (!key) invalid('receipt key must be non-empty');
    return read(`/demo/write-receipts/${encodeURIComponent(key)}`, validateReceipt, signal);
  }

  async function download(item: GuidedDownloadRegistration, signal?: AbortSignal): Promise<void> {
    if (!HASH.test(item.sha256)) invalid('download sha256 must be a registered 64-character hash');
    if (!Number.isSafeInteger(item.bytes) || item.bytes < 0) invalid('download bytes must be a non-negative integer');
    if (typeof item.media_type !== 'string' || !item.media_type.includes('/')) invalid('download media type must be registered');
    if (typeof item.filename !== 'string' || item.filename.length === 0 || item.filename.includes('/') || item.filename.includes('\\') || item.filename.includes('\0')) {
      invalid('download filename must be a registered safe filename');
    }
    const response = await send(`/artifacts/${encodeURIComponent(item.artifact_id)}/download`, {
      method: 'GET',
      headers: { Accept: 'application/octet-stream' },
    }, signal);
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw errorEnvelope(payload, response);
    }
    const responseMedia = (response.headers.get('Content-Type') ?? '').split(';', 1)[0].trim().toLowerCase();
    const expectedMedia = item.media_type.split(';', 1)[0].trim().toLowerCase();
    if (!responseMedia || responseMedia !== expectedMedia) invalid('download MIME does not match registration');
    const length = response.headers.get('Content-Length');
    if (length !== null && (!INTEGER.test(length) || Number(length) !== item.bytes)) invalid('download length does not match registration');
    const buffer = new Uint8Array(await response.arrayBuffer());
    if (buffer.byteLength !== item.bytes) invalid('download bytes do not match registration');
    if ((await sha256Hex(buffer)) !== item.sha256) invalid('download SHA-256 does not match registration');

    const blob = new Blob([buffer], { type: item.media_type });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = item.filename;
    anchor.rel = 'noopener';
    anchor.click();
    URL.revokeObjectURL(objectUrl);
  }

  async function runEvents(runId: string, after: number, signal?: AbortSignal): Promise<GuidedRunEvent[]> {
    if (!Number.isSafeInteger(after) || after < 0) invalid('event after must be a non-negative integer');
    const response = await send(`/runs/${encodeURIComponent(runId)}/events?after=${after}`, { method: 'GET' }, signal);
    if (!response.ok) {
      const payload = await responsePayload(response);
      throw errorEnvelope(payload, response);
    }
    return parseRunEvents(await response.text(), runId);
  }

  // Direct writes for the real article flow. Unlike the demo intents these are
  // never persisted by the caller: each method takes an optional caller-held
  // Idempotency-Key so an in-memory retry can reuse the exact key + body after
  // an unknown outcome, while a fresh submission mints a fresh key.
  function directIntent(path: string, body: unknown, key?: string): WriteIntent {
    return { key: key ?? crypto.randomUUID(), path, body: JSON.stringify(body), state: 'prepared' };
  }

  async function createProject(name: string, key?: string): Promise<GuidedProjectAccepted> {
    return write(directIntent(GUIDED_PATHS.projects, { name, description: 'Guided real article flow' }, key), validateProjectAccepted);
  }

  async function uploadPaperPdf(projectId: string, file: File, options: { titleHint?: string; key?: string } = {}): Promise<GuidedPaperAccepted> {
    const form = new FormData();
    form.append('file', file, file.name);
    if (options.titleHint) form.append('title_hint', options.titleHint);
    form.append('source_processing_consent', 'true');
    // The browser must set the multipart boundary itself; do not preset
    // Content-Type here. The Idempotency-Key still pins the submission.
    const response = await send(GUIDED_PATHS.projectPapers(projectId), {
      method: 'POST',
      headers: { 'Idempotency-Key': options.key ?? crypto.randomUUID() },
      body: form,
    });
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    if (!response.ok) {
      throw errorEnvelope(await responsePayload(response, response.status >= 500), response);
    }
    try {
      const parsed = envelopeData(await responsePayload(response, true), response.status, requestId, true) as { data: unknown };
      return validatePaperAccepted(parsed.data);
    } catch (error) {
      throw asUnknown(error, '上传已返回成功状态但响应无法验证；结果可能未知，请勿重复上传。', response.status, 'UNKNOWN_WRITE_RESULT', requestId);
    }
  }

  async function createTextPaper(projectId: string, input: { text: string; titleHint?: string; key?: string }): Promise<GuidedPaperAccepted> {
    const body: Record<string, unknown> = { text: input.text, source_processing_consent: true };
    if (input.titleHint) body.title_hint = input.titleHint;
    return write(directIntent(GUIDED_PATHS.projectPapersText(projectId), body, input.key), validatePaperAccepted);
  }

  async function getPaper(paperId: string, signal?: AbortSignal): Promise<GuidedPaperInfo> {
    return read(GUIDED_PATHS.paper(paperId), validatePaperInfo, signal);
  }

  async function createProviderSession(input: { base_url: string; model_id: string; api_key: string; endpoint_alias: string; key?: string }): Promise<GuidedProviderSessionSummary> {
    return write(directIntent(GUIDED_PATHS.providerSessions, {
      base_url: input.base_url,
      model_id: input.model_id,
      api_key: input.api_key,
      endpoint_alias: input.endpoint_alias,
      revision: '1',
    }, input.key), validateProviderSessionSummary);
  }

  async function deleteProviderSession(providerSessionId: string, key?: string): Promise<{ provider_session_id: string; secret_configured: boolean }> {
    const response = await send(GUIDED_PATHS.providerSession(providerSessionId), {
      method: 'DELETE',
      headers: { 'Idempotency-Key': key ?? crypto.randomUUID() },
    });
    if (!response.ok) {
      throw errorEnvelope(await responsePayload(response, response.status >= 500), response);
    }
    try {
      const parsed = envelopeData(await responsePayload(response, true), response.status, response.headers.get('X-Request-ID') ?? undefined, true) as { data: unknown };
      const item = record(parsed.data, 'provider session clear must be an object');
      return {
        provider_session_id: str(item.provider_session_id, 'provider session clear.provider_session_id'),
        secret_configured: boolean(item.secret_configured, 'provider session clear.secret_configured'),
      };
    } catch (error) {
      if (error instanceof GuidedApiError && !error.unknown) throw error;
      throw asUnknown(error, '清除会话已返回成功状态但响应无法验证；请重新查询会话状态。', response.status, 'UNKNOWN_WRITE_RESULT');
    }
  }

  async function createExtraction(paperId: string, input: { reason: string; provider_session_id: string | null; key?: string }): Promise<GuidedExtractionAccepted> {
    const body: Record<string, unknown> = { reason: input.reason };
    if (input.provider_session_id !== null) body.provider_session_id = input.provider_session_id;
    return write(directIntent(GUIDED_PATHS.paperExtractions(paperId), body, input.key), validateExtractionAccepted);
  }

  async function getExtraction(paperId: string, signal?: AbortSignal): Promise<GuidedExtractionReadback | null> {
    const { data } = await readEnvelope(GUIDED_PATHS.paperExtraction(paperId), signal);
    const item = record(data, 'extraction readback must be an object');
    // A paper with no extraction yet answers 202 {paper_id, operation_id, status}
    // and must not be rendered as a ready extraction.
    if (item.extraction_id === null || item.extraction_id === undefined || item.schema_version === undefined) return null;
    const parsed = validateExtractionReadback(data);
    if (parsed.paper_id !== paperId) invalid('extraction readback does not match the requested paper');
    return parsed;
  }

  async function createEnvironmentCheck(projectId: string, experimentId: string, input: { revision_id: string; requirements?: Record<string, unknown>; key?: string }): Promise<GuidedEnvironmentCheck> {
    const value = await write(directIntent(GUIDED_PATHS.environmentChecks(projectId, experimentId), {
      revision_id: input.revision_id,
      requirements: input.requirements ?? {},
    }, input.key), validateEnvironmentCheck);
    if (value.project_id !== projectId || value.experiment_id !== experimentId || value.revision_id !== input.revision_id) throw Error('RESOURCE_MISMATCH');
    return value;
  }

  async function createSshVerification(environmentCheckId: string, input: { host: string; port: number; user: string; workdir: string; auth: { type: 'agent'; agent_id: string } | { type: 'key_reference'; key_reference: string }; key?: string }): Promise<GuidedSSHVerification> {
    const value = await write(directIntent(GUIDED_PATHS.sshVerifications(environmentCheckId), {
      target: { host: input.host, port: input.port, user: input.user, workdir: input.workdir, auth: input.auth },
      user_selected: true,
    }, input.key), validateSSHVerification);
    if (value.environment_check_id !== environmentCheckId) throw Error('RESOURCE_MISMATCH');
    return value;
  }

  async function createCodegen(experimentId: string, input: { provider_session_id: string | null; reason: string; prompt_version?: string; key?: string }): Promise<GuidedCodegenAccepted> {
    const body: Record<string, unknown> = { reason: input.reason };
    // A null session is omitted (same rule as createExtraction), never sent as null.
    if (input.provider_session_id !== null) body.provider_session_id = input.provider_session_id;
    if (input.prompt_version) body.prompt_version = input.prompt_version;
    return write(directIntent(GUIDED_PATHS.experimentCodegen(experimentId), body, input.key), validateCodegenAccepted);
  }

  async function readAttemptCode(experimentId: string, attemptIndex: number, signal?: AbortSignal): Promise<GuidedAttemptCode> {
    if (!Number.isSafeInteger(attemptIndex) || attemptIndex < 0) invalid('attempt index must be a non-negative integer');
    return read(GUIDED_PATHS.attemptCode(experimentId, attemptIndex), validateAttemptCode, signal);
  }

  return { read, readArray, write, receipt, download, runEvents, createProject, uploadPaperPdf, createTextPaper, getPaper, createProviderSession, deleteProviderSession, createExtraction, getExtraction, createEnvironmentCheck, createSshVerification, createCodegen, readAttemptCode };
}
