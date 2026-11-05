import type { AutonomousEvent, AutonomousFile, AutonomousTask } from '../../api-client/autonomous.ts';
import type { StageMark, WbStage, WbStageItem } from '../../ui/workbench/Workbench.tsx';
import type { WbFile, WbTag } from '../../ui/workbench/tree.ts';
import { evidenceKindLabel, outcomeLabel, outcomeTone, own, researchTaskTypeLabel, stateLabel, taskTone, terminalState } from './config.ts';

// Presentation-only: groups the autonomous task's real event journal into the
// research phases shown on the workbench timeline. There is no backend stage
// machine: Pi decides its own steps, so the phases are derived from tool calls
// and events, and Pi's own progress notes (note_progress) only label them.
// The active phase is the phase of the latest phase-mapped event, so a repair
// round that goes back to writing code shows 编写与执行 as active again.
// Tool calls become sub-steps, coloured by their result; Pi's notes and
// specialist advice are model narrative and are never coloured as passed.

export type PhaseId = 'setup' | 'plan' | 'execute' | 'evidence' | 'review' | 'report';
export const PHASES: Array<{ id: PhaseId; title: string; idle: string }> = [
  { id: 'setup', title: '启动与授权', idle: '等待任务启动' },
  { id: 'plan', title: '阅读与方案', idle: '读取论文、提出实验协议' },
  { id: 'execute', title: '编写与执行', idle: '写代码、在隔离环境运行' },
  { id: 'evidence', title: '冻结证据', idle: '登记代码、环境、数据与结果' },
  { id: 'review', title: '候选与复核', idle: '提交候选、程序评估与独立复核' },
  { id: 'report', title: '报告与结束', idle: '生成报告（不代表复现成立）' },
];

// note_progress 不在表里：它只是 Pi 的自述，留在当前阶段，不改变阶段。
const TOOL_PHASE: Record<string, PhaseId> = {
  read: 'plan', propose_protocol: 'plan',
  write: 'execute', execute: 'execute', background: 'execute', status: 'execute', logs: 'execute', stop: 'execute',
  register_artifact: 'evidence',
  submit_candidate: 'review',
};
// 注意：authorization_confirmed、config_version、background_jobs_retained 在后端
// 只是 payload 字段（分别在 task_created / application_exit / task_paused 中），
// 不是事件类型，不能出现在这张表里（契约 C9 核对，2026-11-05）。
// worker_exited / application_exit 是进程生命周期（暂停时也会出现），不代表进入报告阶段。
// protocol_rejected 与审批请求、批准放在同一阶段，一次决定不拆到两处。
const EVENT_PHASE: Record<string, PhaseId> = {
  task_created: 'setup', worker_started: 'setup', session_ready: 'setup',
  protocol_approval_required: 'plan', protocol_approved: 'plan', protocol_rejected: 'plan',
  job_reconciled: 'execute',
  artifact_frozen: 'evidence',
  candidate_frozen: 'review', review_recorded: 'review', review_failed: 'review',
  report_created: 'report', report_failed: 'report', task_finished: 'report', budget_exhausted: 'report', safety_blocked: 'report',
};
const FAILURE_EVENTS = new Set(['task_error', 'budget_exhausted', 'safety_blocked', 'report_failed']);
const DELEGATION_LABEL = '专家建议（模型叙述）';

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isSafeInteger(v) ? v : null);
const short = (s: string, n = 90) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);
export const clock = (iso: string | null | undefined) => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
};

export function toolLabel(name: string, args: Record<string, unknown>): string {
  const path = str(args.path); const command = str(args.command);
  if (name === 'read') return `读取 ${path ?? '文件'}`;
  if (name === 'write') return `写入 ${path ?? '文件'}`;
  if (name === 'execute') return `$ ${short(command ?? '命令', 70)}`;
  if (name === 'background') return `后台 $ ${short(command ?? '命令', 64)}`;
  if (name === 'status') return '查询作业状态';
  if (name === 'logs') return '读取作业日志';
  if (name === 'stop') return '停止作业';
  if (name === 'propose_protocol') return '提出实验协议';
  if (name === 'register_artifact') return `登记 ${str(args.name) ?? '产物'}`;
  if (name === 'submit_candidate') return '提交候选结果';
  if (name === 'note_progress') return str(args.phase) ? `Pi 自报进度：${short(str(args.phase)!, 40)}` : 'Pi 自报进度';
  return name;
}

