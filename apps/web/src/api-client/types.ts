// Paper2Run 公共 API 契约类型 · 逐字对齐 docs/engineering/09-前后端公共接口契约.md v0.2
// 后端交付 OpenAPI 生成类型后，本文件由生成物替换；组件只消费类型，不改字段。

export type ID = string;
export type Hash = string;
export type UtcTime = string;
export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue };
export type Role = 'owner' | 'editor' | 'viewer';
export type DataMode = 'mock' | 'live' | 'replay';
export type ExperimentMode = 'smoke' | 'scaled' | 'reference';

export interface Meta { request_id: string }
export interface Ok<T> { data: T; meta: Meta }
export interface FieldError { path: string; code: string; message: string }
export interface ApiError {
  code: string;
  message: string;
  retryable: boolean;
  details: {
    field_errors?: FieldError[];
    operation_id?: ID;
    revision_id?: ID;
    latest_revision_id?: ID;
    [key: string]: JsonValue | FieldError[] | undefined;
  };
}

export interface SessionInfo {
  user: { id: ID; display_name: string };
  session: { expires_at: UtcTime; idle_expires_at: UtcTime };
  csrf_token: string;
  permissions: { can_create_project: boolean };
  memberships: { project_id: ID; role: Role }[];
}

export interface RecipeCapability {
  id: string; version: string; label: string;
  modes: ExperimentMode[]; dataset_ids: string[];
  execution_support: 'available' | 'unavailable'; reason: string | null;
}
export interface Capabilities {
  contract_version: '0.2';
  execution_backend: 'mock' | 'local' | 'remote'; data_mode: DataMode;
  writes_enabled: boolean;
  upload: { max_pdf_bytes: number; max_pages: number; arxiv_enabled: boolean };
  limits: { max_repairs: number; max_queued_runs_per_user: number };
  recipes: RecipeCapability[];
  models: { purpose: 'parse' | 'plan' | 'code'; display_name: string; available: boolean }[];
}

export interface Project {
  id: ID; name: string; description: string; role: Role;
  version: number; deletion_state: 'active' | 'deleting' | 'deleted';
  created_at: UtcTime; updated_at: UtcTime;
}
export interface ProjectCreate { name: string; description?: string }

export type OperationStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
export interface Operation {
  id: ID; kind: 'paper_ingest' | 'paper_extract' | 'report_build' | 'export_build' | 'project_purge';
  status: OperationStatus;
  resource: { type: 'paper' | 'report' | 'export' | 'project'; id: ID };
  stage: string | null;
  progress: { completed_units: number; total_units: number } | null;
  error: ApiError | null; updated_at: UtcTime;
}
export interface OperationAccepted { operation_id: ID; status: 'queued' | 'running' }

export type PaperStatus = 'uploaded' | 'parsing' | 'ready' | 'failed' | 'unsupported';
export interface Paper {
  paper_id: ID; project_id: ID; title: string | null;
  status: PaperStatus; sha256: Hash | null; arxiv_version: string | null;
  page_count: number | null; content_available: boolean;
  current_extraction_id: ID | null; active_operation_id: ID | null;
  processing_consent: { accepted_at: UtcTime; policy_version: string };
  error: ApiError | null; created_at: UtcTime;
}
export interface PaperAccepted {
  paper_id: ID; operation_id: ID; status: 'uploaded' | 'parsing';
}

export type EvidenceStatus = 'supported' | 'inferred' | 'missing' | 'conflicting';
export interface ExtractedField<T> { value: T | null; evidence_status: EvidenceStatus; source_refs: ID[] }
export interface SourceRef {
  id: ID; paper_id: ID; pdf_sha256: Hash; page: number;
  section: string | null; table: string | null; row: string | null;
  quote: string;
  bbox: [number, number, number, number] | null;
}
export interface CandidateExperiment {
  id: ID; label: string; source_refs: ID[];
  recipe_id: string | null; recipe_version: string | null;
  execution_support: 'supported' | 'requires_review' | 'unsupported';
  unsupported_reasons: string[];
}
export interface Extraction {
  extraction_id: ID; paper_id: ID; schema_version: '1.0';
  fields: {
    task: ExtractedField<string>; dataset: ExtractedField<string>; optimizer: ExtractedField<string>;
  };
  methods: { id: ID; label: string; source_refs: ID[] }[];
  formulas: { id: ID; latex: string; source_refs: ID[] }[];
  candidate_experiments: CandidateExperiment[]; source_refs: SourceRef[];
  warnings: string[];
}

