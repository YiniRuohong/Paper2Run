import type { GuidedApi } from '../../api-client/guided.ts';
import { GuidedApiError } from '../../api-client/guided.ts';

// Fault-tolerant, read-only loading for the overview. Parsers are lenient on
// purpose: a malformed row is dropped (and reported) instead of failing the
// whole page, and each project/resource is loaded independently.

export interface PaperLite { id: string; projectId: string; title: string | null; status: string; createdAt: string | null }
export interface ExperimentLite { id: string; paperId: string | null }
export interface TaskRun {
  id: string; experimentId: string | null; status: string; verdict: string | null; mode: string | null;
  createdAt: string | null; errorCode: string | null;
}
export interface Snapshot { papers: PaperLite[]; experiments: ExperimentLite[]; runs: TaskRun[] }
export interface LoadIssue { what: string; reason: string }
export interface LoadResult { snapshot: Snapshot; issues: LoadIssue[] }

const rec = (v: unknown): Record<string, unknown> | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? v as Record<string, unknown> : null);
const str = (v: unknown): string | null => (typeof v === 'string' && v.length > 0 ? v : null);

const parseProject = (v: unknown) => { const r = rec(v); const id = r && str(r.id); return id ? { id } : null; };
const parsePaper = (v: unknown): PaperLite | null => {
  const r = rec(v); const id = r && (str(r.paper_id) ?? str(r.id));
  if (!r || !id) return null;
  return { id, projectId: str(r.project_id) ?? '', title: str(r.title), status: str(r.status) ?? 'unknown', createdAt: str(r.created_at) };
};
const parseExperiment = (v: unknown): ExperimentLite | null => {
  const r = rec(v); const id = r && (str(r.experiment_id) ?? str(r.id));
  return r && id ? { id, paperId: str(r.paper_id) } : null;
};
const parseRun = (v: unknown): TaskRun | null => {
  const r = rec(v); const id = r && (str(r.run_id) ?? str(r.id));
  if (!r || !id) return null;
  const err = rec(r.error);
  return { id, experimentId: str(r.experiment_id), status: str(r.status) ?? 'unknown', verdict: str(r.verdict), mode: str(r.mode), createdAt: str(r.created_at), errorCode: err ? str(err.code) : null };
};

function status(error: unknown): number | null {
  return error instanceof GuidedApiError ? error.status : null;
}

async function listOf<T>(api: GuidedApi, path: string, parse: (v: unknown) => T | null, signal: AbortSignal): Promise<{ items: T[]; dropped: number }> {
  const raw: unknown[] = [];
  // The validator only collects; parsing happens below so one bad row can't throw.
  await api.readArray(path, (v: unknown) => { raw.push(v); return v; }, signal);
  const items: T[] = [];
  for (const entry of raw) { const parsed = parse(entry); if (parsed) items.push(parsed); }
  return { items, dropped: raw.length - items.length };
}

export async function loadSnapshot(api: GuidedApi, signal: AbortSignal): Promise<LoadResult> {
  const issues: LoadIssue[] = [];
  let projects: { id: string }[];
  try {
    projects = (await listOf(api, '/projects', parseProject, signal)).items;
  } catch (error) {
    // An uninitialised local user is not an outage: it simply has no tasks yet.
    if (status(error) === 403) return { snapshot: { papers: [], experiments: [], runs: [] }, issues };
    throw error;
  }
  const snapshot: Snapshot = { papers: [], experiments: [], runs: [] };
  await Promise.all(projects.map(async project => {
    const pid = encodeURIComponent(project.id);
    const jobs: Array<[string, Promise<void>]> = [
      ['论文', listOf(api, `/projects/${pid}/papers`, parsePaper, signal).then(r => { snapshot.papers.push(...r.items); if (r.dropped) issues.push({ what: `${project.id} 论文`, reason: `${r.dropped} 条记录格式异常，已跳过` }); })],
      ['实验', listOf(api, `/projects/${pid}/experiments`, parseExperiment, signal).then(r => { snapshot.experiments.push(...r.items); })],
      ['运行', listOf(api, `/projects/${pid}/runs`, parseRun, signal).then(r => { snapshot.runs.push(...r.items); if (r.dropped) issues.push({ what: `${project.id} 运行`, reason: `${r.dropped} 条记录格式异常，已跳过` }); })],
    ];
    await Promise.all(jobs.map(([label, job]) => job.catch(error => {
      if (signal.aborted || (error instanceof DOMException && error.name === 'AbortError')) throw error;
      issues.push({ what: `${project.id} ${label}`, reason: describeError(error).detail });
    })));
  }));
  // Every resource of every project failed → treat as a real outage.
  if (projects.length > 0 && issues.length >= projects.length * 2 && snapshot.papers.length === 0 && snapshot.runs.length === 0) {
    throw new GuidedApiError(issues[0]?.reason ?? '读取失败', 0, 'ALL_READS_FAILED');
  }
  return { snapshot, issues };
}

