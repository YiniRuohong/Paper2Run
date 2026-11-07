import type { AutonomousTask, ModelProfile, ProjectConfig } from '../../api-client/autonomous.ts';
import { ApiError } from '../../api-client/client.ts';
import type { PaperTextStatus } from '../../api-client/papers.ts';
import { defaultConfig } from '../autonomous/config.ts';

// Pure derivation of a draft's setup progress from server state. No React.
// A "draft" is a project with no tasks; every fact here comes from the server.
export type SetupStepId = 'paper' | 'objective' | 'model' | 'launch';
export const SETUP_ORDER: SetupStepId[] = ['paper', 'objective', 'model', 'launch'];
export const OBJECTIVE_LIMIT = 2000;

export interface DraftPaper { paperId: string; versionId: string; title: string; pdf: boolean; textStatus: PaperTextStatus | null; pageCount: number | null }
export interface DraftFacts { paper: DraftPaper | null; description: string; config: ProjectConfig | null; profiles: ModelProfile[]; taskCount: number }
export interface DraftProgress { done: Record<SetupStepId, boolean>; current: SetupStepId; connectionLost: boolean; objective: string }

/** Descriptions older clients wrote when creating a project (v3 and the first guided flow); they are not an objective. */
export const LEGACY_PLACEHOLDER_DESCRIPTIONS: readonly string[] = ['Autonomous experiment flow', 'Guided real article flow'];
/** The project description as the user's own text: trimmed, and empty for a legacy placeholder. */
export function userDescription(description: string): string {
  const text = description.trim();
  return LEGACY_PLACEHOLDER_DESCRIPTIONS.includes(text) ? '' : text;
}

/** A connection this server process can use right now (connections live only in its memory). */
export const usableConnection = (p: ModelProfile) => p.secret_configured === true || p.auth_mode === 'none';
export function hostOf(url: string): string {
  try { return new URL(url).host || url; } catch { return url; }
}
export const connectionLabel = (p: ModelProfile) => `${hostOf(p.base_url)} · ${p.model}`;

export function draftProgress(f: DraftFacts): DraftProgress {
  const objective = f.config?.experiment.objective.trim() || userDescription(f.description);
  const profileId = f.config?.model.provider_profile || '';
  const profile = profileId ? f.profiles.find(p => p.id === profileId) : undefined;
  const done: Record<SetupStepId, boolean> = {
    paper: !!f.paper && !!f.paper.versionId && (!f.paper.pdf || f.paper.textStatus === 'ready'),
    objective: objective !== '',
    model: !!profile && usableConnection(profile),
    launch: f.taskCount > 0,
  };
  const current = SETUP_ORDER.find(step => !done[step]) ?? 'launch';
  return { done, current, connectionLost: !!profileId && !profile, objective };
}

// Specialists and Skills from a v3 config are kept (mergeConfig preserves them)
// but have no editing UI. Step 04 names them and refuses to start when a
// specialist's connection is gone, since the server would fail with CREDENTIAL_REQUIRED.

/** Profile ids of hidden v3 specialists whose connection this process cannot use, without repeats. */
export function missingSpecialistConnections(config: ProjectConfig | null, profiles: ModelProfile[]): string[] {
  const missing: string[] = [];
  for (const specialist of config?.specialists ?? []) {
    const id = specialist.model.provider_profile;
    const profile = profiles.find(p => p.id === id);
    if ((!profile || !usableConnection(profile)) && !missing.includes(id)) missing.push(id);
  }
  return missing;
}
export function legacyLaunchBlock(config: ProjectConfig | null, profiles: ModelProfile[]): string {
  return missingSpecialistConnections(config, profiles).length ? '旧配置中的科研专家模型连接已失效，无法启动（应用重启后需重新配置）' : '';
}
/** Read-only line naming the hidden v3 parts; empty when there are none. */
export function legacyConfigLine(config: ProjectConfig | null): string {
  const specialists = config?.specialists?.length ?? 0;
  const skills = config?.harness.skills.length ?? 0;
  const parts = [specialists ? `${specialists} 位科研专家` : '', skills ? `${skills} 个 Skills` : ''].filter(Boolean);
  return parts.length ? `沿用旧配置：${parts.join(' · ')}` : '';
}

