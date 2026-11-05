// 共享请求基座：ApiError、Fetcher、会话引导（CSRF）、API 信封解析与字段校验。
// api-client 下的所有模块（autonomous、papers）只用这里的错误类型与信封规则，
// 组件只见 ApiError，不直接拼 fetch。
export type Fetcher = (input: string, init?: RequestInit) => Promise<Response>;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | undefined;
  readonly details: unknown;
  readonly unknown: boolean;

  constructor(message: string, status: number, code: string, requestId?: string, details?: unknown, unknown = false) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
    this.unknown = unknown;
  }
}

// ---- 会话引导（CSRF） -------------------------------------------------------
// CSRF token 只保存在内存中：sessionStorage / localStorage 会让注入脚本可读。
let memoryCsrf: string | undefined;
let sessionReady = false;
let sessionLoading: Promise<void> | undefined;

export function csrfToken(): string | undefined {
  return memoryCsrf;
}

export function isSessionReady(): boolean {
  return sessionReady;
}

// GET /api/v1/auth/me 取回 CSRF token。401 表示本地免登录部署，不算错误：
// 会话照常标记就绪，只是没有 token 可发。
export async function initializeSession(): Promise<void> {
  if (sessionLoading) return sessionLoading;
  sessionLoading = (async () => {
    try {
      const res = await fetch('/api/v1/auth/me', { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
      if (res.status === 401) {
        memoryCsrf = undefined;
        sessionReady = true;
        return;
      }
      const payload: unknown = await res.json().catch(() => null);
      if (!res.ok) throw errorEnvelope(payload, res);
      const { data } = envelopeData(payload, res.status, res.headers.get('X-Request-ID') ?? undefined);
      const item = record(data, 'session must be an object');
      const token = item.csrf_token;
      if (typeof token !== 'string' || !token) invalid('session.csrf_token must be a non-empty string');
      memoryCsrf = token;
      sessionReady = true;
    } finally {
      /* sessionLoading 在下方 finally 中复位 */
    }
  })();
  try { await sessionLoading; } finally { sessionLoading = undefined; }
}

// ---- 信封与错误 -------------------------------------------------------------

export function invalid(message: string): never {
  throw new ApiError(message, 200, 'INVALID_DTO');
}

export function errorEnvelope(payload: unknown, response: Response): ApiError {
  const requestId = response.headers.get('X-Request-ID') ?? undefined;
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return new ApiError(`请求失败 (HTTP ${response.status})`, response.status, 'UNKNOWN_ERROR', requestId, undefined, true);
  }
  const envelope = payload as Record<string, unknown>;
  const error = envelope.error && typeof envelope.error === 'object' && !Array.isArray(envelope.error)
    ? envelope.error as Record<string, unknown>
    : null;
  if (!error) return new ApiError(`请求失败 (HTTP ${response.status})`, response.status, 'UNKNOWN_ERROR', requestId, undefined, true);
  const code = typeof error.code === 'string' ? error.code : 'UNKNOWN_ERROR';
  const message = typeof error.message === 'string' ? error.message : `请求失败 (HTTP ${response.status})`;
  const metaId = envelope.meta && typeof envelope.meta === 'object' && !Array.isArray(envelope.meta)
    ? (envelope.meta as Record<string, unknown>).request_id
    : undefined;
  return new ApiError(message, response.status, code, typeof metaId === 'string' ? metaId : requestId, error.details, response.status >= 500);
}

export async function responsePayload(response: Response, unknown = false): Promise<unknown> {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    throw new ApiError('API 未返回有效 JSON；请检查代理或后端。', response.status, 'INVALID_JSON', response.headers.get('X-Request-ID') ?? undefined, undefined, unknown);
  }
}

export function envelopeData(payload: unknown, status: number, requestId?: string, unknown = false): { data: unknown; requestId?: string } {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new ApiError('API 成功响应缺少 data。', status, 'INVALID_ENVELOPE', requestId, undefined, unknown);
  }
  const envelope = payload as Record<string, unknown>;
  const meta = envelope.meta && typeof envelope.meta === 'object' && !Array.isArray(envelope.meta)
    ? envelope.meta as Record<string, unknown>
    : undefined;
  const resolvedRequestId = typeof meta?.request_id === 'string' ? meta.request_id : requestId;
  if (!('data' in envelope)) {
    throw new ApiError('API 成功响应缺少 data。', status, 'INVALID_ENVELOPE', resolvedRequestId, undefined, unknown);
  }
  return { data: envelope.data, requestId: resolvedRequestId };
}

export function isAbort(error: unknown, signal?: AbortSignal): boolean {
  return signal?.aborted === true
    || (error instanceof DOMException && error.name === 'AbortError')
    || (error instanceof Error && error.name === 'AbortError');
}

export function asUnknown(error: unknown, fallbackMessage: string, status: number, code: string, requestId?: string, details?: unknown): ApiError {
  if (error instanceof ApiError) {
    if (error.unknown || error.code === 'NETWORK_ERROR' || error.code === 'INTENT_ACCEPTED' || error.code === 'INTENT_UNKNOWN') return error;
    return new ApiError(error.message, error.status, error.code, error.requestId ?? requestId, error.details ?? details, true);
  }
  return new ApiError(fallbackMessage, status, code, requestId, details, true);
}

// ---- 字段校验 ---------------------------------------------------------------

export function record(value: unknown, message = 'expected an object'): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid(message);
  return value as Record<string, unknown>;
}

export function str(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.length === 0) invalid(`${path} must be a non-empty string`);
  return value;
}

export function nullableStr(value: unknown, path: string): string | null {
  if (value === null) return null;
  if (typeof value !== 'string' || value.length === 0) invalid(`${path} must be a non-empty string or null`);
  return value;
}

export function int(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value)) invalid(`${path} must be an integer`);
  return value;
}

export function nonNegativeInt(value: unknown, path: string): number {
  const parsed = int(value, path);
  if (parsed < 0) invalid(`${path} must be non-negative`);
  return parsed;
}

export function boolean(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') invalid(`${path} must be a boolean`);
  return value;
}