export interface ErrorInfo { kind: 'offline' | 'auth' | 'server' | 'unknown'; title: string; hint: string; steps: string[]; detail: string }

export function describeError(error: unknown): ErrorInfo {
  const code = error instanceof GuidedApiError ? error.code : null;
  const http = status(error);
  const message = error instanceof Error ? error.message : String(error);
  const detail = [http !== null ? `HTTP ${http}` : null, code, message].filter(Boolean).join(' · ');
  // A 5xx without an API error envelope (client fallback code UNKNOWN_ERROR) did
  // not come from the backend: the dev proxy answers 500 with an empty body when
  // its target is down. Real backend failures always carry their own error code.
  const proxyFailure = http !== null && http >= 500 && (code === 'UNKNOWN_ERROR' || code === null);
  if (http === 0 || (http !== null && http >= 502) || proxyFailure || code === 'NETWORK_ERROR' || code === 'ALL_READS_FAILED') {
    return {
      kind: 'offline', title: '无法连接后端服务',
      hint: '浏览器没有收到后端的有效响应。通常是后端未启动，或前端代理指向了错误的端口。',
      steps: ['确认后端已启动：运行「启动Paper2Run.command / .bat」，或在终端启动 uvicorn（默认 8010 端口）。', '如果用 pnpm dev，请设置 P2R_API_TARGET 指向后端地址，例如 http://127.0.0.1:8010。', '看一眼左下角的连接状态；变绿后本页会自动恢复。'],
      detail,
    };
  }
  if (http === 401) return { kind: 'auth', title: '登录已失效', hint: '后端要求登录，当前会话未通过认证。', steps: ['刷新页面重新登录后再试。'], detail };
  if (http === 403) return { kind: 'auth', title: '没有访问权限', hint: '当前用户没有读取这些数据的权限。', steps: ['确认使用的是创建任务的同一账号。'], detail };
  if (http !== null && http >= 500) return { kind: 'server', title: '后端处理请求时出错', hint: '后端已响应，但返回了服务器错误。', steps: ['查看后端日志定位原因，修复后点击重试。'], detail };
  return { kind: 'unknown', title: '读取任务数据失败', hint: '返回的数据无法解析，或请求被中断。', steps: ['点击重试；若持续出现，请把下面的详情发给开发者。'], detail };
}

export interface ReportView {
  recipe: string | null; metrics: Array<[string, string]>; evidenceClass: string | null; verdict: string | null;
  evaluator: string | null; manifestHash: string | null; limitations: string[];
  evidence: Array<{ sha256: string; bytes: number | null; mediaType: string | null }>;
}

export async function loadReport(api: GuidedApi, runId: string, signal: AbortSignal): Promise<ReportView | null> {
  let raw: unknown;
  try {
    raw = await api.read(`/runs/${encodeURIComponent(runId)}/report`, (v: unknown) => v, signal);
  } catch (error) {
    // REPORT_NOT_READY (409) simply means no report has been built yet.
    if (status(error) === 409 || status(error) === 404) return null;
    throw error;
  }
  const r = rec(raw);
  if (!r) return null;
  const metrics: Array<[string, string]> = [];
  for (const [k, v] of Object.entries(rec(r.metrics) ?? {})) {
    if (typeof v === 'number') metrics.push([k, Number.isInteger(v) ? String(v) : v.toFixed(4)]);
    else if (typeof v === 'string') metrics.push([k, v]);
  }
  const recipe = rec(r.recipe);
  const manifest = rec(r.manifest);
  const evidence = (Array.isArray(r.evidence) ? r.evidence : []).flatMap(e => {
    const x = rec(e); const sha = x && str(x.sha256);
    return x && sha ? [{ sha256: sha, bytes: typeof x.bytes === 'number' ? x.bytes : null, mediaType: str(x.media_type) }] : [];
  });
  return {
    recipe: recipe ? [str(recipe.id), str(recipe.version)].filter(Boolean).join('/') : null,
    metrics, evidenceClass: str(r.evidence_class), verdict: str(r.verdict), evaluator: str(r.evaluator_version),
    manifestHash: manifest ? str(manifest.manifest_hash) : null,
    limitations: (Array.isArray(r.limitations) ? r.limitations : []).filter((x): x is string => typeof x === 'string'),
    evidence,
  };
}
