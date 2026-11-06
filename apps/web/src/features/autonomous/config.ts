import type { ModelProfile, ProjectConfig, ResearchTaskType, SaveProfileRequest } from '../../api-client/autonomous.ts';
import type { Tone } from '../../ui/Card.tsx';

export const researchTaskTypes: { value: ResearchTaskType; label: string }[] = [
  { value: 'paper_analysis', label: '论文分析' }, { value: 'method_design', label: '方法设计' },
  { value: 'coding', label: '代码建议' }, { value: 'data_analysis', label: '数据分析' }, { value: 'review', label: '科研审查' },
];
/** Prototype-safe lookup of a backend string in a plain-object table ("constructor" is not a key). */
export function own<T>(table: Record<string, T>, key: unknown): T | undefined {
  return typeof key === 'string' && Object.hasOwn(table, key) ? table[key] : undefined;
}
export const researchTaskTypeLabel = (value: string) => researchTaskTypes.find(type => type.value === value)?.label ?? value;
const EVIDENCE_KIND_LABELS: Record<string, string> = { code: '代码', environment: '环境', data: '数据', result: '结果' };
export const evidenceKindLabel = (kind: string) => own(EVIDENCE_KIND_LABELS, kind) ?? kind;
const profileName = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
export function modelReference(profile?: ModelProfile): ProjectConfig['model'] {
  return profile ? { provider_profile: profile.id, credential_ref: profile.credential_ref ?? `profile:${profile.id}` } : { provider_profile: '', credential_ref: '' };
}
export function reusableProfiles(profile: ModelProfile, profiles: ModelProfile[]): ModelProfile[] {
  return profiles.filter(source => source.auth_mode !== 'none' && source.secret_configured && !source.credential_ref && source.base_url === profile.base_url.trim() && source.api === profile.api);
}
export function profileRequest(profile: ModelProfile, key: string, reuse: string, profiles: ModelProfile[]): SaveProfileRequest {
  if (!profileName.test(profile.id) || !profile.model.trim()) throw new Error('请填写有效配置 ID 和模型名称。');
  let url: URL;
  try { url = new URL(profile.base_url.trim()); } catch { throw new Error('请输入有效的 Base URL。'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('Base URL 必须是无凭据、查询参数和片段的 HTTP(S) 地址。');
  const settings = { id: profile.id, model: profile.model.trim(), base_url: profile.base_url.trim(), api: profile.api, auth_mode: profile.auth_mode ?? 'api-key' };
  if (settings.auth_mode === 'none') {
    const loopback = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
    if (url.protocol !== 'http:' || !loopback) throw new Error('无认证连接仅允许 HTTP 回环地址（localhost、127.0.0.1 或 [::1]）。');
    return settings;
  }
  if (reuse) {
    if (key) throw new Error('输入新密钥与复用已有凭据只能选择一种。');
    if (!reusableProfiles(profile, profiles).some(source => source.id === reuse)) throw new Error('只能复用同一 Base URL 和 API 协议下已配置的凭据。');
    return { ...settings, reuse_credentials_from: reuse };
  }
  if (key && !key.trim()) throw new Error('API Key 不能为空白。');
  return { ...settings, ...(key ? { api_key: key } : {}) };
}

export function defaultConfig(projectId: string): ProjectConfig {
  return {
    schema_version: 'paper2run.project.v1', project: { id: projectId, workspace: `./workspace/${projectId}` },
    model: { provider_profile: '', credential_ref: '' }, harness: { adapter: 'pi', skills: [] },
    target: { kind: 'local', profile: 'local', allocation: 'dedicated', isolation: 'required', image: 'paper2run-experiment:1' },
    limits: { storage_bytes: 5 * 1024 ** 3, max_repair_rounds: DEFAULT_REPAIR_ROUNDS, gpu_devices: [] },
    network: { public_internet: true, host_and_metadata_access: false },
    experiment: { objective: '', protocol_change_policy: 'version_and_pause_if_outside_authorization' },
  };
}
/** 不限时长、调用次数和 tokens：读到旧配置里的这些上限时去掉，下次保存即不再带上限。 */
export function withoutBudgetCaps(config: ProjectConfig): ProjectConfig {
  const { max_model_tokens: _tokens, max_model_calls: _calls, wall_time_seconds: _wall, ...limits } = config.limits;
  return { ...config, limits };
}
export function parseProtocol(text: string): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined;
  const value: unknown = JSON.parse(text);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('实验协议必须是 JSON 对象。');
  return value as Record<string, unknown>;
}
export function validateConfig(config: ProjectConfig): void {
  if (!config.project.id || !config.project.workspace.trim()) throw new Error('请选择项目并填写工作区。');
  if (!config.model.provider_profile || !config.model.credential_ref) throw new Error('请先连接或选择模型配置。');
  const specialists = config.specialists === undefined ? [] : config.specialists;
  if (!Array.isArray(specialists) || specialists.length > 16) throw new Error('科研专家最多配置 16 个。');
  const ids = new Set<string>();
  for (const specialist of specialists) {
    if (!specialist || typeof specialist.id !== 'string' || !profileName.test(specialist.id)) throw new Error('专家 ID 必须为 1–128 位字母、数字、下划线、点或短横线，且以字母或数字开头。');
    if (ids.has(specialist.id)) throw new Error('专家 ID 不能重复。');
    ids.add(specialist.id);
    if (typeof specialist.description !== 'string' || !specialist.description.trim() || specialist.description.length > 8192) throw new Error('专家专长描述不能为空且最多 8192 字符。');
    if (!Array.isArray(specialist.task_types) || !specialist.task_types.length || new Set(specialist.task_types).size !== specialist.task_types.length || specialist.task_types.some(value => !researchTaskTypes.some(type => type.value === value))) throw new Error('每位专家须选择至少一种有效任务类型，且不能重复。');
    if (!specialist.model?.provider_profile || !specialist.model.credential_ref) throw new Error('请为每位专家选择已有模型配置。');
  }
  if (!config.experiment.objective.trim()) throw new Error('请填写实验目标。');
  if (!config.target.profile.trim()) throw new Error('请填写目标配置名称。');
  if (config.target.kind === 'ssh' && (!config.target.ssh_config_alias?.trim() || !config.target.remote_workspace?.startsWith('/'))) throw new Error('SSH 需要配置别名和绝对远程工作区路径。');
  for (const [key, value] of Object.entries(config.limits)) {
    if (key === 'gpu_devices' || (BUDGET_CAPS.has(key) && value == null)) continue; // 未设置即不限
    // 修复轮次允许 0（只复核一次，不修复）；其余预算必须为正。
    if (key === 'max_repair_rounds') {
      // 输入框清空时页面存为 NaN（保存时会变成 null）：要求填写，不当作 0。
      if (value === null || (typeof value === 'number' && Number.isNaN(value))) throw new Error(`请填写修复轮次上限（0–${MAX_REPAIR_ROUNDS}）。`);
      if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0 || value > MAX_REPAIR_ROUNDS) throw new Error(`修复轮次上限必须为 0–${MAX_REPAIR_ROUNDS} 的整数。`);
      continue;
    }
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) throw new Error('预算必须为有限的正整数。');
  }
}
const BUDGET_CAPS = new Set(['max_model_tokens', 'max_model_calls', 'wall_time_seconds']);
export const DEFAULT_REPAIR_ROUNDS = 3;
export const MAX_REPAIR_ROUNDS = 10;
/** 后端 driver.max_repair_rounds：旧任务快照没有这个键时按 3 处理。 */
export function maxRepairRounds(snapshot: { limits?: { max_repair_rounds?: unknown } } | null | undefined): number {
  const value = snapshot?.limits?.max_repair_rounds;
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : DEFAULT_REPAIR_ROUNDS;
}
export function protocolApproval(value: unknown): import('../../api-client/autonomous.ts').ProtocolApproval | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const proposed = item.proposed as Record<string, unknown> | null;
  if (item.kind !== 'protocol_approval' || typeof item.id !== 'string' || !item.id || !proposed || typeof proposed.sha256 !== 'string' || !proposed.protocol || typeof proposed.protocol !== 'object' || Array.isArray(proposed.protocol) || !Array.isArray(item.changed) || !item.changed.every(x => typeof x === 'string') || typeof item.reason !== 'string') return null;
  return value as import('../../api-client/autonomous.ts').ProtocolApproval;
}
export const terminalState = (state: string) => ['completed', 'succeeded', 'failed', 'stopped', 'cancelled', 'incomplete'].includes(state);
const STATE_LABELS: Record<string, string> = { created: '已创建', queued: '排队中', starting: '启动中', running: '运行中', paused: '已暂停', blocked: '已阻塞', awaiting_decision: '等待决策', stopping: '正在停止', completed: '已完成', succeeded: '已完成', incomplete: '未完成', failed: '失败', stopped: '已停止', cancelled: '已取消' };
export const stateLabel = (state: string) => own(STATE_LABELS, state) ?? state;
export const taskStateLabel = stateLabel;
// 任务状态到 tone 的唯一映射：进行中 run；需要人（暂停/阻塞/待决策）warn；
// 失败 bad；正常完成 ok；其余 idle。不新造颜色。
// 停止、取消、无进展结束都不是「完成」：没有结局时用 idle，不配绿。
export const taskTone = (state: string): 'ok' | 'run' | 'bad' | 'warn' | 'idle' =>
  state === 'failed' ? 'bad'
    : state === 'paused' || state === 'blocked' || state === 'awaiting_decision' ? 'warn'
    : state === 'completed' || state === 'succeeded' ? 'ok'
    : state === 'running' || state === 'starting' || state === 'queued' ? 'run'
    : 'idle';

