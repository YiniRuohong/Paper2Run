import { ApiError, csrfToken, initializeSession, isSessionReady, type Fetcher } from './client.ts';

export { ApiError };

export interface Capability { id: string; name: string; version: string; description: string; enabled: boolean }
export interface ModelProfile { id: string; base_url: string; model: string; api: 'anthropic-messages' | 'openai-completions'; auth_mode?: 'api-key' | 'none'; credential_ref?: string; secret_configured?: boolean }
export type SaveProfileRequest = ModelProfile & { api_key?: string; context_window?: number; max_tokens?: number; reuse_credentials_from?: string };
export type ResearchTaskType = 'paper_analysis' | 'method_design' | 'coding' | 'data_analysis' | 'review';
export interface ResearchSpecialist { id: string; description: string; task_types: ResearchTaskType[]; model: { provider_profile: string; credential_ref: string } }
export interface ProjectConfig {
  schema_version: 'paper2run.project.v1'; project: { id: string; workspace: string };
  model: { provider_profile: string; credential_ref: string };
  specialists?: ResearchSpecialist[];
  harness: { adapter: 'pi'; skills: string[] };
  target: { kind: 'local' | 'ssh'; profile: string; ssh_config_alias?: string; remote_workspace?: string; allocation: 'dedicated'; isolation: 'required'; image: 'paper2run-experiment:1' };
  /** max_repair_rounds: 0–10；旧配置没有该键，后端按 3 处理。 */
  limits: { wall_time_seconds: number; max_model_calls: number; max_model_tokens: number; storage_bytes: number; max_repair_rounds?: number; gpu_devices: string[] };
  network: { public_internet: true; host_and_metadata_access: false };
  experiment: { objective: string; protocol_change_policy: 'version_and_pause_if_outside_authorization' };
}
export interface ProtocolApproval {
  id: string; kind: 'protocol_approval'; proposed: { protocol: Record<string, unknown>; sha256: string }; changed: string[]; reason: string;
}
export type TaskOutcome = 'verified' | 'completed_with_issues' | 'review_unavailable' | 'blocked_by_safety';
export type ReviewStatus = 'pending' | 'issues' | 'pass' | 'unavailable';
export interface CandidateArtifact {
  name: string; kind: string; sha256: string; bytes: number; source_path: string; round: number; sequence: number; protocol_sha256: string;
}
/** 冻结候选快照（candidate.py freeze）。 */
export interface TaskCandidate {
  snapshot_sha256: string; round: number; protocol_sha256: string;
  protocol?: { protocol: Record<string, unknown>; sha256: string };
  artifacts: CandidateArtifact[];
  run: { job_id: string; exit_code: number; sequence: number };
  summary: string; review_status: ReviewStatus; attempts: number;
}
export interface ReviewFinding { description: string; evidence: string[] }
export interface TaskReview { snapshot_sha256: string; round: number; attempt: number; verdict: 'pass' | 'issues'; findings: ReviewFinding[] }
export interface AutonomousTask {
  id: string; project_id: string; state: string; objective: string; config_snapshot: ProjectConfig;
  usage: Record<string, unknown>; created_at: string; updated_at: string; report?: unknown; error?: unknown; pending_decision?: unknown;
  /** 程序判定的结局；null 表示尚未判定。旧后端不返回这些字段。 */
  outcome?: TaskOutcome | null;
  repair_round?: number;
  candidate?: TaskCandidate | null;
  reviews?: TaskReview[];
}
export interface AutonomousEvent { sequence: number; type: string; payload: unknown; created_at: string }
export interface AutonomousFile {
  path: string; source: 'artifact' | 'workspace'; bytes: number; media_type: string; modified_at: string | null;
  sha256: string | null; kind: string | null; protocol_sha256: string | null;
}
export interface AutonomousFiles { items: AutonomousFile[]; truncated: boolean; workspace_available: boolean }
const enc = encodeURIComponent;
// 会话引导与 CSRF 由 client.ts 统一管理；保留这两个导出名供既有调用方使用。
export const initializeAutonomousSession = initializeSession;
export const authenticatedGuidedFetch: Fetcher = async (input, init = {}) => {
  if (!isSessionReady() && init.method && !['GET', 'HEAD'].includes(init.method)) await initializeSession();
  const headers = new Headers(init.headers);
  const token = csrfToken();
  if (token) headers.set('X-CSRF-Token', token);
  return fetch(input, { ...init, headers });
};
export function createAutonomousApi(actorId = 'demo-user', fetcher: Fetcher = fetch, csrf?: () => string | undefined) {
  async function send(path: string, method = 'GET', body?: unknown, version?: number | string, signal?: AbortSignal) {
    if (method !== 'GET' && !csrf && !isSessionReady() && fetcher === fetch) await initializeSession();
    const headers = new Headers({ 'X-Actor-ID': actorId, Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (method !== 'GET') headers.set('Idempotency-Key', crypto.randomUUID());
    const token = csrf?.() ?? csrfToken(); if (token) headers.set('X-CSRF-Token', token);
    if (version !== undefined) headers.set('If-Match', String(version));
    try {
      return await fetcher(`/api/v1/autonomous${path}`, { method, headers, credentials: 'same-origin', cache: 'no-store', body: body === undefined ? undefined : JSON.stringify(body), signal });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError('网络连接失败；写入结果可能未知，请刷新任务列表后再操作。', 0, 'NETWORK_ERROR');
    }
  }
  async function request<T>(path: string, method = 'GET', body?: unknown, version?: number | string, signal?: AbortSignal): Promise<T> {
    const res = await send(path, method, body, version, signal);
    const json = await res.json().catch(() => null);
    if (!res.ok || !json || !('data' in json) || json.error) throw new ApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'INVALID_ENVELOPE', json?.meta?.request_id ?? res.headers.get('X-Request-ID') ?? undefined);
    return json.data as T;
  }
  return {
    initializeSession,
    capabilities: () => request<Capability[]>('/capabilities'),
    importSkill: (source_path: string) => request<Capability>('/capabilities/import', 'POST', { source_path }),
    enableSkill: (id: string, enabled: boolean) => request<Capability>(`/capabilities/${enc(id)}`, 'PUT', { enabled }),
    profiles: () => request<ModelProfile[]>('/profiles'),
    saveProfile: (profile: SaveProfileRequest) => {
      const { secret_configured: _configured, api_key, credential_ref, reuse_credentials_from, ...settings } = profile;
      return request<ModelProfile>('/profiles', 'POST', {
        ...settings, auth_mode: profile.auth_mode ?? 'api-key',
        ...(profile.auth_mode === 'none' ? {} : { api_key, credential_ref, reuse_credentials_from }),
      });
    },
    config: (id: string) => request<{ version: number; config: ProjectConfig }>(`/projects/${enc(id)}/config`),
    saveConfig: (id: string, config: ProjectConfig, version?: number) => request<{ version: number; config: ProjectConfig }>(`/projects/${enc(id)}/config`, 'PUT', { config }, version),
    tasks: (id: string) => request<AutonomousTask[]>(`/projects/${enc(id)}/tasks`),
    start: (id: string, input: { objective: string; paper_version_id?: string; protocol?: Record<string, unknown>; authorization: { confirmed: true } }) => request<AutonomousTask>(`/projects/${enc(id)}/tasks`, 'POST', input),
    task: (id: string, signal?: AbortSignal) => request<AutonomousTask>(`/tasks/${enc(id)}`, 'GET', undefined, undefined, signal),
    events: (id: string, after: number, signal?: AbortSignal) => request<{ events: AutonomousEvent[]; next_cursor: number }>(`/tasks/${enc(id)}/events?after=${after}`, 'GET', undefined, undefined, signal),
    control: (id: string, action: 'pause' | 'resume' | 'stop' | 'message', message?: string) => request<AutonomousTask>(`/tasks/${enc(id)}/control`, 'POST', { action, ...(message ? { message } : {}) }),
    files: (id: string, signal?: AbortSignal) => request<AutonomousFiles>(`/tasks/${enc(id)}/files`, 'GET', undefined, undefined, signal),
    async fileBlob(id: string, file: Pick<AutonomousFile, 'source' | 'path'>, signal?: AbortSignal) {
      const res = await send(`/tasks/${enc(id)}/files/raw?source=${enc(file.source)}&path=${enc(file.path)}`, 'GET', undefined, undefined, signal);
      if (!res.ok) { const json = await res.json().catch(() => null); throw new ApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'FILE_READ_FAILED'); }
      return res.blob();
    },
    async exportTask(id: string) {
      const res = await send(`/tasks/${enc(id)}/export`);
      if (!res.ok) { const json = await res.json().catch(() => null); throw new ApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'EXPORT_FAILED'); }
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement('a'); a.href = url; a.download = `paper2run-${id}.zip`; a.click(); URL.revokeObjectURL(url);
    },
  };
}
export type AutonomousApi = ReturnType<typeof createAutonomousApi>;
