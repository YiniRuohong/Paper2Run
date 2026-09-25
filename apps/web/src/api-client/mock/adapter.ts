// Mock ApiClient：与 live 同一接口、同一字段结构；仅供 mock-ui 模式启用。
// 计时器只存在于本文件（模拟后端状态推进），live 组件不引用。
import { ApiClientError, type ApiClient } from '../client';
import type {
  ApiError, Confirmation, Experiment, ExportAccepted, PageData, PaperAccepted, Project,
  RunAccepted, SpecDraft, SpecRevision,
} from '../types';
import {
  DEMO_USER, artifactText, baseAttempt, baseDraft, baseExtraction, baseRun, fakeHash, iso, type MutableDb,
} from './fixtures';
import { buildScenarioDb, progressingTick, tickDb } from './scenarios';

const SCENARIO_KEY = 'p2r.mock.scenario';

interface MockState {
  db: MutableDb;
}
const state: MockState = { db: buildScenarioDb(localStorage.getItem(SCENARIO_KEY) ?? 'M05') };

export function currentScenarioId(): string {
  return state.db.scenarioId;
}
export function setScenario(id: string): void {
  const prevSession = state.db.session;
  localStorage.setItem(SCENARIO_KEY, id);
  state.db = buildScenarioDb(id);
  state.db.session = prevSession;
}
export function expireNextSession(): void {
  state.db.expireNext = true;
}

function req(): string {
  return `req_m${(state.db.seq++).toString().padStart(5, '0')}`;
}
function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
async function latency(): Promise<void> {
  await sleep(120 + Math.random() * 230);
}
function err(code: string, message: string, status: number, details: ApiError['details'] = {}, retryable = false): ApiClientError {
  return new ApiClientError({ code, message, retryable, details }, req(), status);
}
function guard(): void {
  const db = state.db;
  if (db.expireNext) {
    db.expireNext = false;
    db.session = null;
    throw err('SESSION_EXPIRED', '会话已过期，请重新登录', 401);
  }
  if (!db.session) throw err('SESSION_REQUIRED', '未登录', 401);
}
function cl<T>(x: T): T { return structuredClone(x); }
function page<T>(items: T[]): PageData<T> {
  return { items, has_more: false, next_cursor: null };
}

function cloneExtractionFor(paperId: string): void {
  const db = state.db;
  const clone = structuredClone(baseExtraction());
  clone.extraction_id = 'ext_9001';
  clone.paper_id = paperId;
  clone.source_refs = clone.source_refs.map((s) => ({ ...s, id: `${s.id}_x`, paper_id: paperId }));
  db.extractions.push(clone);
}

// 通用上传→ready 推进（任何场景里用户新上传的论文）
function tickUploads(): void {
  const db = state.db;
  for (const [paperId, readyAt] of db.uploadReadyAt) {
    if (Date.now() < readyAt) continue;
    const paper = db.papers.find((p) => p.paper_id === paperId);
    if (!paper || paper.status !== 'parsing') continue;
    paper.status = 'ready';
    paper.current_extraction_id = 'ext_9001';
    paper.active_operation_id = null;
    for (const op of db.operations.values()) {
      if (op.resource.id === paperId) op.status = 'succeeded';
    }
    if (!db.extractions.some((e) => e.extraction_id === 'ext_9001')) cloneExtractionFor(paperId);
  }
}

function tickAll(): void {
  tickUploads();
  const db = state.db;
  if (db.tickOverride) db.tickOverride(db, Date.now() - db.loadedAt);
  else tickDb(db);
}