// ---- 结局（程序判定，driver.finish 写入）----
// 有结局的任务就是已结束：即使生命周期状态是 stopped / paused，也不再显示
// 「已暂停 / 后台作业可能仍在运行」。生命周期状态作为次要文字保留。
export type Outcome = 'verified' | 'completed_with_issues' | 'review_unavailable' | 'blocked_by_safety';
const OUTCOMES: Record<Outcome, { label: string; tone: Tone; caption: string | null }> = {
  verified: { label: '已复核', tone: 'ok', caption: '复核通过不等于复现成立；可比性见报告' },
  completed_with_issues: { label: '完成，有未解决问题', tone: 'warn', caption: null },
  review_unavailable: { label: '复核不可用', tone: 'warn', caption: null },
  blocked_by_safety: { label: '安全终止', tone: 'bad', caption: null },
};
export const knownOutcome = (outcome: unknown): outcome is Outcome => typeof outcome === 'string' && Object.hasOwn(OUTCOMES, outcome);
export const outcomeLabel = (outcome: unknown): string | null =>
  knownOutcome(outcome) ? OUTCOMES[outcome].label : typeof outcome === 'string' && outcome ? `未知结局（${outcome}）` : null;
export const outcomeTone = (outcome: unknown): Tone | null =>
  knownOutcome(outcome) ? OUTCOMES[outcome].tone : typeof outcome === 'string' && outcome ? 'idle' : null;

