import { GuidedApiError, type Fetcher } from './guided.ts';
import { createLiveClient } from './client.ts';

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
  limits: { wall_time_seconds: number; max_model_calls: number; max_model_tokens: number; storage_bytes: number; gpu_devices: string[] };
  network: { public_internet: true; host_and_metadata_access: false };
  experiment: { objective: string; protocol_change_policy: 'version_and_pause_if_outside_authorization' };
}
export interface ProtocolApproval {
  id: string; kind: 'protocol_approval'; proposed: { protocol: Record<string, unknown>; sha256: string }; changed: string[]; reason: string;
}
export interface AutonomousTask {
  id: string; project_id: string; state: string; objective: string; config_snapshot: ProjectConfig;
  usage: Record<string, unknown>; created_at: string; updated_at: string; report?: unknown; error?: unknown; pending_decision?: unknown;
}
export interface AutonomousEvent { sequence: number; type: string; payload: unknown; created_at: string }
export interface AutonomousFile {
  path: string; source: 'artifact' | 'workspace'; bytes: number; media_type: string; modified_at: string | null;
  sha256: string | null; kind: string | null; protocol_sha256: string | null;
}
export interface AutonomousFiles { items: AutonomousFile[]; truncated: boolean; workspace_available: boolean }
const enc = encodeURIComponent;
let memoryCsrf: string | undefined;
let sessionReady = false;
let sessionLoading: Promise<void> | undefined;
export async function initializeAutonomousSession() {
  if (sessionLoading) return sessionLoading;
  sessionLoading = (async () => {
    try { memoryCsrf = (await createLiveClient().getSession()).csrf_token; sessionReady = true; }
    catch (error) {
      if (!(error instanceof Error && 'status' in error && error.status === 401)) throw error;
      memoryCsrf = undefined; sessionReady = true;
    }
  })();
  try { await sessionLoading; } finally { sessionLoading = undefined; }
}
export const authenticatedGuidedFetch: Fetcher = async (input, init = {}) => {
  if (!sessionReady && init.method && !['GET', 'HEAD'].includes(init.method)) await initializeAutonomousSession();
  const headers = new Headers(init.headers);
  if (memoryCsrf) headers.set('X-CSRF-Token', memoryCsrf);
  return fetch(input, { ...init, headers });
};
export function createAutonomousApi(actorId = 'demo-user', fetcher: Fetcher = fetch, csrf?: () => string | undefined) {
  const initializeSession = initializeAutonomousSession;
  async function send(path: string, method = 'GET', body?: unknown, version?: number | string, signal?: AbortSignal) {
    if (method !== 'GET' && !csrf && !sessionReady && fetcher === fetch) await initializeSession();
    const headers = new Headers({ 'X-Actor-ID': actorId, Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (method !== 'GET') headers.set('Idempotency-Key', crypto.randomUUID());
    const token = csrf?.() ?? memoryCsrf; if (token) headers.set('X-CSRF-Token', token);
    if (version !== undefined) headers.set('If-Match', String(version));
    try {
      return await fetcher(`/api/v1/autonomous${path}`, { method, headers, credentials: 'same-origin', cache: 'no-store', body: body === undefined ? undefined : JSON.stringify(body), signal });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new GuidedApiError('网络连接失败；写入结果可能未知，请刷新任务列表后再操作。', 0, 'NETWORK_ERROR');
    }
  }
  async function request<T>(path: string, method = 'GET', body?: unknown, version?: number | string, signal?: AbortSignal): Promise<T> {
    const res = await send(path, method, body, version, signal);
    const json = await res.json().catch(() => null);
    if (!res.ok || !json || !('data' in json) || json.error) throw new GuidedApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'INVALID_ENVELOPE', json?.meta?.request_id ?? res.headers.get('X-Request-ID') ?? undefined);
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
      if (!res.ok) { const json = await res.json().catch(() => null); throw new GuidedApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'FILE_READ_FAILED'); }
      return res.blob();
    },
    async exportTask(id: string) {
      const res = await send(`/tasks/${enc(id)}/export`);
      if (!res.ok) { const json = await res.json().catch(() => null); throw new GuidedApiError(json?.error?.message ?? `HTTP ${res.status}`, res.status, json?.error?.code ?? 'EXPORT_FAILED'); }
      const url = URL.createObjectURL(await res.blob());
      const a = document.createElement('a'); a.href = url; a.download = `paper2run-${id}.zip`; a.click(); URL.revokeObjectURL(url);
    },
  };
}
export type AutonomousApi = ReturnType<typeof createAutonomousApi>;
