import type { AutonomousEvent, AutonomousFile, AutonomousTask } from '../../api-client/autonomous.ts';
import type { StageMark, WbStage, WbStageItem } from '../../ui/workbench/Workbench.tsx';
import type { WbFile } from '../../ui/workbench/tree.ts';
import { terminalState } from './config.ts';

// Presentation-only: groups the autonomous task's real event journal into the
// research phases shown on the workbench timeline. A phase is "done" only once
// a later phase has started (or the task finished); nothing is inferred beyond
// what the events say. Tool calls become sub-steps, coloured by their result.

export type PhaseId = 'setup' | 'plan' | 'execute' | 'evidence' | 'report';
export const PHASES: Array<{ id: PhaseId; title: string; idle: string }> = [
  { id: 'setup', title: '启动与授权', idle: '等待任务启动' },
  { id: 'plan', title: '阅读与方案', idle: '读取论文、提出实验协议' },
  { id: 'execute', title: '编写与执行', idle: '写代码、在隔离环境运行' },
  { id: 'evidence', title: '冻结证据', idle: '登记代码、环境、数据与结果' },
  { id: 'report', title: '报告与结束', idle: '生成报告（不代表复现成立）' },
];

const TOOL_PHASE: Record<string, PhaseId> = {
  read: 'plan', propose_protocol: 'plan',
  write: 'execute', execute: 'execute', background: 'execute', status: 'execute', logs: 'execute', stop: 'execute',
};
// 注意：authorization_confirmed、config_version、background_jobs_retained 在后端
// 只是 payload 字段（分别在 task_created / application_exit / task_paused 中），
// 不是事件类型，不能出现在这张表里（契约 C9 核对，2026-11-05）。
const EVENT_PHASE: Record<string, PhaseId> = {
  task_created: 'setup', worker_started: 'setup', session_ready: 'setup',
  protocol_approval_required: 'plan', protocol_approved: 'plan',
  job_reconciled: 'execute',
  artifact_frozen: 'evidence',
  report_created: 'report', task_finished: 'report', budget_exhausted: 'report', worker_exited: 'report', application_exit: 'report',
};

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
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
  return name;
}

function eventLabel(e: AutonomousEvent): { label: string; detail: string | null; tone?: WbStageItem['tone'] } | null {
  const p = rec(e.payload);
  switch (e.type) {
    case 'task_created': {
      const version = e.payload && typeof e.payload === 'object' && !Array.isArray(e.payload)
        ? (e.payload as Record<string, unknown>).config_version : null;
      return { label: '任务已创建', detail: typeof version === 'string' || typeof version === 'number' ? `配置版本 ${String(version)}` : null };
    }
    case 'worker_started': return { label: '执行进程已启动', detail: null };
    case 'session_ready': return { label: '模型会话就绪', detail: str(p.session_id), tone: 'ok' };
    case 'protocol_approval_required': return { label: '等待方案审批', detail: str(p.reason), tone: 'warn' };
    case 'protocol_approved': return { label: '方案已批准', detail: str(p.protocol_version) ?? null, tone: 'ok' };
    case 'artifact_frozen': return { label: `冻结 ${str(p.name) ?? '证据'}`, detail: [str(p.kind), typeof p.bytes === 'number' ? `${p.bytes} B` : null].filter(Boolean).join(' · ') || null, tone: 'ok' };
    case 'report_created': return { label: '报告已生成', detail: str(p.verdict) ?? str(p.status), tone: 'ok' };
    case 'task_finished': return { label: '任务结束', detail: str(p.state), tone: 'ok' };
    case 'task_error': return { label: '任务出错', detail: str(rec(p.error).message) ?? str(p.message) ?? str(p.reason), tone: 'bad' };
    case 'budget_exhausted': return { label: '预算耗尽', detail: str(p.reason), tone: 'warn' };
    case 'task_paused': return { label: '已暂停', detail: str(p.reason), tone: 'warn' };
    case 'task_resumed': return { label: '已恢复', detail: null, tone: 'run' };
    case 'stop_requested': return { label: '请求停止', detail: null, tone: 'warn' };
    case 'user_message': return { label: '你的指令', detail: str(p.message) ? short(str(p.message)!, 60) : null, tone: 'accent' };
    default: return null;
  }
}

