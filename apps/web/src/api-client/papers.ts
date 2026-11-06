// 项目、论文上传与 MinerU PDF 解析（docparse）的 API。
// 这些端点不在 /api/v1/autonomous 前缀下，但自主实验流程是它们的唯一消费者。
// 写请求带 Idempotency-Key；密钥类内容任何情况下不经过本模块。
import {
  ApiError, asUnknown, boolean, csrfToken, envelopeData, errorEnvelope, initializeSession, invalid, isAbort, isSessionReady,
  nonNegativeInt, nullableStr, record, responsePayload, str, type Fetcher,
} from './client.ts';

export interface Page<T> { items: T[]; has_more: boolean; next_cursor: string | null }

export interface ProjectInfo { id: string; name: string; role?: string; version?: number; created_at?: string }

export interface ProjectAccepted {
  id: string; name: string; role: string; version: number; created_at: string;
}

export interface PaperAccepted {
  paper_id: string; operation_id: string | null; status: string;
}

export interface PaperInfo {
  paper_id: string; project_id: string; title: string | null; status: string;
  sha256: string; page_count: number | null; content_available: boolean;
  current_extraction_id: string | null; active_operation_id: string | null;
  error: { code: string; message: string } | null; created_at: string;
  paper_version_id?: string; current_version_id?: string;
  /** 存储的来源类型（pdf / text）与媒体类型；旧后端不返回时缺省。 */
  source_kind?: string; media_type?: string;
}

// --- MinerU PDF parsing (docparse) ---
// GET /docparse/status、PUT /docparse/mode 与离线安装路由都返回这个 DTO。
// mode 为 null 表示用户还没有选择解析方式。
export type DocParseMode = 'online' | 'offline';
export type DocParseOfflineState = 'not_installed' | 'installing' | 'ready' | 'broken' | 'failed' | 'unsupported';

export interface DocParseOfflineStatus {
  state: DocParseOfflineState;
  step: string | null;
  progress: number;
  tier: string | null;
  version: string | null;
  bytes_on_disk: number;
  error: string | null;
}

export interface DocParseStatus {
  mode: DocParseMode | null;
  offline: DocParseOfflineStatus;
  platform: string;
}

// POST /papers/{paper_id}/parse (202).
export interface DocParseAccepted {
  paper_id: string;
  job_id: string;
  status: 'parsing';
}

// GET /papers/{paper_id}/text。解析中的 chunk 是进度标记；完成后携带页码范围。
export type PaperTextStatus = 'none' | 'parsing' | 'ready' | 'failed';
export type PaperTextBackend = 'online_agent' | 'offline_mineru';

export interface PaperTextChunk {
  status: string;
  first_page?: number;
  last_page?: number;
}

export interface PaperText {
  status: PaperTextStatus;
  backend: PaperTextBackend | null;
  page_count: number;
  pages_parsed: number;
  chunks: PaperTextChunk[];
  warnings: string[];
  error_code: string | null;
  preview: string | null;
}

const BASE = '/api/v1';
const DOCPARSE_MODES = new Set<DocParseMode>(['online', 'offline']);
const DOCPARSE_OFFLINE_STATES = new Set<DocParseOfflineState>(['not_installed', 'installing', 'ready', 'broken', 'failed', 'unsupported']);
const PAPER_TEXT_STATUS = new Set<PaperTextStatus>(['none', 'parsing', 'ready', 'failed']);
const PAPER_TEXT_BACKENDS = new Set<PaperTextBackend>(['online_agent', 'offline_mineru']);

