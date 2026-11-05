// 实验报告（task.report）的类型守卫。
// 结构以 packages/orchestration/p2r_orchestration/autonomous/evidence.py 的
// report() 输出为准（2026-11-05 核对）：outcome / outcome_reason /
// outcome_snapshot_sha256 / candidate / reviews（均可缺省，旧报告没有）/ status / completion_gate /
// execution_observations / evaluation / verification / artifacts /
// missing_* / protocol / reference_values / model_narrative / limitations / sha256。
// 后端冻结 DTO（契约 C4）之前，任何字段对不上就整体返回 null，
// 页面降级为显示原始 JSON，绝不脑补结构。

const rec = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : null;
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);
const strAllowEmpty = (v: unknown): string | null => (typeof v === 'string' ? v : null);
const bool = (v: unknown): boolean | null => (typeof v === 'boolean' ? v : null);
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null);
const strList = (v: unknown): string[] | null =>
  Array.isArray(v) && v.every(x => typeof x === 'string') ? v as string[] : null;

export interface ReportCompletionGate {
  entrypoint: string | null;
  frozen_code_sha256: string | null;
  rule: string;
}

export interface ExecutionObservation {
  sequence: number;
  tool: string;
  command: string;
  exitCode: number | null;
  matchesEntrypoint: boolean;
}

export interface ReportEvaluation {
  verdict: string;
  comparisonEligible: boolean;
  reasons: string[];
  metrics: Record<string, number>;
  confidenceIntervals: Record<string, { lower: number; upper: number }>;
  withinTolerance: boolean | null;
}

export interface ReportArtifact {
  name: string;
  kind: string;
  sha256: string;
  bytes: number | null;
  protocolSha256: string | null;
}

export interface VerificationComparison {
  metric: string;
  scope: string;
  referenceValue: number | null;
  tolerance: number | null;
  observedValue: number | null;
  absoluteDifference: number | null;
  withinTolerance: boolean | null;
  verdict: string;
  sourceUrl: string | null;
}

export interface ReportVerification {
  scope: string;
  verdict: string;
  comparisons: VerificationComparison[];
  limitations: string[];
}

export interface CandidateArtifactView {
  name: string;
  kind: string;
  sha256: string;
  bytes: number | null;
  round: number | null;
  protocolSha256: string | null;
}

/** 冻结候选（任务 DTO 的 candidate 与报告的 deciding candidate 同构）。 */
export interface CandidateView {
  snapshotSha256: string;
  round: number | null;
  protocolSha256: string | null;
  artifacts: CandidateArtifactView[];
  run: { jobId: string | null; exitCode: number | null } | null;
  /** Pi 提交时的说明：模型叙述，不是执行证据。 */
  summary: string | null;
  reviewStatus: string | null;
  attempts: number | null;
}

export interface ReviewView {
  snapshotSha256: string;
  round: number | null;
  attempt: number | null;
  verdict: string;
  findings: Array<{ description: string; evidence: string[] }>;
}

export interface ExperimentReport {
  /** 结局字段为可选：旧报告没有它们。 */
  outcome: string | null;
  outcomeReason: string | null;
  outcomeSnapshotSha256: string | null;
  candidate: CandidateView | null;
  reviews: ReviewView[];
  status: string;
  verdict: string;
  completionGate: ReportCompletionGate;
  successfulExecutionObserved: boolean;
  executionObservations: ExecutionObservation[];
  evaluation: ReportEvaluation;
  verification: ReportVerification | null;
  artifacts: ReportArtifact[];
  missingArtifacts: string[];
  missingEvidenceKinds: string[];
  modelNarrative: string;
  limitations: string[];
  sha256: string;
}

function parseObservation(value: unknown): ExecutionObservation | null {
  const item = rec(value);
  if (!item) return null;
  const sequence = num(item.sequence);
  const tool = str(item.tool);
  const result = rec(item.result);
  if (sequence === null || tool === null || result === null) return null;
  return {
    sequence,
    tool,
    command: strAllowEmpty(item.command) ?? '',
    exitCode: num(result.exit_code),
    matchesEntrypoint: item.matches_protocol_entrypoint === true,
  };
}

function parseEvaluation(value: unknown): ReportEvaluation | null {
  const item = rec(value);
  if (!item) return null;
  const verdict = str(item.verdict);
  const eligible = bool(item.comparison_eligible);
  const reasons = strList(item.reasons);
  const metrics = rec(item.metrics);
  if (verdict === null || eligible === null || reasons === null || metrics === null) return null;
  const numericMetrics: Record<string, number> = {};
  for (const [k, v] of Object.entries(metrics)) {
    const parsed = num(v);
    if (parsed !== null) numericMetrics[k] = parsed;
  }
  const intervals: ReportEvaluation['confidenceIntervals'] = {};
  for (const [k, v] of Object.entries(rec(item.confidence_intervals) ?? {})) {
    const ci = rec(v);
    const lower = ci && num(ci.lower);
    const upper = ci && num(ci.upper);
    if (lower !== null && upper !== null) intervals[k] = { lower, upper };
  }
  return {
    verdict,
    comparisonEligible: eligible,
    reasons,
    metrics: numericMetrics,
    confidenceIntervals: intervals,
    withinTolerance: bool(item.within_tolerance),
  };
}

function parseArtifact(value: unknown): ReportArtifact | null {
  const item = rec(value);
  if (!item) return null;
  const name = str(item.name);
  const kind = str(item.kind);
  const sha256 = str(item.sha256);
  if (name === null || kind === null || sha256 === null) return null;
  return { name, kind, sha256, bytes: num(item.bytes), protocolSha256: str(item.protocol_sha256) };
}

