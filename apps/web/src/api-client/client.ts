// 统一请求层：组件只见 ApiClient 接口与 ApiClientError；
// mock 与 live 在此切换（契约：切换仅在 config 层，不改组件字段结构）。
import type {
  ApiError, Attempt, Capabilities, Confirmation, CodeFile, Experiment, ExperimentCreate,
  Export, ExportAccepted, ExportCreate, Extraction, ID, Operation, PageData,
  Paper, PaperAccepted, Project, ProjectCreate, Report, ReportPending,
  RevisionConfirm, RevisionCreate, Run, RunAccepted, RunCreate, RunCurves,
  RunManifest, SessionInfo, SpecDraft, SpecRevision, TextChunk,
} from './types';

export class ApiClientError extends Error {
  readonly api: ApiError;
  readonly requestId: string;
  readonly status: number;
  constructor(api: ApiError, requestId: string, status: number) {
    super(api.message);
    this.name = 'ApiClientError';
    this.api = api;
    this.requestId = requestId;
    this.status = status;
  }
}

export interface UploadPaperInput {
  file?: File;
  arxivId?: string;
  titleHint?: string;
  consent: boolean;
}

export interface ApiClient {
  login(inviteToken: string): Promise<SessionInfo>;
  getSession(): Promise<SessionInfo>;
  logout(): Promise<void>;

  getCapabilities(): Promise<Capabilities>;

  listProjects(): Promise<PageData<Project>>;
  createProject(input: ProjectCreate): Promise<Project>;

  listPapers(projectId: ID): Promise<PageData<Paper>>;
  getPaper(paperId: ID): Promise<Paper>;
  uploadPaper(projectId: ID, input: UploadPaperInput): Promise<PaperAccepted>;
  getExtraction(paperId: ID): Promise<{ kind: 'extraction'; extraction: Extraction } | { kind: 'accepted'; accepted: PaperAccepted }>;
  getOperation(operationId: ID): Promise<Operation>;

  listExperiments(projectId: ID, paperId?: ID): Promise<PageData<Experiment>>;
  createExperiment(projectId: ID, input: ExperimentCreate): Promise<Experiment>;
  getDraft(experimentId: ID): Promise<SpecDraft>;
  saveRevision(projectId: ID, input: RevisionCreate): Promise<SpecRevision>;
  confirmRevision(revisionId: ID, input: RevisionConfirm): Promise<Confirmation>;
  createRun(projectId: ID, input: RunCreate): Promise<RunAccepted>;

  listRuns(projectId: ID): Promise<PageData<Run>>;
  getRun(runId: ID): Promise<Run>;
  cancelRun(runId: ID): Promise<Run>;
  listAttempts(runId: ID): Promise<Attempt[]>;
  listFiles(attemptId: ID): Promise<CodeFile[]>;
  getArtifactText(artifactId: ID, offset: number): Promise<TextChunk>;
  getCurves(runId: ID): Promise<RunCurves>;

  getReport(runId: ID): Promise<{ kind: 'report'; report: Report } | { kind: 'pending'; pending: ReportPending }>;
  getManifest(runId: ID): Promise<RunManifest>;
  listExports(runId: ID): Promise<Export[]>;
  createExport(runId: ID, input: ExportCreate): Promise<ExportAccepted>;
  downloadArtifact(artifactId: ID): Promise<void>;
}

// ============ live 实现（F2+ 联调用；走同源 /api/v1，代理到 127.0.0.1:8000）============
const BASE = '/api/v1';

interface Envelope<T> { data: T; meta: { request_id: string } }

function uuid(): string {
  return crypto.randomUUID();
}

async function request<T>(
  method: string,
  path: string,
  opts: { body?: unknown; idempotencyKey?: string } = {},
): Promise<Envelope<T>> {
  const csrf = sessionStorage.getItem('p2r.csrf') ?? undefined;
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: 'same-origin',
    headers: {
      ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      ...(opts.idempotencyKey ? { 'Idempotency-Key': opts.idempotencyKey } : {}),
    },
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  const requestId = res.headers.get('X-Request-ID') ?? 'unknown';
  if (res.status === 204) return { data: undefined as T, meta: { request_id: requestId } };
  const json = (await res.json().catch(() => null)) as
    | { data: T; meta: { request_id: string } }
    | { error: ApiError; meta: { request_id: string } }
    | null;
  if (!res.ok || !json || 'error' in json) {
    const api: ApiError = json && 'error' in json ? json.error : {
      code: 'UNKNOWN', message: `HTTP ${res.status}`, retryable: false, details: {},
    };
    throw new ApiClientError(api, requestId, res.status);
  }
  return json;
}

async function requestPage<T>(path: string): Promise<PageData<T>> {
  const env = (await request<T[]>('GET', path)) as Envelope<T[]> & { page?: { next_cursor: string | null; has_more: boolean } };
  return { items: env.data, has_more: env.page?.has_more ?? false, next_cursor: env.page?.next_cursor ?? null };
}

