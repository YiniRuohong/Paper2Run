import type { GuidedExport } from '../../api-client/guided-types';
import type { Tone } from '../../ui/Card';
import type { WbFile } from '../../ui/workbench/tree';
import type { WbStageItem } from '../../ui/workbench/Workbench';
import { findCandidate, type RealFlowState, type RealFlowStep } from './realflow-state.ts';

// Presentation-only projection of the real-flow state into the workbench:
// every server record the flow has read becomes a read-only "file" (JSON or
// text, exactly as received), and every ready export becomes a real artifact
// that is fetched by its SHA-256. Nothing here is invented: a record appears
// only once the server returned it. The provider api_key never reaches state,
// so it can't reach these records either.

export const RUN_TONE: Record<string, Tone> = {
  queued: 'run', running: 'run', evaluating: 'run', cancel_requested: 'warn',
  completed: 'ok', failed: 'bad', cancelled: 'idle', awaiting_revision: 'warn', created: 'warn',
};

export interface FlowRecords {
  files: WbFile[];
  /** path → text for server records rendered as files. */
  text: Map<string, string>;
  /** path → artifact_id for exported artifacts (fetched as bytes). */
  artifacts: Map<string, string>;
  /** per-step sub-items for the timeline. */
  items: Partial<Record<RealFlowStep, WbStageItem[]>>;
}

const utf8 = new TextEncoder();
const json = (value: unknown) => JSON.stringify(value, null, 2);
const clock = (iso: string | null | undefined) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
};

export const FLOW_PATHS = {
  paper: '论文/paper.json',
  parse: '论文/parse-status.json',
  extraction: '抽取/extraction.json',
  spec: '方案/spec.json',
  environment: '环境/environment-check.json',
  ssh: '环境/ssh-verification.json',
  events: '运行/events.log',
  run: '运行/run.json',
  report: '报告/report.json',
} as const;