function parseVerification(value: unknown): ReportVerification | null {
  const item = rec(value);
  if (!item) return null;
  const scope = strAllowEmpty(item.scope);
  const verdict = str(item.verdict);
  const limitations = strList(item.limitations);
  if (scope === null || verdict === null || limitations === null || !Array.isArray(item.comparisons)) return null;
  const comparisons: VerificationComparison[] = [];
  for (const raw of item.comparisons) {
    const c = rec(raw);
    if (!c) return null;
    const metric = str(c.metric);
    const cVerdict = str(c.verdict);
    if (metric === null || cVerdict === null) return null;
    comparisons.push({
      metric,
      scope: strAllowEmpty(c.scope) ?? scope,
      referenceValue: num(c.value),
      tolerance: num(c.tolerance),
      observedValue: num(c.observed_value),
      absoluteDifference: num(c.absolute_difference),
      withinTolerance: bool(c.within_tolerance),
      verdict: cVerdict,
      sourceUrl: str(c.source_url),
    });
  }
  return { scope, verdict, comparisons, limitations };
}

// 候选快照：snapshot_sha256 与 artifacts 必须对得上，其余字段缺失时显示未知。
export function parseCandidate(value: unknown): CandidateView | null {
  const item = rec(value);
  if (!item) return null;
  const snapshot = str(item.snapshot_sha256);
  if (snapshot === null || !Array.isArray(item.artifacts)) return null;
  const artifacts: CandidateArtifactView[] = [];
  for (const raw of item.artifacts) {
    const parsed = parseArtifact(raw);
    if (!parsed) return null;
    artifacts.push({ ...parsed, round: num(rec(raw)!.round) });
  }
  const run = rec(item.run);
  return {
    snapshotSha256: snapshot,
    round: num(item.round),
    protocolSha256: str(item.protocol_sha256),
    artifacts,
    run: run ? { jobId: str(run.job_id), exitCode: num(run.exit_code) } : null,
    summary: strAllowEmpty(item.summary),
    reviewStatus: str(item.review_status),
    attempts: num(item.attempts),
  };
}

// 复核记录列表：undefined / null 视为空；任何一条对不上就返回 null。
export function parseReviews(value: unknown): ReviewView[] | null {
  if (value === undefined || value === null) return [];
  if (!Array.isArray(value)) return null;
  const reviews: ReviewView[] = [];
  for (const raw of value) {
    const item = rec(raw);
    const snapshot = item && str(item.snapshot_sha256);
    const verdict = item && str(item.verdict);
    if (!item || !snapshot || !verdict || !Array.isArray(item.findings)) return null;
    const findings: ReviewView['findings'] = [];
    for (const f of item.findings) {
      const finding = rec(f);
      const description = finding && strAllowEmpty(finding.description);
      const evidence = finding && strList(finding.evidence);
      if (description === null || evidence === null) return null;
      findings.push({ description: description!, evidence: evidence! });
    }
    reviews.push({ snapshotSha256: snapshot, round: num(item.round), attempt: num(item.attempt), verdict, findings });
  }
  return reviews;
}

const optionalStr = (v: unknown): string | null | undefined => (v === undefined || v === null ? null : str(v) ?? undefined);

// 返回 null 表示结构不认识；调用方降级展示原始 JSON。
export function parseReport(value: unknown): ExperimentReport | null {
  const item = rec(value);
  if (!item) return null;
  const outcome = optionalStr(item.outcome);
  const outcomeReason = optionalStr(item.outcome_reason);
  const outcomeSnapshot = optionalStr(item.outcome_snapshot_sha256);
  if (outcome === undefined || outcomeReason === undefined || outcomeSnapshot === undefined) return null;
  const candidate = item.candidate === undefined || item.candidate === null ? null : parseCandidate(item.candidate);
  if (item.candidate != null && candidate === null) return null;
  const reviews = parseReviews(item.reviews);
  if (reviews === null) return null;
  const status = str(item.status);
  const verdict = str(item.verdict);
  const gate = rec(item.completion_gate);
  const observed = bool(item.successful_execution_observed);
  const evaluation = parseEvaluation(item.evaluation);
  const missingArtifacts = strList(item.missing_artifacts);
  const missingKinds = strList(item.missing_evidence_kinds);
  const limitations = strList(item.limitations);
  const sha256 = str(item.sha256);
  const narrative = strAllowEmpty(item.model_narrative);
  if (status === null || verdict === null || gate === null || observed === null || evaluation === null
    || missingArtifacts === null || missingKinds === null || limitations === null || sha256 === null || narrative === null) {
    return null;
  }
  const rule = str(gate.rule);
  if (rule === null) return null;
  if (!Array.isArray(item.execution_observations) || !Array.isArray(item.artifacts)) return null;
  const observations: ExecutionObservation[] = [];
  for (const raw of item.execution_observations) {
    const parsed = parseObservation(raw);
    if (!parsed) return null;
    observations.push(parsed);
  }
  const artifacts: ReportArtifact[] = [];
  for (const raw of item.artifacts) {
    const parsed = parseArtifact(raw);
    if (!parsed) return null;
    artifacts.push(parsed);
  }
  const verification = item.verification === null || item.verification === undefined ? null : parseVerification(item.verification);
  if (item.verification != null && verification === null) return null;
  return {
    outcome,
    outcomeReason,
    outcomeSnapshotSha256: outcomeSnapshot,
    candidate,
    reviews,
    status,
    verdict,
    completionGate: {
      entrypoint: str(gate.entrypoint),
      frozen_code_sha256: str(gate.frozen_code_sha256),
      rule,
    },
    successfulExecutionObserved: observed,
    executionObservations: observations,
    evaluation,
    verification,
    artifacts,
    missingArtifacts,
    missingEvidenceKinds: missingKinds,
    modelNarrative: narrative,
    limitations,
    sha256,
  };
}