export function createLiveClient(): ApiClient {
  return {
    async login(inviteToken) {
      const env = await request<SessionInfo>('POST', '/auth/session', { body: { invite_token: inviteToken } });
      sessionStorage.setItem('p2r.csrf', env.data.csrf_token);
      return env.data;
    },
    async getSession() {
      const env = await request<SessionInfo>('GET', '/auth/me');
      sessionStorage.setItem('p2r.csrf', env.data.csrf_token);
      return env.data;
    },
    async logout() {
      await request<void>('DELETE', '/auth/session');
      sessionStorage.removeItem('p2r.csrf');
    },
    async getCapabilities() {
      return (await request<Capabilities>('GET', '/capabilities')).data;
    },
    async listProjects() {
      return requestPage<Project>('/projects?limit=20');
    },
    async createProject(input) {
      return (await request<Project>('POST', '/projects', { body: input, idempotencyKey: uuid() })).data;
    },
    async listPapers(projectId) {
      return requestPage<Paper>(`/projects/${projectId}/papers?limit=20`);
    },
    async getPaper(paperId) {
      return (await request<Paper>('GET', `/papers/${paperId}`)).data;
    },
    async uploadPaper(projectId, input) {
      if (!input.consent) {
        throw new ApiClientError({ code: 'CONSENT_REQUIRED', message: '未同意论文内容处理，不能上传', retryable: false, details: {} }, 'local', 0);
      }
      let body: BodyInit;
      const headers: Record<string, string> = {};
      if (input.file) {
        const fd = new FormData();
        fd.append('file', input.file);
        if (input.titleHint) fd.append('title_hint', input.titleHint);
        fd.append('source_processing_consent', 'true');
        body = fd;
      } else if (input.arxivId) {
        headers['Content-Type'] = 'application/json';
        body = JSON.stringify({ source_type: 'arxiv', arxiv_id: input.arxivId, source_processing_consent: true });
      } else {
        throw new ApiClientError({ code: 'NO_SOURCE', message: '缺少 PDF 文件或 arXiv ID', retryable: false, details: {} }, 'local', 0);
      }
      const res = await fetch(`${BASE}/projects/${projectId}/papers`, {
        method: 'POST', credentials: 'same-origin',
        headers: { ...headers, 'X-CSRF-Token': sessionStorage.getItem('p2r.csrf') ?? '', 'Idempotency-Key': uuid() },
        body,
      });
      const json = await res.json();
      if (!res.ok) throw new ApiClientError(json.error, res.headers.get('X-Request-ID') ?? 'unknown', res.status);
      return json.data as PaperAccepted;
    },
    async getExtraction(paperId) {
      const res = await fetch(`${BASE}/papers/${paperId}/extraction`, { credentials: 'same-origin' });
      const json = await res.json();
      if (!res.ok) throw new ApiClientError(json.error, res.headers.get('X-Request-ID') ?? 'unknown', res.status);
      if (res.status === 202) return { kind: 'accepted', accepted: json.data as PaperAccepted };
      return { kind: 'extraction', extraction: json.data as Extraction };
    },
    async getOperation(operationId) {
      return (await request<Operation>('GET', `/operations/${operationId}`)).data;
    },
    async listExperiments(projectId, paperId) {
      const q = paperId ? `?paper_id=${paperId}&limit=20` : '?limit=20';
      return requestPage<Experiment>(`/projects/${projectId}/experiments${q}`);
    },
    async createExperiment(projectId, input) {
      return (await request<Experiment>('POST', `/projects/${projectId}/experiments`, { body: input, idempotencyKey: uuid() })).data;
    },
    async getDraft(experimentId) {
      return (await request<SpecDraft>('GET', `/experiments/${experimentId}/draft`)).data;
    },
    async saveRevision(projectId, input) {
      return (await request<SpecRevision>('POST', `/projects/${projectId}/spec-revisions`, { body: input, idempotencyKey: uuid() })).data;
    },
    async confirmRevision(revisionId, input) {
      return (await request<Confirmation>('POST', `/spec-revisions/${revisionId}/confirm`, { body: input, idempotencyKey: uuid() })).data;
    },
    async createRun(projectId, input) {
      return (await request<RunAccepted>('POST', `/projects/${projectId}/runs`, { body: input, idempotencyKey: uuid() })).data;
    },
    async listRuns(projectId) {
      return requestPage<Run>(`/projects/${projectId}/runs?limit=20`);
    },
    async getRun(runId) {
      return (await request<Run>('GET', `/runs/${runId}`)).data;
    },
    async cancelRun(runId) {
      return (await request<Run>('POST', `/runs/${runId}/cancel`, { body: {} })).data;
    },
    async listAttempts(runId) {
      return (await requestPage<Attempt>(`/runs/${runId}/attempts?limit=20`)).items;
    },
    async listFiles(attemptId) {
      return (await requestPage<CodeFile>(`/attempts/${attemptId}/files?limit=50`)).items;
    },
    async getArtifactText(artifactId, offset) {
      return (await request<TextChunk>('GET', `/artifacts/${artifactId}/text?offset=${offset}&limit=65536`)).data;
    },
    async getCurves(runId) {
      return (await request<RunCurves>('GET', `/runs/${runId}/curves`)).data;
    },
    async getReport(runId) {
      const res = await fetch(`${BASE}/runs/${runId}/report`, { credentials: 'same-origin' });
      const json = await res.json();
      if (!res.ok) throw new ApiClientError(json.error, res.headers.get('X-Request-ID') ?? 'unknown', res.status);
      if (res.status === 202) return { kind: 'pending', pending: json.data as ReportPending };
      return { kind: 'report', report: json.data as Report };
    },
    async getManifest(runId) {
      return (await request<RunManifest>('GET', `/runs/${runId}/manifest`)).data;
    },
    async listExports(runId) {
      return (await requestPage<Export>(`/runs/${runId}/exports?limit=20`)).items;
    },
    async createExport(runId, input) {
      return (await request<ExportAccepted>('POST', `/runs/${runId}/exports`, { body: input, idempotencyKey: uuid() })).data;
    },
    async downloadArtifact(artifactId) {
      window.location.assign(`${BASE}/artifacts/${artifactId}/download`);
    },
  };
}