export function createMockClient(): ApiClient {
  return {
    async login(inviteToken) {
      await latency();
      if (!inviteToken.trim()) throw err('INVITE_TOKEN_REQUIRED', '请输入邀请码', 422, {
        field_errors: [{ path: '/invite_token', code: 'required', message: '邀请码不能为空' }],
      });
      state.db.session = DEMO_USER;
      return DEMO_USER;
    },
    async getSession() {
      await latency();
      if (!state.db.session) throw err('SESSION_REQUIRED', '未登录', 401);
      return state.db.session;
    },
    async logout() {
      await latency();
      state.db.session = null;
    },
    async getCapabilities() {
      await latency();
      guard();
      return state.db.capabilities;
    },
    async listProjects() {
      await latency(); guard();
      return page(cl(state.db.projects));
    },
    async createProject(input) {
      await latency(); guard();
      const project: Project = {
        id: `prj_${state.db.seq++}`,
        name: input.name,
        description: input.description ?? '',
        role: 'owner',
        version: 1,
        deletion_state: 'active',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
      state.db.projects.push(project);
      return project;
    },
    async listPapers(projectId) {
      await latency(); guard();
      tickUploads();
      return page(cl(state.db.papers.filter((p) => p.project_id === projectId)));
    },
    async getPaper(paperId) {
      await latency(); guard();
      tickUploads();
      const paper = state.db.papers.find((p) => p.paper_id === paperId);
      if (!paper) throw err('RESOURCE_NOT_FOUND', '论文不存在', 404);
      return cl(paper);
    },
    async uploadPaper(projectId, input) {
      await latency(); guard();
      if (!input.consent) throw err('CONSENT_REQUIRED', '未同意论文内容处理，不能上传', 422);
      const caps = state.db.capabilities;
      if (input.file) {
        if (!/\.pdf$/i.test(input.file.name)) throw err('UNSUPPORTED_MEDIA_TYPE', '仅支持 PDF 文件', 415);
        if (input.file.size > caps.upload.max_pdf_bytes) {
          throw err('FILE_TOO_LARGE', `文件超过上限 ${Math.floor(caps.upload.max_pdf_bytes / 1024 / 1024)} MiB`, 413, {
            field_errors: [{ path: '/file', code: 'too_large', message: '文件超过大小上限' }],
          });
        }
      }
      if (!input.file && !input.arxivId) throw err('NO_SOURCE', '缺少 PDF 文件或 arXiv ID', 422);
      const paperId = `pap_9${state.db.seq++}`;
      const opId = `op_7${state.db.seq++}`;
      state.db.papers.push({
        paper_id: paperId,
        project_id: projectId,
        title: input.titleHint ?? (input.arxivId ? `arXiv:${input.arxivId}` : input.file?.name.replace(/\.pdf$/i, '') ?? null),
        status: 'parsing',
        sha256: input.file ? fakeHash(`${input.file.name}:${input.file.size}`) : null,
        arxiv_version: input.arxivId ?? null,
        page_count: null,
        content_available: false,
        current_extraction_id: null,
        active_operation_id: opId,
        processing_consent: { accepted_at: new Date().toISOString(), policy_version: 'consent-2026-09' },
        error: null,
        created_at: new Date().toISOString(),
      });
      state.db.operations.set(opId, {
        id: opId, kind: 'paper_ingest', status: 'running',
        resource: { type: 'paper', id: paperId },
        stage: 'download_or_store', progress: null, error: null,
        updated_at: new Date().toISOString(),
      });
      state.db.uploadReadyAt.set(paperId, Date.now() + 25_000);
      const accepted: PaperAccepted = { paper_id: paperId, operation_id: opId, status: 'parsing' };
      return accepted;
    },
    async getExtraction(paperId) {
      await latency(); guard();
      tickUploads();
      const paper = state.db.papers.find((p) => p.paper_id === paperId);
      if (!paper) throw err('RESOURCE_NOT_FOUND', '论文不存在', 404);
      if (paper.status === 'parsing' || paper.status === 'uploaded') {
        return { kind: 'accepted', accepted: { paper_id: paperId, operation_id: paper.active_operation_id ?? 'op_7001', status: 'parsing' } };
      }
      const extraction = state.db.extractions.find((e) => e.extraction_id === paper.current_extraction_id);
      if (!extraction) {
        throw err(paper.error?.code ?? 'PAPER_PARSE_FAILED', paper.error?.message ?? '解析不可用', 422);
      }
      return { kind: 'extraction', extraction: cl(extraction) };
    },
    async getOperation(operationId) {
      await latency(); guard();
      tickUploads();
      const op = state.db.operations.get(operationId);
      if (!op) throw err('RESOURCE_NOT_FOUND', '操作不存在', 404);
      return cl(op);
    },
    async listExperiments(projectId, paperId) {
      await latency(); guard();
      let list = state.db.experiments.filter((e) => e.project_id === projectId);
      if (paperId) list = list.filter((e) => e.paper_id === paperId);
      return page(cl(list));
    },
    async createExperiment(projectId, input) {
      await latency(); guard();
      const existing = state.db.experiments.find((e) => e.paper_id === input.paper_id);
      if (existing) return existing;
      const exp: Experiment = {
        experiment_id: `exp_9${state.db.seq++}`,
        project_id: projectId,
        paper_id: input.paper_id,
        status: 'draft',
        head_revision_id: null,
        active_run_id: null,
        latest_run_id: null,
        repairs_used: 0,
        repairs_limit: state.db.capabilities.limits.max_repairs,
        created_at: new Date().toISOString(),
      };
      const draft = baseDraft();
      draft.experiment_id = exp.experiment_id;
      draft.spec.candidate_experiment_id = input.candidate_experiment_id;
      draft.spec.recipe = { id: input.recipe_id, version: input.recipe_version };
      state.db.experiments.push(exp);
      state.db.drafts.set(exp.experiment_id, draft);
      return exp;
    },
    async getDraft(experimentId) {
      await latency(); guard();
      const draft = state.db.drafts.get(experimentId);
      if (!draft) throw err('RESOURCE_NOT_FOUND', '草稿不存在', 404);
      return cl(draft);
    },
    async saveRevision(projectId, input) {
      await latency(); guard();
      const db = state.db;
      const draft = db.drafts.get(input.experiment_id);
      if (!draft) throw err('RESOURCE_NOT_FOUND', '实验不存在', 404);
      const latest = db.revisions.find((r) => r.experiment_id === input.experiment_id && r.is_current);
      if (db.conflictOnce) {
        // M04：模拟队友刚保存了新版本，返回 409，前端保留内存编辑
        db.conflictOnce = false;
        if (!latest) {
          const base = baseRevisionFor(draft);
          db.revisions.push(base);
          draft.base_revision_id = base.id;
        }
        throw err('REVISION_CONFLICT', '草稿基线已过期：队友保存了新版本', 409, {
          latest_revision_id: draft.base_revision_id ?? 'rev_2001',
        });
      }
      const mode = input.spec.mode;
      const fraction = input.spec.dataset.train_subset_fraction ?? 1;
      const issues = draft.issues.filter((i) =>
        !(i.code === 'TARGET_SAMPLE_INSUFFICIENT' && (mode === 'scaled' || fraction >= 0.3)));
      const smoke = mode === 'smoke';
      const rev: SpecRevision = {
        id: `rev_2${db.seq++}`,
        experiment_id: input.experiment_id,
        revision_no: (latest?.revision_no ?? 0) + 1,
        spec_hash: fakeHash(JSON.stringify(input.spec.training) + input.spec.mode),
        policy_hash: draft.policy_hash,
        spec: input.spec,
        validation: { issues },
        comparison_precheck: smoke
          ? { status: 'not_comparable', reasons: ['SMOKE_MODE', 'TRAINING_BUDGET_REDUCED'] }
          : { status: 'eligible', reasons: [] },
        budget_estimate: {
          currency: 'CNY',
          amount_micros: smoke ? 1_840_000 : 6_512_000,
          duration_seconds: smoke ? 2400 : 5400,
          basis: smoke ? '按 smoke 子集 10%、3 epochs 与上限修复 5 次估算' : '按 scaled 缩减轮数 60/182 与上限修复 5 次估算',
        },
        confirmation: null,
        is_current: true,
        created_at: new Date().toISOString(),
      };
      db.revisions.forEach((r) => { r.is_current = false; });
      db.revisions.push(rev);
      draft.spec = input.spec;
      draft.base_revision_id = rev.id;
      draft.issues = issues;
      draft.comparison_precheck = rev.comparison_precheck;
      const exp = db.experiments.find((e) => e.experiment_id === input.experiment_id);
      if (exp) exp.head_revision_id = rev.id;
      void projectId;
      return cl(rev);
    },
    async confirmRevision(revisionId, input) {
      await latency(); guard();
      const db = state.db;
      const rev = db.revisions.find((r) => r.id === revisionId);
      if (!rev) throw err('RESOURCE_NOT_FOUND', '版本不存在', 404);
      const blocking = rev.validation.issues.filter((i) => i.severity === 'blocking');
      if (blocking.length > 0) {
        throw err('SPEC_INVALID', '存在 blocking 问题，不能确认', 422, {
          field_errors: blocking.map((b) => ({ path: b.path ?? '/', code: b.code, message: b.message })),
        });
      }
      const warnings = rev.validation.issues.filter((i) => i.severity === 'warning').map((w) => w.code);
      const unaccepted = warnings.filter((w) => !input.accepted_issue_codes.includes(w));
      if (unaccepted.length > 0) throw err('WARNINGS_NOT_ACCEPTED', '存在未确认的 warning', 422, {
        field_errors: [{ path: '/', code: unaccepted.join(','), message: '请先勾选接受全部 warning' }],
      });
      if (!input.accept_budget) throw err('BUDGET_NOT_ACCEPTED', '未接受预算估计', 422);
      const cfm: Confirmation = {
        confirmation_id: `cfm_9${db.seq++}`,
        revision_id: revisionId,
        spec_hash: rev.spec_hash,
        confirmed_by: db.session?.user.id ?? 'usr_801',
        confirmed_at: new Date().toISOString(),
      };
      rev.confirmation = cfm;
      const exp = db.experiments.find((e) => e.experiment_id === rev.experiment_id);
      if (exp) exp.status = 'ready';
      return cfm;
    },
    async createRun(projectId, input) {
      await latency(); guard();
      const db = state.db;
      const rev = db.revisions.find((r) => r.id === input.spec_revision_id);
      const cfm = rev?.confirmation;
      if (!rev || !cfm) throw err('REVISION_NOT_CONFIRMED', '版本尚未确认', 409);
      const active = db.runs.find((r) => !['completed', 'failed', 'cancelled', 'awaiting_revision'].includes(r.status));
      if (active) throw err('RUN_ACTIVE', '已有运行中的任务', 409, { operation_id: active.id });
      const run = baseRun({
        id: `run_3${db.seq++}`,
        experiment_id: input.experiment_id,
        spec_revision_id: input.spec_revision_id,
        mode: rev.spec.mode,
        status: 'queued',
        stage: null,
        version: 1,
        last_event_seq: 0,
        attempt_index: null,
        repairs_used: 0,
        report_status: 'pending',
        terminal_reason: null,
        created_at: new Date().toISOString(),
        started_at: null,
        ended_at: null,
        budget: { currency: 'CNY', limit_micros: 20_000_000, actual_micros: null, reserved_micros: 8_000_000, unknown_charges: false },
      });
      db.runs = [run];
      db.attempts = [baseAttempt(1, { run_id: run.id, status: 'generated', created_at: new Date().toISOString() })];
      db.filesByAttempt.clear();
      db.cancelRequestedAt.clear();
      db.tickOverride = progressingTick({});
      const exp = db.experiments.find((e) => e.experiment_id === input.experiment_id);
      if (exp) {
        exp.active_run_id = run.id;
        exp.latest_run_id = run.id;
        exp.status = 'active';
      }
      void projectId;
      const accepted: RunAccepted = { run_id: run.id, status: 'queued', events_url: `/api/v1/runs/${run.id}/events`, last_event_seq: 0 };
      return accepted;
    },
    async listRuns(projectId) {
      await latency(); guard();
      tickAll();
      void projectId;
      return page(cl([...state.db.runs].sort((a, b) => b.created_at.localeCompare(a.created_at))));
    },
    async getRun(runId) {
      await latency(); guard();
      tickAll();
      const run = state.db.runs.find((r) => r.id === runId);
      if (!run) throw err('RESOURCE_NOT_FOUND', '运行不存在', 404);
      return cl(run);
    },
    async cancelRun(runId) {
      await latency(); guard();
      tickAll();
      const run = state.db.runs.find((r) => r.id === runId);
      if (!run) throw err('RESOURCE_NOT_FOUND', '运行不存在', 404);
      if (['completed', 'failed', 'cancelled', 'awaiting_revision'].includes(run.status)) return run;
      run.status = 'cancel_requested';
      state.db.cancelRequestedAt.set(runId, Date.now());
      return cl(run);
    },
    async listAttempts(runId) {
      await latency(); guard();
      tickAll();
      return cl(state.db.attempts.filter((a) => a.run_id === runId));
    },
    async listFiles(attemptId) {
      await latency(); guard();
      tickAll();
      return cl(state.db.filesByAttempt.get(attemptId) ?? []);
    },
    async getArtifactText(artifactId, offset) {
      await latency(); guard();
      tickAll();
      return artifactText(artifactId, offset);
    },
    async getCurves(runId) {
      await latency(); guard();
      tickAll();
      return cl(state.db.curves.get(runId) ?? { run_id: runId, attempt_id: null, series: [], downsampled: false, warnings: [] });
    },
    async getReport(runId) {
      await latency(); guard();
      tickAll();
      const run = state.db.runs.find((r) => r.id === runId);
      if (!run) throw err('RESOURCE_NOT_FOUND', '运行不存在', 404);
      const entry = state.db.reports.get(runId);
      if (!['completed', 'failed', 'cancelled', 'awaiting_revision'].includes(run.status)) {
        return { kind: 'pending' as const, pending: { run_id: runId, status: 'waiting_for_run' as const, operation_id: null, retry_after_ms: 3000 } };
      }
      if (!entry || 'kind' in entry) {
        if (run.report_status === 'failed') {
          throw err('REPORT_BUILD_FAILED', '报告生成失败：执行未完成', 422);
        }
        return { kind: 'pending' as const, pending: { run_id: runId, status: 'generating' as const, operation_id: entry && 'kind' in entry ? entry.operationId : null, retry_after_ms: 3000 } };
      }
      return { kind: 'report' as const, report: cl(entry) };
    },
    async getManifest(runId) {
      await latency(); guard();
      tickAll();
      const m = state.db.manifests.get(runId);
      if (!m) throw err('RESOURCE_NOT_FOUND', '复现声明尚未生成', 404);
      return cl(m);
    },
    async listExports(runId) {
      await latency(); guard();
      tickAll();
      return cl(state.db.exports.get(runId) ?? []);
    },
    async createExport(runId, input) {
      await latency(); guard();
      tickAll();
      const db = state.db;
      const report = db.reports.get(runId);
      const manifest = db.manifests.get(runId);
      const run = db.runs.find((r) => r.id === runId);
      if (!run || run.report_status !== 'ready' || !report || 'kind' in report || !manifest) {
        throw err('EXPORT_NOT_READY', '报告尚未就绪，暂不能导出', 409);
      }
      const formats = Array.from(new Set(input.formats)).slice(0, 4);
      if (formats.length === 0) throw err('SPEC_INVALID', '至少选择一种导出格式', 422);
      const id = `exp_9002`;
      const opId = `op_72${db.seq++}`;
      const list = db.exports.get(runId) ?? [];
      list.unshift({
        id, run_id: runId, status: 'queued', operation_id: opId,
        created_at: new Date().toISOString(),
        items: formats.map((f) => ({ format: f, status: 'pending' as const, artifact_id: null, filename: null, bytes: null, error: null })),
      });
      db.exports.set(runId, list);
      db.exportCreatedAt.set(id, Date.now());
      const accepted: ExportAccepted = { export_id: id, operation_id: opId, status: 'queued' };
      return accepted;
    },
    async downloadArtifact(artifactId) {
      guard();
      const text = `Paper2Run mock artifact\nartifact_id=${artifactId}\n场景=${currentScenarioId()}\n本文件由 mock-ui 模式生成，不是真实实验产物。\n`;
      const blob = new Blob([text], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `mock-${artifactId}.txt`;
      a.click();
      URL.revokeObjectURL(url);
    },
  };
}

function baseRevisionFor(draft: SpecDraft): SpecRevision {
  return {
    id: 'rev_2001',
    experiment_id: draft.experiment_id,
    revision_no: 1,
    spec_hash: fakeHash('rev-2001-spec'),
    policy_hash: draft.policy_hash,
    spec: draft.spec,
    validation: { issues: draft.issues },
    comparison_precheck: draft.comparison_precheck,
    budget_estimate: { currency: 'CNY', amount_micros: 1_840_000, duration_seconds: 2400, basis: '按 smoke 子集估算' },
    confirmation: null,
    is_current: true,
    created_at: iso(20 * 60_000),
  };
}