type Label = { label: string; detail: string | null; tone?: WbStageItem['tone'] };
function eventLabel(e: AutonomousEvent): Label | null {
  const p = rec(e.payload);
  switch (e.type) {
    case 'task_created': {
      const version = p.config_version;
      return { label: '任务已创建', detail: typeof version === 'string' || typeof version === 'number' ? `配置版本 ${String(version)}` : null };
    }
    case 'worker_started': return { label: '执行进程已启动', detail: null };
    case 'session_ready': return { label: '模型会话就绪', detail: str(p.session_id), tone: 'ok' };
    case 'protocol_approval_required': return { label: '等待方案审批', detail: str(p.reason), tone: 'warn' };
    case 'protocol_approved': return { label: '方案已批准', detail: str(p.protocol_version) ?? null, tone: 'ok' };
    case 'protocol_rejected': return { label: '方案变更已拒绝', detail: str(p.note) ? `理由：${short(str(p.note)!, 60)}` : '当前方案保持不变', tone: 'accent' };
    case 'artifact_frozen': return { label: `冻结 ${str(p.name) ?? '证据'}`, detail: [str(p.kind), typeof p.bytes === 'number' ? `${p.bytes} B` : null].filter(Boolean).join(' · ') || null, tone: 'ok' };
    case 'candidate_frozen': {
      const round = int(p.round); const snapshot = str(p.snapshot_sha256);
      return { label: '候选已冻结', detail: [round !== null ? `第 ${round} 轮` : null, snapshot ? snapshot.slice(0, 12) : null].filter(Boolean).join(' · ') || null, tone: 'ok' };
    }
    case 'review_recorded': {
      const findings = Array.isArray(p.findings) ? p.findings.length : 0;
      return p.verdict === 'pass'
        ? { label: '独立复核通过', detail: null, tone: 'ok' }
        : { label: `复核发现 ${findings} 个问题`, detail: str(p.verdict) === 'issues' ? null : `结论 ${String(p.verdict)}`, tone: 'warn' };
    }
    case 'review_failed': return { label: int(p.attempt) !== null ? `第 ${int(p.attempt)} 次复核未完成` : '复核未完成', detail: str(p.reason) ? short(str(p.reason)!, 60) : null, tone: 'bad' };
    case 'continuation_sent': return p.reason === 'review_issues' && int(p.repair_round) !== null ? { label: `进入第 ${int(p.repair_round)} 轮修复`, detail: null, tone: 'warn' } : null;
    case 'safety_blocked': return { label: '安全终止', detail: str(p.code), tone: 'bad' };
    // 报告与 verdict 分开：not_comparable 不配绿，这里只说报告已生成和结局。
    case 'report_created': return { label: '报告已生成', detail: outcomeLabel(p.outcome), tone: 'idle' };
    case 'report_failed': return { label: '报告生成失败', detail: str(p.code), tone: 'bad' };
    case 'task_finished': return { label: '任务结束', detail: outcomeLabel(p.outcome) ?? (str(p.state) ? stateLabel(str(p.state)!) : null), tone: outcomeTone(p.outcome) ?? taskTone(str(p.state) ?? '') };
    case 'task_error': return { label: '任务出错', detail: str(rec(p.error).message) ?? str(p.message) ?? str(p.reason), tone: 'bad' };
    case 'budget_exhausted': return { label: '预算耗尽', detail: str(p.reason), tone: 'warn' };
    case 'task_paused': return { label: '已暂停', detail: str(p.reason), tone: 'warn' };
    case 'task_resumed': return { label: '已恢复', detail: null, tone: 'run' };
    case 'stop_requested': return { label: '请求停止', detail: null, tone: 'warn' };
    case 'user_message': return { label: '你的指令', detail: str(p.message) ? short(str(p.message)!, 60) : null, tone: 'accent' };
    default: return null;
  }
}

/** Path of an artifact file in the listing (mirrors files._artifact_listing_path). */
export function artifactPath(name: string, protocolSha: string | null | undefined, round?: number | null) {
  return `artifacts/${protocolSha ? protocolSha.slice(0, 12) : 'unversioned'}${round == null ? '' : `/r${round}`}/${name}`;
}
/** Repair round parsed from `artifacts/<v12>/r<n>/<name>`; the legacy layout has none. */
export function artifactRound(path: string): number | null {
  const match = /^artifacts\/[^/]+\/r(\d+)\//.exec(path);
  return match ? Number(match[1]) : null;
}

