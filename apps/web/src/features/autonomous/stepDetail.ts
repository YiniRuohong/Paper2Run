import type { AutonomousEvent, AutonomousTask } from '../../api-client/autonomous.ts';
import { artifactPath, clock, eventLabel, specialistCalls, toolLabel, type SpecialistCall } from './stages.ts';

// 抽屉内容：左栏一条子步骤（id 由 stages.ts 生成：t<序号> 工具调用、e<序号> 事件、n<序号> 自报进度）
// 对应的过程，只从真实事件里取，不推断。

/** 输出只保留末尾这么多字符，前面注明已截断。 */
export const OUTPUT_LIMIT = 64_000;

export type ItemDetail =
  | { kind: 'command'; name: string; command: string; running: boolean; stdout: string | null; stderr: string | null; exitCode: number | null; state: string | null; truncated: boolean; error: string | null; time: string | null }
  | { kind: 'tool'; name: string; title: string; args: Record<string, unknown>; running: boolean; result: unknown; error: string | null; time: string | null }
  | { kind: 'note'; phase: string; summary: string; time: string | null }
  | { kind: 'delegation'; call: SpecialistCall }
  | { kind: 'artifact'; name: string; evidenceKind: string | null; sha256: string | null; bytes: number | null; round: number | null; sourcePath: string | null; path: string; time: string | null }
  | { kind: 'event'; type: string; label: string; detail: string | null; payload: unknown; time: string | null };

const rec = (v: unknown): Record<string, unknown> => (v && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : {});
const str = (v: unknown): string | null => (typeof v === 'string' && v ? v : null);
const int = (v: unknown): number | null => (typeof v === 'number' && Number.isSafeInteger(v) ? v : null);
const COMMAND_TOOLS = new Set(['execute', 'background', 'status', 'logs', 'stop']);

export function itemSequence(id: string): number | null {
  const match = /^[ten](\d+)$/.exec(id);
  return match ? Number(match[1]) : null;
}

/** 超长输出保留末尾 OUTPUT_LIMIT 个字符。 */
export function tail(text: string | null): { text: string | null; cut: boolean } {
  if (text === null || text.length <= OUTPUT_LIMIT) return { text, cut: false };
  return { text: text.slice(-OUTPUT_LIMIT), cut: true };
}

function toolError(result: Record<string, unknown>): string {
  return str(result.message) ?? str(result.code) ?? '工具调用失败';
}

export function itemDetail(events: AutonomousEvent[], itemId: string, task?: Pick<AutonomousTask, 'state' | 'outcome'> | null): ItemDetail | null {
  const sequence = itemSequence(itemId);
  if (sequence === null) return null;
  const event = events.find(e => e.sequence === sequence);
  if (!event) return null;
  const p = rec(event.payload);
  const time = clock(event.created_at);

  if (event.type === 'tool_started') {
    const name = str(p.name) ?? 'tool';
    const args = rec(p.arguments);
    const done = events.find(e => e.type === 'tool_completed' && str(rec(e.payload).id) === str(p.id) && str(p.id) !== null);
    const result = done ? rec(rec(done.payload).result) : {};
    const failed = done ? rec(done.payload).is_error === true : false;
    if (name === 'note_progress') {
      const noted = events.find(e => e.type === 'progress_noted' && e.sequence > sequence);
      const n = noted ? rec(noted.payload) : args;
      return { kind: 'note', phase: str(n.phase) ?? '未命名阶段', summary: str(n.summary) ?? '', time };
    }
    if (COMMAND_TOOLS.has(name)) {
      const logs = rec(result.logs);
      const out = tail(str(logs.stdout) ?? str(result.stdout));
      const err = tail(str(logs.stderr) ?? str(result.stderr));
      const job = str(args.job_id);
      return {
        kind: 'command', name, time, running: !done,
        command: str(args.command) ?? (job ? `${name} ${job.slice(0, 12)}` : name),
        stdout: out.text, stderr: err.text,
        exitCode: int(result.exit_code) ?? int(logs.exit_code), state: str(result.state),
        truncated: out.cut || err.cut || logs.output_truncated === true || result.output_truncated === true,
        error: failed ? toolError(result) : null,
      };
    }
    return { kind: 'tool', name, title: toolLabel(name, args), args, running: !done, result: done ? rec(done.payload).result : null, error: failed ? toolError(result) : null, time };
  }
  if (event.type === 'progress_noted') {
    return { kind: 'note', phase: str(p.phase) ?? '未命名阶段', summary: str(p.summary) ?? '', time };
  }
  const call = specialistCalls(events, task).find(c => c.sequence === sequence);
  if (call) return { kind: 'delegation', call };
  if (event.type === 'artifact_frozen' && str(p.name)) {
    return {
      kind: 'artifact', name: str(p.name)!, evidenceKind: str(p.kind), sha256: str(p.sha256), bytes: int(p.bytes),
      round: int(p.round), sourcePath: str(p.source_path), path: artifactPath(str(p.name)!, str(p.protocol_sha256), int(p.round)), time,
    };
  }
  const label = eventLabel(event);
  return { kind: 'event', type: event.type, label: label?.label ?? event.type, detail: label?.detail ?? null, payload: event.payload, time };
}
