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

export function draftProgress(f: DraftFacts): DraftProgress {
  const objective = f.config?.experiment.objective.trim() || f.description.trim();
  const profileId = f.config?.model.provider_profile || '';
  const profile = profileId ? f.profiles.find(p => p.id === profileId) : undefined;
  const done: Record<SetupStepId, boolean> = {
    paper: !!f.paper && !!f.paper.versionId && (!f.paper.pdf || f.paper.textStatus === 'ready'),
    objective: objective !== '',
    model: !!profile && (profile.secret_configured === true || profile.auth_mode === 'none'),
    launch: f.taskCount > 0,
  };
  const current = SETUP_ORDER.find(step => !done[step]) ?? 'launch';
  return { done, current, connectionLost: !!profileId && !profile, objective };
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
  const tokens = limits.max_model_tokens % 10000 === 0 ? `${limits.max_model_tokens / 10000} 万` : String(limits.max_model_tokens);
  return `${Math.round(limits.wall_time_seconds / 60)} 分钟 · ${limits.max_model_calls} 次模型调用 · ${tokens} tokens · ${limits.max_repair_rounds ?? 3} 轮修复`;
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
