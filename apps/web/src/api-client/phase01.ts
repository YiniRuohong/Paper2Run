import type { Session, Cell, Action, Event, Checkpoint, Recovery, CheckpointBody } from './phase01-types';
export type { Session, Cell, Action, Event, Checkpoint, Recovery, CheckpointBody } from './phase01-types';

export class ApiError extends Error {
  status: number;
  code?: string;
  requestId?: string;
  details?: unknown;
  constructor(message: string, status: number, code?: string, requestId?: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }
}

/** A completed write must not be repeated just because its follow-up read failed. */
export function snapshotRefreshError(reason: unknown): Error {
  const message = `写请求已返回，快照刷新失败；请手动刷新，勿重复写入。${reason instanceof Error ? reason.message : String(reason)}`;
  return reason instanceof ApiError
    ? new ApiError(message, reason.status, reason.code, reason.requestId, reason.details)
    : new Error(message);
}

/** The Phase 0/1 endpoint closes after returning a finite SSE snapshot. */
export function parseEventSnapshot(text: string): Event[] {
  return text.replace(/\r\n/g, '\n').split(/\n\n+/).flatMap((block) => {
    const data = block.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).replace(/^ /, '')).join('\n');
    if (!data) return [];
    try {
      const event = JSON.parse(data) as Event;
      if (!Number.isInteger(event.sequence_no) || typeof event.type !== 'string' || !event.payload) throw new Error();
      return [event];
    } catch { throw new ApiError('事件快照格式无效，未显示伪造事件。', 200, 'INVALID_EVENT'); }
  });
}

/** Only use metadata from an actual cell response; never invent hashes. */
export async function buildCheckpoint(name: string, cell: Cell, actions: Action[]): Promise<CheckpointBody> {
  if (!/^[a-f0-9]{64}$/i.test(cell.namespace_hash) || !cell.environment_id) {
    throw new Error('缺少真实执行元数据，请先执行 cell。');
  }
  const latest = actions.reduce<Action | undefined>((last, action) => !last || action.sequence_no > last.sequence_no ? action : last, undefined);
  if (!latest || latest.cell_id !== cell.id) throw new Error('当前 cell 不是最新 action，请重新执行后创建 checkpoint。');
  const action_cursor = latest.sequence_no + 1;
  // The backend canonicalizes JSON with sorted keys and no whitespace.
  const manifest = { action_cursor, environment_id: cell.environment_id, input_artifact_ids: [] as string[] };
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(manifest)));
  const manifest_hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  return { name, action_cursor, namespace_snapshot_hash: cell.namespace_hash, input_artifact_ids: [], environment_id: cell.environment_id, replay_plan: {}, manifest_hash, manifest };
}

export function createApi(actorId: string, fetcher: typeof fetch = fetch) {
  // Memory only, scoped to this actor/client. Never automatically retry a write.
  // An explicit repeat after an ambiguous response reuses its exact body and key.
  const pendingWrites = new Map<string, string>();
  const identity = (path: string, body: string) => JSON.stringify([path, body]);

  async function request(path: string, body?: string): Promise<Response> {
    const headers = new Headers({ 'X-Actor-ID': actorId, Accept: 'application/json, text/event-stream' });
    if (body !== undefined) {
      headers.set('Content-Type', 'application/json');
      const operation = identity(path, body);
      const key = pendingWrites.get(operation) ?? crypto.randomUUID();
      pendingWrites.set(operation, key);
      headers.set('Idempotency-Key', key);
    }
    let response: Response;
    try {
      response = await fetcher(path, { method: body === undefined ? 'GET' : 'POST', headers, credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : { body }) });
    } catch {
      // No automatic retries: the server may already have accepted a write.
      throw new ApiError('网络连接失败；写入结果可能未知，请先刷新核对，不要直接重复提交。确认本地 API 与 Vite proxy 已启动。', 0, 'NETWORK_ERROR');
    }
    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({}));
      throw new ApiError(errorBody?.error?.message || `请求失败 (HTTP ${response.status})`, response.status, errorBody?.error?.code, errorBody?.meta?.request_id || errorBody?.request_id || response.headers.get('X-Request-ID') || undefined, errorBody?.error?.details);
    }
    return response;
  }
  async function json<T>(path: string, body?: unknown, raw = false): Promise<T> {
    const serialized = body === undefined ? undefined : JSON.stringify(body);
    const response = await request(path, serialized);
    const requestId = response.headers.get('X-Request-ID') || undefined;
    const result = await response.json().catch(() => { throw new ApiError('API 未返回有效 JSON；请检查代理或后端。', response.status, 'INVALID_JSON', requestId); });
    if (!raw && (!result || typeof result !== 'object' || !('data' in result))) throw new ApiError('API 成功响应缺少 data。', response.status, 'INVALID_ENVELOPE', requestId);
    if (serialized !== undefined) pendingWrites.delete(identity(path, serialized));
    return (raw ? result : result.data) as T;
  }
  const sessionPath = (id: string) => `/api/v1/sessions/${encodeURIComponent(id)}`;
  return {
    health: () => json<{ status: string; service: string }>('/healthz', undefined, true),
    sessions: (project: string) => json<Session[]>(`/api/v1/projects/${encodeURIComponent(project)}/sessions`),
    createSession: (project: string) => json<Session>(`/api/v1/projects/${encodeURIComponent(project)}/sessions`, { trust_class: 'untrusted', metadata: {} }),
    session: (id: string) => json<Session>(sessionPath(id)),
    cell: (id: string, source: string) => json<Cell>(`${sessionPath(id)}/cells`, { session_id: id, source, requested_capabilities: [] }),
    actions: (id: string, after = -1) => json<Action[]>(`${sessionPath(id)}/actions?limit=100&after=${after}`),
    events: async (id: string, after = -1) => parseEventSnapshot(await (await request(`${sessionPath(id)}/events?limit=100&after=${after}`)).text()),
    checkpoints: (id: string) => json<Checkpoint[]>(`${sessionPath(id)}/checkpoints?limit=100`),
    checkpoint: (id: string, payload: CheckpointBody) => json<Checkpoint>(`${sessionPath(id)}/checkpoints`, payload),
    restore: (id: string, checkpoint_id: string) => json<Recovery>(`${sessionPath(id)}/restore`, { checkpoint_id }),
  };
}
