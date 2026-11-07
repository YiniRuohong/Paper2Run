import type { AutonomousApi, AutonomousTask, ModelProfile, ProjectConfig } from '../../api-client/autonomous.ts';
import { ApiError } from '../../api-client/client.ts';
import type { PapersApi, PaperInfo, PaperText, ProjectInfo } from '../../api-client/papers.ts';
import { withoutBudgetCaps } from '../autonomous/config.ts';
import { paperIsPdf } from '../autonomous/launch.ts';
import { pickPaper, type DraftFacts, type DraftPaper } from './draft.ts';

// Reads one draft project from the server. No React; the draft page and the
// overview both build their view from this.

export type SavedConfig = { version: number; config: ProjectConfig };
type Api = Pick<AutonomousApi, 'config' | 'tasks'>;
type Papers = Pick<PapersApi, 'getProject' | 'listPapers' | 'getPaper' | 'getPaperText'>;

export interface DraftData {
  facts: DraftFacts;
  project: ProjectInfo;
  saved: SavedConfig | null;
  /** Parse record of the picked paper when it is a PDF; null for pasted text. */
  paperText: PaperText | null;
  tasks: AutonomousTask[];
}

/** The saved project config, or null when none has been written yet (404).
 *  Time, call and token caps left in an older config are dropped here, so the next save persists without them. */
export function readConfig(api: Pick<AutonomousApi, 'config'>, projectId: string): Promise<SavedConfig | null> {
  return api.config(projectId).then(saved => saved && { ...saved, config: withoutBudgetCaps(saved.config) }, error => {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  });
}

/** fallbackTitle: the project name (taken from the file name or title hint) when the paper has no title of its own. */
async function readPaper(papers: Papers, row: Pick<PaperInfo, 'paper_id' | 'title'>, fallbackTitle: string, signal?: AbortSignal): Promise<{ paper: DraftPaper; text: PaperText | null }> {
  let info: PaperInfo;
  try {
    info = await papers.getPaper(row.paper_id, signal);
  } catch (error) {
    if (signal?.aborted) throw error;
    // Without its details the paper has no usable version; pickPaper skips it.
    return { paper: { paperId: row.paper_id, versionId: '', title: row.title || fallbackTitle, pdf: false, textStatus: null, pageCount: null }, text: null };
  }
  const pdf = paperIsPdf(info);
  const text = pdf ? await papers.getPaperText(row.paper_id, signal) : null;
  return {
    paper: {
      paperId: row.paper_id, versionId: info.paper_version_id ?? '', title: info.title || row.title || fallbackTitle, pdf,
      textStatus: text?.status ?? null, pageCount: text && text.page_count > 0 ? text.page_count : info.page_count ?? null,
    },
    text,
  };
}

export async function loadDraft(api: Api, papers: Papers, projectId: string, profiles: ModelProfile[], signal?: AbortSignal): Promise<DraftData> {
  const [project, rows, saved, tasks] = await Promise.all([
    papers.getProject(projectId, signal), papers.listPapers(projectId, signal), readConfig(api, projectId), api.tasks(projectId),
  ]);
  // The server lists papers oldest first; pickPaper takes the newest with a version.
  const read = await Promise.all(rows.map(row => readPaper(papers, row, project.name || row.paper_id, signal)));
  const paper = pickPaper(read.map(r => r.paper));
  const paperText = paper ? read.find(r => r.paper === paper)?.text ?? null : null;
  return {
    facts: { paper, description: project.description ?? '', config: saved?.config ?? null, profiles, taskCount: tasks.length },
    project, saved, paperText, tasks,
  };
}

export async function loadDraftFacts(api: Api, papers: Papers, projectId: string, profiles: ModelProfile[], signal?: AbortSignal): Promise<DraftFacts> {
  return (await loadDraft(api, papers, projectId, profiles, signal)).facts;
}