/** 报告 outcome_reason 的说明；未登记的值原样返回（未知就写未知）。 */
const OUTCOME_REASONS: Record<string, string> = { review_failed: '独立复核多次未能完成', budget_exhausted: '预算耗尽', safety_blocked: '容器隔离校验失败' };
export const outcomeReasonLabel = (reason: string | null): string | null =>
  reason === null ? null
    : own(OUTCOME_REASONS, reason) ?? reason;

export interface TaskDisplay {
  /** 主状态词：有结局时是结局，否则是生命周期状态。 */
  text: string;
  tone: Tone;
  caption: string | null;
  /** 有结局时的生命周期状态（次要文字）；无结局时为 null。 */
  lifecycle: string | null;
  finished: boolean;
  /** 是否显示「已暂停 / 后台作业可能仍在运行」类提示。 */
  pausedNotice: boolean;
}
/** 待决定是否仍需要人处理：任务已结束、正在停止或已有结局时，决定无法再回答，不显示。
 * 后端停止时会撤回决定（decision_withdrawn）；这里兜住旧任务和竞争窗口。 */
export function decisionVisible(task: { state: string; outcome?: string | null; pending_decision?: unknown }): boolean {
  return task.pending_decision != null && !terminalState(task.state) && task.state !== 'stopping' && task.outcome == null;
}
export function taskDisplay(task: { state: string; outcome?: string | null; pending_decision?: unknown }): TaskDisplay {
  const outcome = task.outcome ?? null;
  if (outcome) {
    return { text: outcomeLabel(outcome)!, tone: outcomeTone(outcome)!, caption: knownOutcome(outcome) ? OUTCOMES[outcome].caption : null,
      lifecycle: stateLabel(task.state), finished: true, pausedNotice: false };
  }
  const tone: Tone = decisionVisible(task) ? 'warn' : taskTone(task.state);
  return { text: stateLabel(task.state), tone, caption: null, lifecycle: null, finished: terminalState(task.state), pausedNotice: task.state === 'paused' };
}
/** 总览筛选：安全终止归入「失败」，其余有结局的任务归入「已结束」。 */
export function taskBucket(task: { state: string; outcome?: string | null }): 'active' | 'done' | 'failed' {
  if (task.state === 'failed' || task.outcome === 'blocked_by_safety') return 'failed';
  return taskDisplay(task).finished ? 'done' : 'active';
}