export function flowRecords(state: RealFlowState, report: Record<string, unknown> | null, exports: GuidedExport[]): FlowRecords {
  const files: WbFile[] = [];
  const text = new Map<string, string>();
  const artifacts = new Map<string, string>();
  const items: FlowRecords['items'] = {};
  const record = (path: string, content: string, sha256: string | null = null) => {
    text.set(path, content);
    files.push({ path, bytes: utf8.encode(content).length, mediaType: path.endsWith('.json') ? 'application/json' : 'text/plain', badge: '记录', sha256 });
  };

  if (state.paper) {
    const p = state.paper;
    record(FLOW_PATHS.paper, json(p));
    items.input = [
      { id: 'paper', label: p.title || '已上传论文', detail: `sha256 ${p.sha256.slice(0, 12)}…`, tone: p.error ? 'bad' : 'ok', openPath: FLOW_PATHS.paper, time: clock(p.created_at) },
      { id: 'status', label: `状态 ${p.status}${p.page_count ? ` · ${p.page_count} 页` : ''}`, tone: p.error ? 'bad' : undefined },
    ];
    if (state.docparse.text) {
      const t = state.docparse.text;
      record(FLOW_PATHS.parse, json(t));
      items.input.push({ id: 'parse', label: `PDF 解析 ${t.status} · ${t.pages_parsed}/${t.page_count} 页`, detail: t.backend ?? null, openPath: FLOW_PATHS.parse, tone: t.status === 'ready' ? 'ok' : t.status === 'failed' || t.error_code ? 'bad' : t.status === 'none' ? 'idle' : 'run' });
    }
  }
  if (state.provider) {
    items.provider = [{ id: 'model', label: `${state.provider.modelId} · ${state.provider.endpointAlias}`, detail: `key ••••${state.provider.secretLast4 ?? ''}`, tone: 'ok' }];
  }
  if (state.extraction) {
    const e = state.extraction;
    record(FLOW_PATHS.extraction, json(e));
    items.extract = [
      { id: 'ext', label: `抽取出 ${e.candidate_experiments.length} 个候选实验`, detail: e.extraction_id, tone: 'ok', openPath: FLOW_PATHS.extraction },
      ...e.candidate_experiments.slice(0, 6).map((c, i) => ({ id: `c${i}`, label: c.label || c.id || `候选 ${i + 1}`, detail: `${c.recipe_id ?? '—'} · ${c.execution_support}`, tone: (c.execution_support === 'supported' ? 'ok' : c.execution_support === 'requires_review' ? 'warn' : 'idle') as Tone, openPath: FLOW_PATHS.extraction })),
    ];
    if (e.warnings.length) items.extract.push({ id: 'warn', label: `${e.warnings.length} 条警告`, tone: 'warn', openPath: FLOW_PATHS.extraction });
  }
  const candidate = findCandidate(state.extraction, state.selectedCandidateId);
  if (candidate && (state.experimentId || state.confirmationId)) {
    record(FLOW_PATHS.spec, json({ candidate, training: state.training, experiment_id: state.experimentId, revision_id: state.revisionId, confirmation_id: state.confirmationId }));
    items.spec = [
      { id: 'cand', label: candidate.label || candidate.id || '已选候选', detail: `${candidate.recipe_id ?? '—'}/${candidate.recipe_version ?? '—'}`, openPath: FLOW_PATHS.spec },
      { id: 'conf', label: state.confirmationId ? '方案已确认' : '方案未确认', detail: state.confirmationId ?? state.revisionId, tone: state.confirmationId ? 'ok' : 'warn', openPath: FLOW_PATHS.spec },
    ];
  } else if (candidate) {
    items.spec = [{ id: 'cand', label: `已选 ${candidate.label || candidate.id}`, detail: '尚未确认' }];
  }
  if (state.envCheck) {
    const env = state.envCheck;
    record(FLOW_PATHS.environment, json(env));
    const failed = env.items.filter(i => i.status === 'fail').length;
    const unknown = env.items.filter(i => i.status === 'unknown').length;
    items.environment = [
      { id: 'decision', label: `decision=${env.decision}`, detail: `${env.items.length} 项检查${failed ? ` · ${failed} 项失败` : ''}${unknown ? ` · ${unknown} 项未知` : ''}`, tone: env.decision === 'ready' ? 'ok' : env.decision === 'blocked' ? 'warn' : 'bad', openPath: FLOW_PATHS.environment },
      ...env.items.filter(i => i.status !== 'pass' && i.status !== 'not_needed').slice(0, 5).map(i => ({ id: i.key, label: i.key, detail: i.status, tone: (i.status === 'fail' ? 'bad' : 'warn') as Tone, openPath: FLOW_PATHS.environment })),
    ];
    if (state.ssh) {
      record(FLOW_PATHS.ssh, json(state.ssh));
      items.environment.push({ id: 'ssh', label: 'SSH target 已登记（fake 验证）', detail: state.ssh.status, tone: 'warn', openPath: FLOW_PATHS.ssh });
    }
  }
  if (state.codegen) {
    const c = state.codegen;
    const path = `代码/attempt-${c.attemptIndex}/reproduce.py`;
    record(path, c.code, c.codeSha256);
    items.codegen = [
      { id: 'code', label: `复现代码 · ${c.code.split('\n').length} 行`, detail: `sha256 ${c.codeSha256.slice(0, 12)}…`, tone: 'ok', openPath: path },
      ...(c.notes ? [{ id: 'notes', label: c.notes }] : []),
    ];
  }
  if (state.runId) {
    if (state.run) record(FLOW_PATHS.run, json(state.run));
    if (state.events.length) record(FLOW_PATHS.events, state.events.map(e => `#${e.seq}  ${e.occurred_at}  ${e.type}  ${JSON.stringify(e.payload)}`).join('\n'));
    const recent = state.events.slice(-6).reverse();
    items.execute = [
      { id: 'run', label: `Run ${state.run?.status ?? '已创建'}`, detail: state.runId, tone: state.run ? RUN_TONE[state.run.status] ?? 'idle' : 'run', openPath: state.run ? FLOW_PATHS.run : undefined },
      ...recent.map(e => ({ id: `e${e.seq}`, label: e.type, detail: `#${e.seq}`, time: clock(e.occurred_at), openPath: FLOW_PATHS.events })),
    ];
  }
  if (report) {
    record(FLOW_PATHS.report, json(report));
    (items.execute ??= []).push({ id: 'report', label: `报告 · ${typeof report.evidence_class === 'string' ? report.evidence_class : state.reportStatus ?? 'ready'}`, tone: 'ok', openPath: FLOW_PATHS.report });
  }
  for (const bundle of exports) {
    for (const item of bundle.items) {
      if (item.status !== 'ready' || !item.artifact_id || !item.filename) continue;
      const path = `导出/${item.filename}`;
      if (artifacts.has(path)) continue;
      artifacts.set(path, item.artifact_id);
      files.push({ path, bytes: item.bytes, mediaType: item.media_type, sha256: item.sha256, badge: '导出' });
      (items.execute ??= []).push({ id: `x-${item.format}`, label: item.filename, detail: item.format, tone: 'ok', openPath: path });
    }
  }
  return { files, text, artifacts, items };
}