export function validateDocParseStatus(value: unknown): DocParseStatus {
  const item = record(value, 'docparse status must be an object');
  if (item.mode !== null && !DOCPARSE_MODES.has(item.mode as DocParseMode)) invalid('docparse status.mode is unsupported');
  const offline = record(item.offline, 'docparse status.offline must be an object');
  if (!DOCPARSE_OFFLINE_STATES.has(offline.state as DocParseOfflineState)) invalid('docparse status.offline.state is unsupported');
  const progress = offline.progress;
  if (typeof progress !== 'number' || !Number.isFinite(progress) || progress < 0 || progress > 1) invalid('docparse status.offline.progress must be a number in [0, 1]');
  // 安装器的自由文本（step/tier/version/error）可以为空，只校验类型。
  const text = (v: unknown, path: string): string | null => {
    if (v !== null && typeof v !== 'string') invalid(`${path} must be a string or null`);
    return v as string | null;
  };
  return {
    mode: item.mode as DocParseMode | null,
    offline: {
      state: offline.state as DocParseOfflineState,
      step: text(offline.step, 'docparse status.offline.step'),
      progress,
      tier: text(offline.tier, 'docparse status.offline.tier'),
      version: text(offline.version, 'docparse status.offline.version'),
      bytes_on_disk: nonNegativeInt(offline.bytes_on_disk, 'docparse status.offline.bytes_on_disk'),
      error: text(offline.error, 'docparse status.offline.error'),
    },
    platform: str(item.platform, 'docparse status.platform'),
  };
}

export function validateDocParseAccepted(value: unknown): DocParseAccepted {
  const item = record(value, 'parse accepted must be an object');
  if (item.status !== 'parsing') invalid('parse accepted.status must be parsing');
  return {
    paper_id: str(item.paper_id, 'parse accepted.paper_id'),
    job_id: str(item.job_id, 'parse accepted.job_id'),
    status: 'parsing',
  };
}

function validatePaperTextChunk(value: unknown, index: number): PaperTextChunk {
  const item = record(value, `paper text.chunks[${index}] must be an object`);
  const chunk: PaperTextChunk = { status: str(item.status, `paper text.chunks[${index}].status`) };
  if (item.first_page !== undefined) chunk.first_page = nonNegativeInt(item.first_page, `paper text.chunks[${index}].first_page`);
  if (item.last_page !== undefined) chunk.last_page = nonNegativeInt(item.last_page, `paper text.chunks[${index}].last_page`);
  return chunk;
}

export function validatePaperText(value: unknown): PaperText {
  const item = record(value, 'paper text must be an object');
  if (!PAPER_TEXT_STATUS.has(item.status as PaperTextStatus)) invalid('paper text.status is unsupported');
  if (item.backend !== null && !PAPER_TEXT_BACKENDS.has(item.backend as PaperTextBackend)) invalid('paper text.backend is unsupported');
  if (!Array.isArray(item.chunks)) invalid('paper text.chunks must be an array');
  if (!Array.isArray(item.warnings) || item.warnings.some(w => typeof w !== 'string')) invalid('paper text.warnings must be an array of strings');
  // 空的 Markdown 预览也是真实预览，只校验类型。
  if (item.preview !== null && typeof item.preview !== 'string') invalid('paper text.preview must be a string or null');
  return {
    status: item.status as PaperTextStatus,
    backend: item.backend as PaperTextBackend | null,
    page_count: nonNegativeInt(item.page_count, 'paper text.page_count'),
    pages_parsed: nonNegativeInt(item.pages_parsed, 'paper text.pages_parsed'),
    chunks: item.chunks.map(validatePaperTextChunk),
    warnings: [...item.warnings as string[]],
    error_code: nullableStr(item.error_code, 'paper text.error_code'),
    preview: item.preview as string | null,
  };
}

function validateProjectAccepted(value: unknown): ProjectAccepted {
  const item = record(value, 'project accepted must be an object');
  return {
    id: str(item.id, 'project accepted.id'),
    name: str(item.name, 'project accepted.name'),
    role: str(item.role, 'project accepted.role'),
    version: nonNegativeInt(item.version, 'project accepted.version'),
    created_at: str(item.created_at, 'project accepted.created_at'),
  };
}

