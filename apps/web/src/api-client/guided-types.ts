// Narrow Guided Demo wire DTOs for the observed local API only.
// This file intentionally does not import mock fixtures or the legacy Session API.

export type Step = 'paper' | 'extraction' | 'experiment' | 'run' | 'report' | 'export';

export interface WriteIntent {
  key: string;
  path: string;
  body: string;
  state: 'prepared' | 'unknown' | 'accepted';
}

export type GuidedRunStatus =
  | 'created'
  | 'queued'
  | 'running'
  | 'evaluating'
  | 'cancel_requested'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'awaiting_revision';

export interface GuidedError {
  code: string;
  message: string;
}

export interface GuidedRun {
  run_id: string;
  project_id: string;
  experiment_id: string;
  revision_id: string;
  status: GuidedRunStatus;
  last_event_seq: number;
  created_at: string;
  updated_at: string;
  error: GuidedError | null;
}

export interface GuidedPage<T> {
  items: T[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface GuidedReceipt {
  state: 'pending' | 'completed';
  target_path: string;
  request_sha256: string;
  response: unknown;
}

export interface GuidedBootstrap {
  project_id: string;
  paper_id: string;
  extraction_id: string;
}

export interface GuidedExperiment {
  experiment_id: string;
  project_id: string;
  paper_id: string;
  status: 'draft' | 'ready' | 'active' | 'needs_revision' | 'closed';
  head_revision_id: string | null;
  active_run_id: string | null;
  latest_run_id: string | null;
  repairs_used: number;
  repairs_limit: number;
  created_at: string;
}

export interface GuidedRevision {
  id: string;
  experiment_id: string;
  revision_no: number;
  spec_hash: string;
  policy_hash: string;
  spec: Record<string, unknown>;
  validation: { issues?: unknown };
  comparison_precheck?: unknown;
  budget_estimate?: unknown;
  confirmation: {
    confirmation_id: string;
    revision_id: string;
    spec_hash: string;
    confirmed_by: string;
    confirmed_at: string;
  } | null;
  is_current: boolean;
  created_at: string;
}

export interface GuidedConfirmation {
  confirmation_id: string;
  revision_id: string;
  spec_hash: string;
  confirmed_by: string;
  confirmed_at: string;
}

export interface GuidedRunAccepted {
  run_id: string;
  status: 'queued';
  events_url: string;
  last_event_seq: number;
}

export interface GuidedRunEvent {
  schema_version: '1.0';
  run_id: string;
  seq: number;
  type: string;
  occurred_at: string;
  payload: Record<string, unknown>;
}

export interface GuidedAttempt {
  attempt_id: string;
  run_id: string;
  attempt_index: number;
  repair_ordinal: number;
  source?: string;
  status: string;
  base_code_hash?: string | null;
  patch_hash?: string | null;
  created_at: string;
  updated_at: string;
}

export interface GuidedTextChunk {
  artifact_id: string;
  text: string;
  offset: number;
  next_offset: number;
  eof: boolean;
}

export interface GuidedOperation {
  operation_id: string;
  kind: string;
  resource_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  error: GuidedError | null;
}

export type GuidedReportState = 'pending' | 'generating' | 'ready' | 'failed';

export interface GuidedReportStatus {
  run_id: string;
  status: GuidedReportState | null;
  build_id: string | null;
  operation_id: string | null;
  report_hash: string | null;
  error: GuidedError | null;
}

export interface GuidedReportBuildAccepted {
  build_id: string | null;
  operation_id: string | null;
  run_id: string;
  status: GuidedReportState;
}

export type GuidedExportFormat = 'report_json' | 'report_markdown' | 'bundle_zip' | 'report_pdf';

export interface GuidedExportItem {
  format: GuidedExportFormat;
  status: 'pending' | 'running' | 'ready' | 'failed';
  artifact_id: string | null;
  filename: string | null;
  bytes: number | null;
  media_type: string | null;
  sha256: string | null;
  error: GuidedError | null;
}

export interface GuidedExport {
  id: string;
  run_id: string;
  status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed';
  operation_id: string | null;
  report_hash: string | null;
  items: GuidedExportItem[];
  created_at: string;
}

export interface GuidedExportAccepted {
  export_id: string;
  operation_id: string;
  status: 'queued';
}

export interface GuidedDownloadRegistration {
  artifact_id: string;
  filename: string;
  bytes: number;
  sha256: string;
  media_type: string;
}

// --- Real article flow (paper upload → provider extraction → environment) ---
// Narrow wire DTOs for the phase-02 endpoints. All writes carry an
// Idempotency-Key; none of these bodies may contain anything the caller must
// not re-send, except the provider session api_key which is kept in React
// state only (never localStorage) and re-typed after every refresh.

export interface GuidedProjectAccepted {
  id: string;
  name: string;
  role: string;
  version: number;
  created_at: string;
}

export interface GuidedPaperAccepted {
  paper_id: string;
  operation_id: string | null;
  status: string;
}

export interface GuidedPaperInfo {
  paper_id: string;
  project_id: string;
  title: string | null;
  status: string;
  sha256: string;
  page_count: number | null;
  content_available: boolean;
  current_extraction_id: string | null;
  active_operation_id: string | null;
  error: GuidedError | null;
  created_at: string;
}

export interface GuidedProviderSessionSummary {
  provider_session_id: string;
  endpoint_alias: string;
  model_id: string;
  base_url: string;
  secret_configured: boolean;
  secret_last4: string;
}

export interface GuidedExtractionAccepted {
  paper_id: string;
  operation_id: string | null;
  status: string;
  extraction_id: string | null;
}

export interface GuidedSourceRef {
  paper_id: string | null;
  pdf_sha256: string | null;
  page: number | null;
  section: string | null;
  table: number | null;
  row: number | null;
  quote: string | null;
  bbox: unknown;
}

export type GuidedExecutionSupport = 'supported' | 'requires_review' | 'unsupported';

export interface GuidedCandidateExperiment {
  id: string | null;
  candidate_id: string | null;
  label: string;
  recipe_id: string | null;
  recipe_version: string | null;
  support_status: string | null;
  execution_support: GuidedExecutionSupport;
  unsupported_reasons: string[];
  parameters: Record<string, unknown>;
  required_dependencies: unknown[];
  required_datasets: unknown[];
  resource_requirements: Record<string, unknown>;
  estimated_runtime: unknown;
  source_refs: GuidedSourceRef[];
  provenance: string | null;
}

export interface GuidedExtractionField {
  value: unknown;
  evidence_status: string | null;
  source_refs: unknown[];
}

export interface GuidedExtractionReadback {
  extraction_id: string | null;
  paper_id: string;
  schema_version: string;
  fields: Record<string, GuidedExtractionField>;
  methods: Array<Record<string, unknown>>;
  formulas: Array<Record<string, unknown>>;
  candidate_experiments: GuidedCandidateExperiment[];
  source_refs: GuidedSourceRef[];
  warnings: string[];
  provider: Record<string, unknown>;
}

export type GuidedEnvironmentCheckStatus = 'pass' | 'fail' | 'unknown' | 'not_needed';
export type GuidedEnvironmentDecision = 'ready' | 'blocked' | 'unsupported';

export interface GuidedEnvironmentCheckItem {
  key: string;
  status: GuidedEnvironmentCheckStatus;
  required: boolean;
  observed: unknown;
  required_value: unknown;
  suggestion: string;
}

export interface GuidedEnvironmentCheck {
  environment_check_id: string;
  project_id: string;
  experiment_id: string;
  revision_id: string;
  decision: GuidedEnvironmentDecision;
  status: GuidedEnvironmentDecision;
  checker_version: string;
  requirements: Record<string, unknown>;
  items: GuidedEnvironmentCheckItem[];
  repair_suggestions: string[];
  created_at: string;
}

export interface GuidedSSHVerification {
  ssh_verification_id: string;
  environment_check_id: string;
  project_id: string;
  experiment_id: string;
  revision_id: string;
  status: string;
  target: Record<string, unknown>;
  summary: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export type GuidedSSHAuthInput =
  | { type: 'agent'; agent_id: string }
  | { type: 'key_reference'; key_reference: string };

export interface GuidedSSHTargetInput {
  host: string;
  port: number;
  user: string;
  workdir: string;
  auth: GuidedSSHAuthInput;
}

// Optional spec.training overrides for the real flow. Empty string means
// "leave null and let the registered recipe default decide".
export interface GuidedTrainingForm {
  seed: string;
  max_epochs: string;
  learning_rate: string;
  batch_size: string;
}

// --- sandbox reproduction code (recipe sandbox_python) ---
// Cross-task contract: POST /experiments/{eid}/codegen answers
// {attempt_id, attempt_index, code, code_sha256}; notes is an optional extra
// the generator may attach (absent maps to null, never a made-up value).
export interface GuidedCodegenAccepted {
  attempt_id: string;
  attempt_index: number;
  code: string;
  code_sha256: string;
  notes: string | null;
}

// GET /experiments/{eid}/attempts/{attempt_index}/code readback. The path
// already identifies the attempt, so the body only needs to carry the code.
export interface GuidedAttemptCode {
  code: string;
  code_sha256: string;
  notes: string | null;
}
