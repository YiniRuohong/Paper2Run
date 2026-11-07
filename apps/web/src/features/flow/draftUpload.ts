// After a PDF upload the paper exists, so the page must always show it, even when
// the parse could not be started. The start error is returned, not thrown, so the
// caller can show it on the draft where the "start parsing" retry is reachable.
export async function finishUpload(steps: { startParse: () => Promise<void>; accepted: (failure: unknown) => Promise<void> }): Promise<unknown> {
  let failure: unknown;
  try { await steps.startParse(); } catch (e) { failure = e; }
  await steps.accepted(failure);
  return failure;
}

// A notice for the draft that is about to open. #/new -> #/draft/<id> remounts the
// page, so page state cannot carry it; this lives in module memory only.
const notices = new Map<string, string>();
export const carryNotice = (projectId: string, text: string) => { notices.set(projectId, text); };
export const peekNotice = (projectId: string | null) => (projectId && notices.get(projectId)) || '';
export const clearNotice = (projectId: string | null) => { if (projectId) notices.delete(projectId); };