// 后端错误码的中文说明：附在后端消息之后（保留 missing: … 等细节），并附 code 与 request_id。
const ERROR_EXPLANATIONS: Record<string, string> = {
  TASK_FINISHED: '任务已有结局，不能继续；可以导出 ZIP 保留证据',
  LEGACY_TASK: '该任务创建于新版 harness 之前，只能导出',
  DECISION_PENDING: '有待处理的方案变更：请先在上方批准或拒绝，再继续',
  DECISION_MISMATCH: '该决定已被处理或已过期，已刷新任务',
  CANDIDATE_UNDER_REVIEW: '候选结果正在独立复核，证据已冻结；复核结束后才能修改',
  CANDIDATE_INCOMPLETE: '候选结果不满足提交条件，详见事件日志',
  TASK_BUSY: '上一个执行进程仍在暂停，请稍后重试',
  BUDGET_EXHAUSTED: '任务预算（模型调用次数、tokens 或运行时间）已用完，任务已自动停止；已完成的证据仍可导出',
  CREDENTIAL_REQUIRED: '模型密钥只保存在内存中，应用重启后需要重新输入；在本页「需要恢复」卡片中重新输入后继续',
  CANCELLATION_PENDING: '任务正在停止，等待后台作业结束',
  PAPER_CONTENT_REQUIRED: '论文正文尚未就绪，请先完成解析',
  CSRF_FAILED: '请求来源被拒绝；请从启动器打开的页面操作',
};
export const errorExplanation = (code: string): string | null => own(ERROR_EXPLANATIONS, code) ?? null;
/** 决定提交冲突：两次点击竞争或决定已被替换。刷新任务即可，不当作错误。 */
export const decisionSuperseded = (code: string) => code === 'INVALID_STATE' || code === 'DECISION_MISMATCH';

/** 模型流式输出（sdk_event）最多保留最近这么多条；节点类事件和专家调用事件不设上限。 */
export const STREAM_EVENT_KEEP = 300;
const isSpecialistEvent = (e: import('../../api-client/autonomous.ts').AutonomousEvent) => {
  const p = (e.payload && typeof e.payload === 'object' ? e.payload : {}) as Record<string, unknown>;
  return p.type === 'research_delegation_started' || p.type === 'research_delegation_completed' || (p.type === 'message_end' && p.delegated === true);
};

// 合并事件：按序号去重排序。时间线、报告和用量都靠节点类事件（工具调用、冻结证据、复核……），
// 它们一条都不能丢；占绝大多数的是流式输出，只留最近一段。
export function mergeEvents(previous: import('../../api-client/autonomous.ts').AutonomousEvent[], incoming: import('../../api-client/autonomous.ts').AutonomousEvent[]) {
  const all = [...new Map([...previous, ...incoming].map(e => [e.sequence, e])).values()].sort((a, b) => a.sequence - b.sequence);
  const dropped = new Set<number>();
  let streamed = 0;
  for (let i = all.length - 1; i >= 0; i--) {
    const e = all[i];
    if (e.type !== 'sdk_event' || isSpecialistEvent(e)) continue;
    if (++streamed > STREAM_EVENT_KEEP) dropped.add(e.sequence);
  }
  return dropped.size === 0 ? all : all.filter(e => !dropped.has(e.sequence));
}