// 专家调用的真实事件（apps/harness/bridge.mjs research_delegate）：
//   research_delegation_started → 仅成功时 research_delegation_completed（带 text）→
//   finally 中总有一条 message_end {delegated: true, specialist_id, message: {model, stopReason}}。
// 失败时没有 completed，message_end 的 stopReason 为 error / aborted。
const isDelegation = (e: AutonomousEvent) => {
  const p = rec(e.payload);
  return e.type === 'sdk_event' && (p.type === 'research_delegation_started' || p.type === 'research_delegation_completed'
    || (p.type === 'message_end' && p.delegated === true));
};
// 与 tools/autonomous_acceptance.py 相同：完成事件按 (专家, 任务类型, 模型) 先进先出配对。
const delegationKey = (p: Record<string, unknown>) => JSON.stringify([p.specialist_id, p.task_type, p.model]);
const FAILED_STOP_REASONS = new Set(['error', 'aborted']);
const CALL_STATUS_TEXT: Record<SpecialistCall['status'], string> = { running: '进行中', done: '已返回', failed: '调用失败', missing: '未返回' };

export function buildStages(task: AutonomousTask | null, events: AutonomousEvent[]): { stages: WbStage[]; active: PhaseId; started: Set<PhaseId> } {
  const items = new Map<PhaseId, WbStageItem[]>(PHASES.map(p => [p.id, []]));
  const started = new Set<PhaseId>();
  const failed = new Set<PhaseId>();
  const toolItem = new Map<string, WbStageItem>();
  const calls = new Map(specialistCalls(events, task).map(call => [call.sequence, call]));
  let lastVerdict: string | null = null;
  let reviewFailedCount = 0;
  const notes = new Map<PhaseId, { phase: string; summary: string }>();
  let current: PhaseId = 'setup';
  const enter = (phase: PhaseId) => {
    if (phase !== current) notes.delete(phase); // 重新进入的阶段不沿用上一轮的自报
    current = phase; started.add(phase);
  };
  for (const e of events) {
    const p = rec(e.payload);
    const base = { id: `e${e.sequence}`, time: clock(e.created_at), openPath: 'logs/events.log' };
    if (e.type === 'tool_started') {
      const name = str(p.name) ?? 'tool';
      const phase = own(TOOL_PHASE, name);
      if (phase) enter(phase); else started.add(current);
      const item: WbStageItem = { ...base, id: `t${e.sequence}`, label: toolLabel(name, rec(p.arguments)), detail: `#${e.sequence} · ${name}`, tone: 'run' };
      items.get(current)!.push(item);
      const id = str(p.id); if (id) toolItem.set(id, item);
      continue;
    }
    if (e.type === 'tool_completed') {
      const item = toolItem.get(str(p.id) ?? '');
      if (item) {
        item.tone = p.is_error ? 'bad' : p.name === 'note_progress' ? 'idle' : 'ok';
        const result = rec(p.result);
        if (typeof result.exit_code === 'number') item.detail = `${item.detail} · exit ${result.exit_code}`;
        if (p.is_error) failed.add(own(TOOL_PHASE, p.name) ?? current);
      }
      continue;
    }
    if (e.type === 'progress_noted') {
      const phase = str(p.phase); const summary = str(p.summary);
      if (phase || summary) notes.set(current, { phase: phase ?? '未命名阶段', summary: summary ?? '' });
      continue;
    }
    if (isDelegation(e)) {
      // 每次调用一条子步骤，放在调用开始时所在的阶段；状态取配对后的最终结果。
      started.add(current);
      const call = calls.get(e.sequence);
      if (call) items.get(current)!.push({ ...base, label: DELEGATION_LABEL, tone: call.status === 'failed' ? 'bad' : 'idle',
        detail: `${call.specialistId ?? '未知专家'} · ${call.taskTypeLabel}，${CALL_STATUS_TEXT[call.status]}` });
      continue;
    }
    if (e.type === 'review_recorded') lastVerdict = str(p.verdict) ?? '未知';
    if (e.type === 'review_failed') reviewFailedCount += 1;
    const label = eventLabel(e);
    const mapped = own(EVENT_PHASE, e.type);
    if (mapped) enter(mapped);
    if (FAILURE_EVENTS.has(e.type)) failed.add(current);
    if (e.type === 'artifact_frozen' && str(p.name)) {
      items.get(current)!.push({ ...base, ...label!, openPath: artifactPath(str(p.name)!, str(p.protocol_sha256), int(p.round)) });
      continue;
    }
    if (label) items.get(current)!.push({ ...base, ...label });
  }
  if (task) started.add('setup');
  const hasOutcome = task?.outcome != null;
  const finished = task ? terminalState(task.state) || hasOutcome : false;
  const order = PHASES.map(p => p.id);
  const active = current;
  const activeIdx = order.indexOf(active);
  const running = !hasOutcome && (task?.state === 'running' || task?.state === 'starting' || task?.state === 'queued');
  // 候选与复核是程序结论：只有 verified 或最近一次复核通过才算完成（绿勾）。
  const reviewMark = (): StageMark => {
    const outcome = task?.outcome ?? null;
    if (outcome === 'blocked_by_safety') return 'failed';
    if (outcome === 'verified') return 'done';
    if (outcome === 'completed_with_issues' || outcome === 'review_unavailable') return 'attention';
    if (lastVerdict === 'pass') return 'done';
    if (lastVerdict !== null || reviewFailedCount > 0) return 'attention';
    return failed.has('review') && task?.state === 'failed' ? 'failed' : 'open';
  };
  const stages = PHASES.map(phase => {
    const idx = order.indexOf(phase.id);
    const list = items.get(phase.id)!.slice(-14);
    const isStarted = started.has(phase.id);
    const liveActive = idx === activeIdx && !finished;
    let mark: StageMark;
    if (phase.id === 'review' && isStarted && !liveActive) mark = reviewMark();
    else if (idx < activeIdx) mark = failed.has(phase.id) && task?.state === 'failed' ? 'failed' : 'done';
    // 修复轮次回到前面的阶段时，后面只在上一轮开始过的阶段显示「可进入」，不显示完成。
    else if (idx > activeIdx) mark = isStarted ? 'open' : 'locked';
    else if (!finished) mark = task?.pending_decision != null || task?.state === 'paused' || task?.state === 'blocked' ? 'attention' : running ? 'running' : 'current';
    else if (failed.has(phase.id) || task?.state === 'failed' || task?.outcome === 'blocked_by_safety') mark = 'failed';
    else mark = hasOutcome || task?.state === 'completed' || task?.state === 'succeeded' ? 'done' : 'attention';
    const count = items.get(phase.id)!.length;
    const note = phase.id === active ? notes.get(active) : undefined;
    const summary = note ? `Pi 自报 · ${short(note.phase, 24)}：${short(note.summary, 80)}`
      : isStarted ? `${count} 条记录${count > 14 ? '（显示最近 14 条）' : ''}` : phase.idle;
    return { id: phase.id, title: phase.title, mark, summary, items: list, disabled: list.length === 0 };
  });
  return { stages, active, started };
}