export function mergeConfig(
  saved: ProjectConfig | null, projectId: string,
  patch: { objective?: string; model?: ProjectConfig['model']; target?: ProjectConfig['target'] },
): ProjectConfig {
  const base = saved ?? defaultConfig(projectId);
  const next: ProjectConfig = { ...base };
  if (patch.objective !== undefined) next.experiment = { ...base.experiment, objective: patch.objective };
  if (patch.model !== undefined) next.model = { ...patch.model };
  if (patch.target !== undefined) next.target = { ...patch.target };
  return next;
}

export function profileIdFor(projectId: string, config: ProjectConfig | null): string {
  const existing = config?.model.provider_profile;
  if (existing) return existing;
  return `model-${projectId.replace(/[^A-Za-z0-9]/g, '').slice(0, 12)}`;
}

export function objectiveError(text: string): string {
  if (!text.trim()) return '请填写实验目标';
  if (text.length > OBJECTIVE_LIMIT) return `实验目标最多 ${OBJECTIVE_LIMIT} 字`;
  return '';
}

export function budgetLine(limits: ProjectConfig['limits']): string {
  // No time, call or token cap by default; a cap is shown only when a config still carries one.
  const { wall_time_seconds: wall, max_model_calls: calls, max_model_tokens: tokens } = limits;
  const caps = [
    typeof wall === 'number' ? `${Math.round(wall / 60)} 分钟` : '',
    typeof calls === 'number' ? `${calls} 次模型调用` : '',
    typeof tokens === 'number' ? `${tokens % 10000 === 0 ? `${tokens / 10000} 万` : tokens} tokens` : '',
  ].filter(Boolean);
  return `${caps.length ? caps.join(' · ') : '不限时长、调用次数和 tokens'} · ${limits.max_repair_rounds ?? 3} 轮修复`;
}

export function newestTaskId(tasks: Pick<AutonomousTask, 'id' | 'created_at' | 'updated_at'>[]): string | null {
  let best: { id: string; at: number } | null = null;
  for (const task of tasks) {
    const parsed = Date.parse(task.updated_at || task.created_at);
    const at = Number.isNaN(parsed) ? 0 : parsed;
    if (!best || at > best.at) best = { id: task.id, at };
  }
  return best ? best.id : null;
}

/** The latest paper that has a version; `papers` is ordered oldest to newest. */
export function pickPaper(papers: DraftPaper[]): DraftPaper | null {
  for (let i = papers.length - 1; i >= 0; i -= 1) if (papers[i].versionId) return papers[i];
  return null;
}

type Saved = { version: number; config: ProjectConfig };
const isConflict = (error: unknown) => error instanceof ApiError && error.code === 'REVISION_CONFLICT';

/** Read-modify-write with one retry on a revision conflict; the second failure is thrown. */
export async function saveConfigWithRetry(
  read: () => Promise<Saved | null>,
  save: (config: ProjectConfig, version?: number) => Promise<Saved>,
  apply: (saved: ProjectConfig | null) => ProjectConfig,
): Promise<Saved> {
  const attempt = async () => {
    const saved = await read();
    return save(apply(saved?.config ?? null), saved?.version);
  };
  try {
    return await attempt();
  } catch (error) {
    if (!isConflict(error)) throw error;
    return attempt();
  }
}

/**
 * Runs a save made of several requests. When any of them fails, the page reads
 * the server again (project version, config, saved connections) so a retry
 * starts from fresh state; the original error is rethrown for the page to show.
 */
export async function refreshOnFailure<T>(work: () => Promise<T>, refresh: () => Promise<unknown>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    await refresh().catch(() => undefined);
    throw error;
  }
}