/** Path of an artifact file in the listing (mirrors the backend's layout). */
export function artifactPath(name: string, protocolSha: string | null | undefined) {
  return `artifacts/${protocolSha ? protocolSha.slice(0, 12) : 'unversioned'}/${name}`;
}

export function buildStages(task: AutonomousTask | null, events: AutonomousEvent[]): { stages: WbStage[]; active: PhaseId; started: Set<PhaseId> } {
  const items = new Map<PhaseId, WbStageItem[]>(PHASES.map(p => [p.id, []]));
  const started = new Set<PhaseId>();
  const failed = new Set<PhaseId>();
  const toolItem = new Map<string, WbStageItem>();
  let current: PhaseId = 'setup';
  for (const e of events) {
    const p = rec(e.payload);
    if (e.type === 'tool_started') {
      const name = str(p.name) ?? 'tool';
      const phase: PhaseId = TOOL_PHASE[name] ?? current;
      current = phase; started.add(phase);
      const item: WbStageItem = { id: `t${e.sequence}`, label: toolLabel(name, rec(p.arguments)), detail: `#${e.sequence} · ${name}`, time: clock(e.created_at), tone: 'run', openPath: 'logs/events.log' };
      items.get(phase)!.push(item);
      const id = str(p.id); if (id) toolItem.set(id, item);
      continue;
    }
    if (e.type === 'tool_completed') {
      const item = toolItem.get(str(p.id) ?? '');
      if (item) {
        item.tone = p.is_error ? 'bad' : 'ok';
        const result = rec(p.result);
        if (typeof result.exit_code === 'number') item.detail = `${item.detail} · exit ${result.exit_code}`;
        if (p.is_error) failed.add(TOOL_PHASE[str(p.name) ?? ''] ?? current);
      }
      continue;
    }
    const label = eventLabel(e);
    const phase: PhaseId = EVENT_PHASE[e.type] ?? current;
    if (EVENT_PHASE[e.type]) { current = phase; started.add(phase); }
    if (e.type === 'artifact_frozen' && str(p.name)) {
      items.get(phase)!.push({ id: `e${e.sequence}`, ...label!, time: clock(e.created_at), openPath: artifactPath(str(p.name)!, str(p.protocol_sha256)) });
      continue;
    }
    if (e.type === 'task_error' || e.type === 'budget_exhausted') failed.add(phase);
    if (label) items.get(phase)!.push({ id: `e${e.sequence}`, ...label, time: clock(e.created_at), openPath: 'logs/events.log' });
  }
  if (task) started.add('setup');
  const done = task ? terminalState(task.state) : false;
  const order = PHASES.map(p => p.id);
  const lastStarted = order.filter(id => started.has(id)).pop() ?? 'setup';
  const running = task?.state === 'running' || task?.state === 'starting' || task?.state === 'queued';
  const stages = PHASES.map(phase => {
    const idx = order.indexOf(phase.id);
    const list = items.get(phase.id)!.slice(-14);
    const isStarted = started.has(phase.id);
    let mark: StageMark;
    if (!isStarted) mark = order.indexOf(lastStarted) > idx ? 'done' : 'locked';
    else if (phase.id === lastStarted && !done) mark = task?.pending_decision != null || task?.state === 'paused' || task?.state === 'blocked' ? 'attention' : running ? 'running' : 'current';
    else if (failed.has(phase.id) && (phase.id === lastStarted || task?.state === 'failed')) mark = 'failed';
    else mark = 'done';
    if (phase.id === lastStarted && done && task?.state !== 'completed' && task?.state !== 'succeeded') mark = task?.state === 'failed' ? 'failed' : 'attention';
    const count = items.get(phase.id)!.length;
    return { id: phase.id, title: phase.title, mark, summary: isStarted ? `${count} 条记录${count > 14 ? '（显示最近 14 条）' : ''}` : phase.idle, items: list, disabled: list.length === 0 };
  });
  return { stages, active: lastStarted, started };
}

export function toWbFiles(items: AutonomousFile[]): WbFile[] {
  return items.map(f => ({ path: f.path, bytes: f.bytes, mediaType: f.media_type, sha256: f.sha256, badge: f.source === 'artifact' ? '证据' : undefined }));
}