// ---- 由事件流得出的只读列表（候选复核卡、自报进度、专家调用、安全终止提示用）----

export interface ReviewFailure { sequence: number; snapshotSha256: string | null; attempt: number | null; reason: string | null; time: string | null }
/** 未完成的复核尝试（review_failed）。 */
export function reviewFailures(events: AutonomousEvent[]): ReviewFailure[] {
  return events.filter(e => e.type === 'review_failed').map(e => {
    const p = rec(e.payload);
    return { sequence: e.sequence, snapshotSha256: str(p.snapshot_sha256), attempt: int(p.attempt), reason: str(p.reason), time: clock(e.created_at) };
  });
}

export interface ProgressNote { sequence: number; phase: string | null; summary: string | null; time: string | null }
/** Pi 自报进度（progress_noted），新的在前。模型叙述，不是程序判定。 */
export function progressNotes(events: AutonomousEvent[]): ProgressNote[] {
  return events.filter(e => e.type === 'progress_noted').map(e => {
    const p = rec(e.payload);
    return { sequence: e.sequence, phase: str(p.phase), summary: str(p.summary), time: clock(e.created_at) };
  }).reverse();
}

export interface SpecialistCall {
  sequence: number; specialistId: string | null; taskType: string | null; taskTypeLabel: string; model: string | null; reason: string | null;
  /** missing：任务已结束或已有结局，但这次调用既没有返回也没有失败记录。 */
  status: 'running' | 'done' | 'failed' | 'missing'; text: string | null; time: string | null;
}
/**
 * 科研专家调用：建议，不是执行证据。按开始顺序排列。
 * started 与 completed 按 (专家, 任务类型, 模型) 先进先出配对；每次调用最后的 delegated
 * message_end 按 (专家, 模型) 配对到最早一条尚未结束的调用，stopReason 为 error / aborted 即失败。
 */