function validatePaperAccepted(value: unknown): PaperAccepted {
  const item = record(value, 'paper accepted must be an object');
  return {
    paper_id: str(item.paper_id, 'paper accepted.paper_id'),
    operation_id: nullableStr(item.operation_id, 'paper accepted.operation_id'),
    status: str(item.status, 'paper accepted.status'),
  };
}

function validatePaperInfo(value: unknown): PaperInfo {
  const item = record(value, 'paper info must be an object');
  const pageCount = item.page_count;
  if (pageCount !== null && typeof pageCount !== 'number') invalid('paper info.page_count must be a number or null');
  const error = item.error === null || item.error === undefined ? null : record(item.error, 'paper info.error must be an object or null');
  const version = item.paper_version_id ?? item.current_version_id;
  return {
    paper_id: str(item.paper_id, 'paper info.paper_id'),
    project_id: str(item.project_id, 'paper info.project_id'),
    title: nullableStr(item.title, 'paper info.title'),
    status: str(item.status, 'paper info.status'),
    sha256: str(item.sha256, 'paper info.sha256'),
    page_count: pageCount === null ? null : nonNegativeInt(pageCount, 'paper info.page_count'),
    content_available: boolean(item.content_available, 'paper info.content_available'),
    current_extraction_id: nullableStr(item.current_extraction_id, 'paper info.current_extraction_id'),
    active_operation_id: nullableStr(item.active_operation_id, 'paper info.active_operation_id'),
    error: error ? { code: str(error.code, 'paper info.error.code'), message: typeof error.message === 'string' ? error.message : str(error.code, 'paper info.error.code') } : null,
    created_at: str(item.created_at, 'paper info.created_at'),
    ...(typeof version === 'string' && version ? { paper_version_id: version } : {}),
    ...(typeof item.source_kind === 'string' && item.source_kind ? { source_kind: item.source_kind } : {}),
    ...(typeof item.media_type === 'string' && item.media_type ? { media_type: item.media_type } : {}),
  };
}

export interface PapersApi {
  listProjects(signal?: AbortSignal): Promise<ProjectInfo[]>;
  createProject(name: string, key?: string): Promise<ProjectAccepted>;
  listPapers(projectId: string, signal?: AbortSignal): Promise<PaperInfo[]>;
  getPaper(paperId: string, signal?: AbortSignal): Promise<PaperInfo>;
  uploadPaperPdf(projectId: string, file: File, options?: { titleHint?: string; key?: string }): Promise<PaperAccepted>;
  createTextPaper(projectId: string, input: { text: string; titleHint?: string; key?: string }): Promise<PaperAccepted>;
  getDocParseStatus(signal?: AbortSignal): Promise<DocParseStatus>;
  setDocParseMode(mode: DocParseMode): Promise<DocParseStatus>;
  startOfflineInstall(): Promise<DocParseStatus>;
  cancelOfflineInstall(): Promise<DocParseStatus>;
  uninstallOffline(): Promise<DocParseStatus>;
  parsePaper(paperId: string, key: string): Promise<DocParseAccepted>;
  getPaperText(paperId: string, signal?: AbortSignal): Promise<PaperText>;
}

