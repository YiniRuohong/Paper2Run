// 实验报告（task.report）的类型守卫。
// 结构以 packages/orchestration/p2r_orchestration/autonomous/evidence.py 的
// report() 输出为准（2026-11-05 核对）：status / completion_gate /
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

export interface ExperimentReport {
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

// 返回 null 表示结构不认识；调用方降级展示原始 JSON。
export function parseReport(value: unknown): ExperimentReport | null {
  const item = rec(value);
  if (!item) return null;
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