export function specialistCalls(events: AutonomousEvent[], task?: Pick<AutonomousTask, 'state' | 'outcome'> | null): SpecialistCall[] {
  const calls: SpecialistCall[] = [];
  const awaitingCompletion = new Map<string, SpecialistCall[]>();
  const awaitingEnd: SpecialistCall[] = [];
  for (const e of events) {
    if (!isDelegation(e)) continue;
    const p = rec(e.payload);
    if (p.type === 'message_end') {
      const message = rec(p.message);
      const index = awaitingEnd.findIndex(c => c.specialistId === str(p.specialist_id) && c.model === str(message.model));
      if (index < 0) continue;
      const [call] = awaitingEnd.splice(index, 1);
      if (call.status === 'running' && FAILED_STOP_REASONS.has(String(message.stopReason))) {
        call.status = 'failed';
        for (const queue of awaitingCompletion.values()) { const at = queue.indexOf(call); if (at >= 0) queue.splice(at, 1); }
      }
      continue;
    }
    const key = delegationKey(p);
    const queue = awaitingCompletion.get(key) ?? [];
    let call = p.type === 'research_delegation_completed' ? queue.shift() : undefined;
    if (!call) {
      const taskType = str(p.task_type);
      call = { sequence: e.sequence, specialistId: str(p.specialist_id), taskType, taskTypeLabel: taskType ? researchTaskTypeLabel(taskType) : '未知类型',
        model: str(p.model), reason: str(p.reason), status: 'running', text: null, time: clock(e.created_at) };
      calls.push(call);
      awaitingEnd.push(call);
      if (p.type === 'research_delegation_started') awaitingCompletion.set(key, [...queue, call]);
    }
    if (p.type === 'research_delegation_completed') {
      call.status = 'done';
      call.text = typeof p.text === 'string' ? p.text : null;
    }
  }
  const finished = !!task && (terminalState(task.state) || task.outcome != null);
  if (finished) for (const call of calls) if (call.status === 'running') call.status = 'missing';
  return calls;
}

/** 安全终止：结局为 blocked_by_safety 或出现 safety_blocked 事件。code 未知时为 null。 */
export function safetyBlock(task: Pick<AutonomousTask, 'outcome' | 'report'> | null, events: AutonomousEvent[]): { code: string | null } | null {
  const event = [...events].reverse().find(e => e.type === 'safety_blocked');
  if (!event && task?.outcome !== 'blocked_by_safety') return null;
  return { code: (event && str(rec(event.payload).code)) ?? str(rec(task?.report).outcome_reason) };
}

/** 当前生效的协议 SHA256：取最近的协议事件，其次是当前候选冻结的协议；都没有时未知。 */
export function currentProtocolSha(task: Pick<AutonomousTask, 'candidate'> | null, events: AutonomousEvent[]): string | null {
  let sha: string | null = null;
  for (const e of events) {
    const p = rec(e.payload);
    if (e.type === 'task_created') sha = str(p.protocol_sha256) ?? sha;
    if (e.type === 'protocol_approved' || e.type === 'protocol_version') sha = str(rec(p.protocol).sha256) ?? sha;
  }
  return sha ?? str(task?.candidate?.protocol_sha256);
}

/**
 * Listing → workbench files. Evidence entries carry their kind and repair round
 * (parsed from `artifacts/<v12>/r<n>/<name>`), and are flagged when registered
 * under another protocol version or in an earlier round than the current one.
 * Without context (round / protocol unknown) nothing is flagged.
 */
export function toWbFiles(items: AutonomousFile[], context: { round: number | null; protocolSha256: string | null } = { round: null, protocolSha256: null }): WbFile[] {
  return items.map(f => {
    const file: WbFile = { path: f.path, bytes: f.bytes, mediaType: f.media_type, sha256: f.sha256, badge: f.source === 'artifact' ? '证据' : undefined };
    if (f.source !== 'artifact') return file;
    const tags: WbTag[] = [];
    if (f.kind) tags.push({ text: evidenceKindLabel(f.kind), title: `证据类型 ${f.kind}` });
    const round = artifactRound(f.path);
    if (round !== null) tags.push({ text: `第 ${round} 轮`, title: `修复轮次 ${round}` });
    if (context.protocolSha256 && f.protocol_sha256 !== context.protocolSha256) {
      tags.push({ text: '旧方案版本', tone: 'warn', inTree: true, title: `登记时的协议 ${f.protocol_sha256 ?? '未版本化'} 与当前方案不同` });
    }
    if (round !== null && context.round !== null && round < context.round) {
      tags.push({ text: '早期轮次', tone: 'idle', inTree: true, title: `登记于第 ${round} 轮，当前为第 ${context.round} 轮` });
    }
    return tags.length ? { ...file, tags } : file;
  });
}