// csrf：测试或调用方注入的 token 读取函数；缺省时用 client.ts 内存中的会话 token（与 autonomous.ts 相同）。
export function createPapersApi(actorId: string, fetcher: Fetcher = fetch, csrf?: () => string | undefined): PapersApi {
  if (!actorId) throw new ApiError('actor ID is required', 0, 'INVALID_ACTOR');

  async function send(path: string, init: RequestInit, signal?: AbortSignal): Promise<Response> {
    const write = !!init.method && !['GET', 'HEAD'].includes(init.method);
    if (write && !csrf && !isSessionReady() && fetcher === fetch) await initializeSession();
    const headers = new Headers(init.headers);
    headers.set('X-Actor-ID', actorId);
    if (!headers.has('Accept')) headers.set('Accept', 'application/json');
    const token = write ? csrf?.() ?? csrfToken() : undefined;
    if (token) headers.set('X-CSRF-Token', token);
    try {
      return await fetcher(`${BASE}${path}`, { ...init, headers, credentials: 'same-origin', cache: 'no-store', ...(signal ? { signal } : {}) });
    } catch (error) {
      if (isAbort(error, signal)) throw error;
      throw new ApiError('网络连接失败；写入结果可能未知，请刷新后再操作。', 0, 'NETWORK_ERROR', undefined, undefined, true);
    }
  }

  async function read<T>(path: string, validator: (value: unknown) => T, signal?: AbortSignal): Promise<T> {
    const response = await send(path, { method: 'GET' }, signal);
    const payload = await responsePayload(response);
    if (!response.ok) throw errorEnvelope(payload, response);
    const { data } = envelopeData(payload, response.status, response.headers.get('X-Request-ID') ?? undefined);
    return validator(data);
  }

  // 列表端点的 data 必须是真实数组；分页信息存在 page 字段中（可选）。
  async function readArray<T>(path: string, validator: (value: unknown) => T, signal?: AbortSignal): Promise<Page<T>> {
    const response = await send(path, { method: 'GET' }, signal);
    const payload = await responsePayload(response);
    if (!response.ok) throw errorEnvelope(payload, response);
    const { data, requestId } = envelopeData(payload, response.status, response.headers.get('X-Request-ID') ?? undefined);
    if (!Array.isArray(data)) throw new ApiError('分页 data 必须是真实数组。', response.status, 'INVALID_ENVELOPE', requestId);
    const envelope = payload as Record<string, unknown>;
    if (!('page' in envelope)) return { items: data.map(validator), has_more: false, next_cursor: null };
    const page = record(envelope.page, 'page must be an object');
    const hasMore = boolean(page.has_more, 'page.has_more');
    const cursor = page.next_cursor;
    if (cursor !== null && typeof cursor !== 'string') invalid('page.next_cursor must be string or null');
    return { items: data.map(validator), has_more: hasMore, next_cursor: cursor };
  }

  // 直接写：每次调用使用调用方给的或新生成的 Idempotency-Key。
  // 2xx 但响应无法验证时，写入结果未知，提示用户先查询而不是自动重试。
  async function write<T>(path: string, body: unknown, validator: (value: unknown) => T, key?: string): Promise<T> {
    const headers = new Headers({ 'Content-Type': 'application/json', 'Idempotency-Key': key ?? crypto.randomUUID() });
    const response = await send(path, { method: 'POST', headers, body: JSON.stringify(body) });
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    if (!response.ok) throw errorEnvelope(await responsePayload(response, response.status >= 500), response);
    try {
      const { data } = envelopeData(await responsePayload(response, true), response.status, requestId, true);
      return validator(data);
    } catch (error) {
      throw asUnknown(error, `写请求已返回成功状态但响应无法验证；写入结果可能未知。请刷新查询后再操作。(${response.status})`, response.status, 'UNKNOWN_WRITE_RESULT', requestId);
    }
  }

  // docparse 模式 / 安装器命令本身幂等，不带 Idempotency-Key。
  async function docParseCommand(method: 'PUT' | 'POST' | 'DELETE', path: string, body?: unknown): Promise<DocParseStatus> {
    const init: RequestInit = body === undefined
      ? { method }
      : { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
    const response = await send(path, init);
    const requestId = response.headers.get('X-Request-ID') ?? undefined;
    if (!response.ok) throw errorEnvelope(await responsePayload(response, response.status >= 500), response);
    try {
      const { data } = envelopeData(await responsePayload(response, true), response.status, requestId, true);
      return validateDocParseStatus(data);
    } catch (error) {
      throw asUnknown(error, `解析设置已返回成功状态但响应无法验证；请重新查询解析状态。(${response.status})`, response.status, 'UNKNOWN_WRITE_RESULT', requestId);
    }
  }

  const enc = encodeURIComponent;
  return {
    async listProjects(signal) {
      const page = await readArray('/projects', v => {
        const item = record(v, 'project must be an object');
        const out: ProjectInfo = { id: str(item.id, 'project.id'), name: typeof item.name === 'string' && item.name ? item.name : str(item.id, 'project.id') };
        return out;
      }, signal);
      return page.items;
    },
    createProject(name, key) {
      return write('/projects', { name, description: 'Autonomous experiment flow' }, validateProjectAccepted, key);
    },
    async listPapers(projectId, signal) {
      const page = await readArray(`/projects/${enc(projectId)}/papers`, v => v, signal);
      const papers: PaperInfo[] = [];
      for (const raw of page.items) {
        const item = record(raw, 'paper must be an object');
        const id = item.paper_id ?? item.id;
        if (typeof id !== 'string' || !id) continue;
        papers.push({
          paper_id: id,
          project_id: projectId,
          title: typeof item.title === 'string' ? item.title : null,
          status: typeof item.status === 'string' ? item.status : 'unknown',
          sha256: '', page_count: null, content_available: false,
          current_extraction_id: null, active_operation_id: null, error: null,
          created_at: typeof item.created_at === 'string' ? item.created_at : '',
        });
      }
      return papers;
    },
    getPaper(paperId, signal) {
      return read(`/papers/${enc(paperId)}`, validatePaperInfo, signal);
    },
    async uploadPaperPdf(projectId, file, options = {}) {
      const form = new FormData();
      form.append('file', file, file.name);
      if (options.titleHint) form.append('title_hint', options.titleHint);
      form.append('source_processing_consent', 'true');
      // multipart 边界由浏览器自己设置；Idempotency-Key 固定这次提交。
      const response = await send(`/projects/${enc(projectId)}/papers`, {
        method: 'POST',
        headers: { 'Idempotency-Key': options.key ?? crypto.randomUUID() },
        body: form,
      });
      const requestId = response.headers.get('X-Request-ID') ?? undefined;
      if (!response.ok) throw errorEnvelope(await responsePayload(response, response.status >= 500), response);
      try {
        const { data } = envelopeData(await responsePayload(response, true), response.status, requestId, true);
        return validatePaperAccepted(data);
      } catch (error) {
        throw asUnknown(error, '上传已返回成功状态但响应无法验证；结果可能未知，请勿重复上传。', response.status, 'UNKNOWN_WRITE_RESULT', requestId);
      }
    },
    createTextPaper(projectId, input) {
      const body: Record<string, unknown> = { text: input.text, source_processing_consent: true };
      if (input.titleHint) body.title_hint = input.titleHint;
      return write(`/projects/${enc(projectId)}/papers/text`, body, validatePaperAccepted, input.key);
    },
    getDocParseStatus(signal) {
      return read('/docparse/status', validateDocParseStatus, signal);
    },
    async setDocParseMode(mode) {
      if (!DOCPARSE_MODES.has(mode)) invalid('docparse mode must be online or offline');
      return docParseCommand('PUT', '/docparse/mode', { mode });
    },
    startOfflineInstall() {
      return docParseCommand('POST', '/docparse/offline/install');
    },
    cancelOfflineInstall() {
      return docParseCommand('POST', '/docparse/offline/cancel');
    },
    uninstallOffline() {
      return docParseCommand('DELETE', '/docparse/offline');
    },
    async parsePaper(paperId, key) {
      if (!key) invalid('parse requires a non-empty idempotency key');
      const accepted = await write(`/papers/${enc(paperId)}/parse`, {}, validateDocParseAccepted, key);
      if (accepted.paper_id !== paperId) invalid('parse accepted does not match the requested paper');
      return accepted;
    },
    getPaperText(paperId, signal) {
      return read(`/papers/${enc(paperId)}/text`, validatePaperText, signal);
    },
  };
}
