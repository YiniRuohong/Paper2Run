import type { AutonomousApi } from '../../api-client/autonomous.ts';
import type { PapersApi } from '../../api-client/papers.ts';
import { loadDraftFacts } from '../flow/draftData.ts';
import { draftProgress, type SetupStepId } from '../flow/draft.ts';

type Api = Pick<AutonomousApi, 'profiles' | 'config' | 'tasks'>;
type Papers = Pick<PapersApi, 'getProject' | 'listPapers' | 'getPaper' | 'getPaperText'>;

const CONCURRENCY = 3;

/**
 * Current setup step of each draft. Model profiles are read once; when that read
 * fails no step is guessed (the result is empty). A draft whose own read fails is
 * left out. onStep reports each step as soon as it is known.
 */
export async function loadDraftSteps(
  api: Api, papers: Papers, projectIds: string[], signal?: AbortSignal,
  onStep?: (projectId: string, step: SetupStepId) => void,
): Promise<Record<string, SetupStepId>> {
  const steps: Record<string, SetupStepId> = {};
  let profiles;
  try { profiles = await api.profiles(); } catch { return steps; }
  if (signal?.aborted) return steps;
  const queue = [...projectIds];
  const worker = async () => {
    for (let id = queue.shift(); id !== undefined && !signal?.aborted; id = queue.shift()) {
      try {
        const step = draftProgress(await loadDraftFacts(api, papers, id, profiles, signal)).current;
        if (signal?.aborted) return;
        steps[id] = step;
        onStep?.(id, step);
      } catch { /* 读不到步骤时保持「草稿」 */ }
    }
  };
  await Promise.all(Array.from({ length: Math.min(CONCURRENCY, queue.length) }, worker));
  return steps;
}