export type ExperimentStatus = 'draft' | 'ready' | 'active' | 'needs_revision' | 'closed';
export interface BudgetLimit {
  max_model_calls: number; max_tokens: number; max_execution_seconds: number;
  max_artifact_bytes: number; currency: 'CNY'; max_cost_micros: number;
}
export interface Issue { code: string; message: string; path: string | null; severity: 'warning' | 'blocking' }
export interface ComparisonPrecheck { status: 'eligible' | 'not_comparable' | 'unresolved'; reasons: string[] }
export interface MetricTarget {
  id: ID; metric: string; unit: 'percent' | 'ratio' | 'count' | 'scalar';
  direction: 'lower' | 'higher'; reference_value: number | null;
  source_ref: ID | null; statistic: 'single_reported' | 'mean' | 'best' | 'unknown';
  tolerance: number | null; weight: number;
}
export interface ExperimentSpec {
  schema_version: '1.0'; paper_id: ID; extraction_id: ID; candidate_experiment_id: ID;
  recipe: { id: string; version: string }; mode: ExperimentMode;
  method: { family: string; variant: string | null; must_include: string[] };
  dataset: {
    id: string | null; version: string | null; split_policy_id: string | null;
    train_subset_fraction: number | null;
  };
  training: {
    seed: number | null; batch_size: number | null; max_steps: number | null;
    max_epochs: number | null; optimizer: string | null;
    learning_rate: number | null; weight_decay: number | null;
  };
  targets: MetricTarget[]; budget: BudgetLimit;
  recipe_config: { [key: string]: JsonValue };
  assumptions: { path: string; description: string; source_refs: ID[] }[];
  user_changes: { path: string; reason: string }[];
}
export interface EditableField {
  path: string;
  kind: 'integer' | 'number' | 'select' | 'boolean';
  label: string; required: boolean; nullable: boolean;
  min: number | null; max: number | null;
  options: { value: string | number | boolean; label: string }[] | null;
}
export interface SpecDraft {
  experiment_id: ID; base_revision_id: ID | null; policy_hash: Hash;
  spec: ExperimentSpec; editable_fields: EditableField[];
  issues: Issue[]; comparison_precheck: ComparisonPrecheck;
}
export interface Confirmation {
  confirmation_id: ID; revision_id: ID; spec_hash: Hash;
  confirmed_by: ID; confirmed_at: UtcTime;
}
export interface SpecRevision {
  id: ID; experiment_id: ID; revision_no: number; spec_hash: Hash; policy_hash: Hash;
  spec: ExperimentSpec; validation: { issues: Issue[] };
  comparison_precheck: ComparisonPrecheck;
  budget_estimate: { currency: 'CNY'; amount_micros: number | null; duration_seconds: number | null; basis: string };
  confirmation: Confirmation | null; is_current: boolean; created_at: UtcTime;
}
export interface Experiment {
  experiment_id: ID; project_id: ID; paper_id: ID; status: ExperimentStatus;
  head_revision_id: ID | null; active_run_id: ID | null; latest_run_id: ID | null;
  repairs_used: number; repairs_limit: number; created_at: UtcTime;
}
export interface ExperimentCreate {
  paper_id: ID; extraction_id: ID; candidate_experiment_id: ID;
  recipe_id: string; recipe_version: string;
}
export interface RevisionCreate { experiment_id: ID; base_revision_id: ID | null; spec: ExperimentSpec }
export interface RevisionConfirm {
  spec_hash: Hash; policy_hash: Hash; accepted_issue_codes: string[]; accept_budget: true;
}
export interface RunCreate {
  experiment_id: ID; spec_revision_id: ID; confirmation_id: ID; parent_run_id: ID | null;
}
export interface RunAccepted { run_id: ID; status: 'queued'; events_url: string; last_event_seq: number }

export type RunStatus =
  | 'queued' | 'generating' | 'executing' | 'repairing' | 'verifying'
  | 'cancel_requested' | 'completed' | 'failed' | 'cancelled' | 'awaiting_revision';
export type RunStage =
  | 'plan_build' | 'code_generate' | 'code_validate' | 'smoke_execute'
  | 'train_execute' | 'predict_execute' | 'metrics_verify' | 'repair_propose';
