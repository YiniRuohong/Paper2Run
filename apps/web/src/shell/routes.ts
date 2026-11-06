// Hash routes (pure; no React):
//   #/                 overview
//   #/settings         settings
//   #/new              new experiment (full-bleed workbench)
//   #/draft/<id>       draft project workbench (full-bleed)
//   #/task/<id>        task workbench (full-bleed)
//   #/reader[/<id>]    paper reader (PDF preview + annotations; sidebar shell, full-height body)
// Retired links redirect: #/autonomous[?task=<id>] and #/new?project=<id>.
export type Route =
  | { view: 'overview' } | { view: 'settings' } | { view: 'reader'; paperId: string }
  | { view: 'new' } | { view: 'draft'; projectId: string } | { view: 'task'; taskId: string };

export const draftHref = (projectId: string) => `#/draft/${encodeURIComponent(projectId)}`;

const queryOf = (hash: string) => new URLSearchParams(hash.includes('?') ? hash.slice(hash.indexOf('?') + 1) : '');
const idOf = (hash: string, prefix: string) => {
  const raw = hash.slice(prefix.length).split('?')[0];
  try { return decodeURIComponent(raw); } catch { return raw; }
};

export function parseRoute(hash: string): { route: Route; redirect: string | null } {
  const to = (route: Route, redirect: string | null = null) => ({ route, redirect });
  if (hash.startsWith('#/autonomous')) {
    const task = queryOf(hash).get('task');
    return to({ view: 'new' }, task ? `#/task/${encodeURIComponent(task)}` : '#/new');
  }
  if (hash.startsWith('#/new')) {
    const project = queryOf(hash).get('project');
    return project ? to({ view: 'new' }, draftHref(project)) : to({ view: 'new' });
  }
  if (hash.startsWith('#/draft/')) {
    const projectId = idOf(hash, '#/draft/');
    return projectId ? to({ view: 'draft', projectId }) : to({ view: 'new' }, '#/new');
  }
  if (hash.startsWith('#/task/')) {
    const taskId = idOf(hash, '#/task/');
    return taskId ? to({ view: 'task', taskId }) : to({ view: 'overview' });
  }
  if (/^#\/reader(?:[/?]|$)/.test(hash)) {
    return to({ view: 'reader', paperId: hash.startsWith('#/reader/') ? idOf(hash, '#/reader/') : '' });
  }
  if (hash.startsWith('#/settings')) return to({ view: 'settings' });
  return to({ view: 'overview' });
}
