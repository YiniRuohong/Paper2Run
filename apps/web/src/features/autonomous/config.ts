import type { ModelProfile, ProjectConfig, ResearchTaskType, SaveProfileRequest } from '../../api-client/autonomous.ts';

export const researchTaskTypes: { value: ResearchTaskType; label: string }[] = [
  { value: 'paper_analysis', label: '论文分析' }, { value: 'method_design', label: '方法设计' },
  { value: 'coding', label: '代码建议' }, { value: 'data_analysis', label: '数据分析' }, { value: 'review', label: '科研审查' },
];
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
    limits: { wall_time_seconds: 1800, max_model_calls: 30, max_model_tokens: 100000, storage_bytes: 5 * 1024 ** 3, gpu_devices: [] },
    network: { public_internet: true, host_and_metadata_access: false },
    experiment: { objective: '', protocol_change_policy: 'version_and_pause_if_outside_authorization' },
  };
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
    if (key !== 'gpu_devices' && (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0)) throw new Error('预算必须为有限的正整数。');
  }
}
export function protocolApproval(value: unknown): import('../../api-client/autonomous.ts').ProtocolApproval | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  const proposed = item.proposed as Record<string, unknown> | null;
  if (item.kind !== 'protocol_approval' || typeof item.id !== 'string' || !item.id || !proposed || typeof proposed.sha256 !== 'string' || !proposed.protocol || typeof proposed.protocol !== 'object' || Array.isArray(proposed.protocol) || !Array.isArray(item.changed) || !item.changed.every(x => typeof x === 'string') || typeof item.reason !== 'string') return null;
  return value as import('../../api-client/autonomous.ts').ProtocolApproval;
}
export const terminalState = (state: string) => ['completed', 'succeeded', 'failed', 'stopped', 'cancelled'].includes(state);
export const stateLabel = (state: string) => ({ created: '已创建', queued: '排队中', starting: '启动中', running: '运行中', paused: '已暂停', blocked: '已阻塞', awaiting_decision: '等待决策', completed: '已完成', succeeded: '已完成', failed: '失败', stopped: '已停止', cancelled: '已取消' }[state] ?? state);
export function mergeEvents(previous: import('../../api-client/autonomous.ts').AutonomousEvent[], incoming: import('../../api-client/autonomous.ts').AutonomousEvent[]) {
  return [...new Map([...previous, ...incoming].map(e => [e.sequence, e])).values()].sort((a, b) => a.sequence - b.sequence).slice(-500);
}