export type ReportStatus = 'pending' | 'generating' | 'ready' | 'failed';
export interface RunBudget {
  currency: 'CNY'; limit_micros: number; actual_micros: number | null;
  reserved_micros: number; unknown_charges: boolean;
}
export interface Run {
  id: ID; project_id: ID; paper_id: ID; paper_title: string | null;
  experiment_id: ID; spec_revision_id: ID; parent_run_id: ID | null;
  status: RunStatus; stage: RunStage | null; version: number;
  last_event_seq: number; attempt_index: number | null;
  repairs_used: number; repairs_limit: number;
  mode: ExperimentMode; data_mode: DataMode;
  budget: RunBudget;
  resources: {
    cpu_millicores: number | null; ram_limit_bytes: number | null;
    gpu_slot: string | null; gpu_memory_used_bytes: number | null;
  };
  report_status: ReportStatus; terminal_reason: { code: string; message: string } | null;
  created_at: UtcTime; started_at: UtcTime | null; ended_at: UtcTime | null;
}
export interface Attempt {
  id: ID; run_id: ID; attempt_index: number; repair_ordinal: number;
  status: 'generated' | 'validating' | 'running' | 'failed' | 'succeeded';
  code_hash: Hash; code_manifest_id: ID;
  patch_artifact_id: ID | null; parent_attempt_id: ID | null;
  error_summary: { code: string; message: string } | null;
  diagnosis_summary: string | null; evidence_artifact_ids: ID[];
  log_artifact_ids: ID[]; created_at: UtcTime;
}
export interface CodeFile { path: string; artifact_id: ID; bytes: number; sha256: Hash; media_type: string }
export interface TextChunk {
  artifact_id: ID; text: string; offset: number; next_offset: number; eof: boolean;
}
export interface CurveSeries {
  id: ID; name: string; source: 'measured' | 'paper_digitized';
  metric: string; unit: string; x_axis: 'step' | 'epoch' | 'elapsed_seconds';
  points: { x: number; y: number | null }[]; evidence_artifact_id: ID;
}
export interface RunCurves {
  run_id: ID; attempt_id: ID | null; series: CurveSeries[];
  downsampled: boolean; warnings: string[];
}

export type Verdict = 'matched' | 'outside_tolerance' | 'not_comparable' | 'execution_failed' | 'insufficient_evidence';
export interface MetricRow {
  id: ID; name: string; unit: 'percent' | 'ratio' | 'count' | 'scalar';
  reference_value: number | null; observed_value: number | null;
  delta_absolute: number | null; delta_relative: number | null;
  tolerance: number | null; comparable: boolean; within_tolerance: boolean | null;
  source_ref: ID | null; evidence_artifact_ids: ID[];
}
export interface Report {
  id: ID; run_id: ID; version: number; mode: ExperimentMode; data_mode: DataMode;
  verdict: Verdict; comparison_reasons: string[];
  score: { policy_version: string; metric_match: number | null; process: number; environment: number; total: number | null };
  metrics: MetricRow[];
  evidence: { spec_revision_id: ID; manifest_artifact_id: ID; metric_result_id: ID | null };
  limitations: string[]; created_at: UtcTime;
}
export interface ReportPending {
  run_id: ID; status: 'waiting_for_run' | 'generating'; operation_id: ID | null; retry_after_ms: number;
}
export interface RunManifest {
  run_id: ID; status: 'provisional' | 'finalized'; schema_version: '1.0';
  spec_hash: Hash; paper_sha256: Hash; code_sha256: Hash | null; image_digest: string | null;
  seed: number | null; dataset: { id: string; version: string; sha256: Hash; split_sha256: Hash } | null;
  environment: { python: string | null; torch: string | null; cuda: string | null; gpu: string | null; driver: string | null };
  models: { purpose: string; model_id: string; prompt_version: string }[];
  evaluator_version: string | null;
  artifacts: { artifact_id: ID; kind: string; bytes: number; sha256: Hash }[];
  limitations: string[];
}
export type ExportFormat = 'bundle_zip' | 'report_pdf' | 'report_markdown' | 'report_json';
export interface ExportCreate { formats: ExportFormat[]; include_checkpoint?: boolean }
export interface ExportAccepted { export_id: ID; operation_id: ID; status: 'queued' }
export interface Export {
  id: ID; run_id: ID; status: 'queued' | 'running' | 'succeeded' | 'partial' | 'failed';
  operation_id: ID; created_at: UtcTime;
  items: { format: ExportFormat; status: 'pending' | 'running' | 'ready' | 'failed'; artifact_id: ID | null; filename: string | null; bytes: number | null; error: ApiError | null }[];
}

// UI 侧规范化分页（cursor 由服务端生成，前端只透传）
export interface PageData<T> { items: T[]; has_more: boolean; next_cursor: string | null }
