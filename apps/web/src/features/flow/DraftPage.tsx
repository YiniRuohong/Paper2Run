import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createAutonomousApi, type AutonomousApi } from '../../api-client/autonomous';
import { createPapersApi, type DocParseMode, type DocParseStatus, type PapersApi } from '../../api-client/papers';
import { draftHref } from '../../shell/routes';
import { Icon } from '../../ui/Icon';
import { Workbench, type WbContent, type WbFile } from '../../ui/workbench/Workbench';
import { errorDetail } from '../autonomous/errors';
import ModeDialog from '../docparse/ModeDialog';
import { canParseNow } from '../docparse/docparse';
import { SETUP_ORDER, draftProgress, newestTaskId, type DraftFacts, type DraftPaper, type DraftProgress, type SetupStepId } from './draft';
import { loadDraft, type DraftData } from './draftData';
import { carryNotice, clearNotice, finishUpload, peekNotice } from './draftUpload';
import RailHead from './RailHead';
import type { StepFeedback } from './StepFrame';
import { flowStages, type SetupView } from './timeline';
import LaunchStep from './steps/LaunchStep';
import ModelStep, { connectionLabel } from './steps/ModelStep';
import ObjectiveStep from './steps/ObjectiveStep';
import PaperStep from './steps/PaperStep';
import './flow.css';

const autonomous = createAutonomousApi();
const papersApi = createPapersApi('demo-user');
const EMPTY: DraftFacts = { paper: null, description: '', config: null, profiles: [], taskCount: 0 };
const UNTITLED = '未命名论文';

const projectName = (hint: string) => Array.from(hint.trim() || UNTITLED).slice(0, 120).join('');
const clip = (text: string, n: number) => Array.from(text).length > n ? `${Array.from(text).slice(0, n).join('')}…` : text;
const utf8Bytes = (text: string) => new TextEncoder().encode(text).length;
const paperResult = (paper: DraftPaper) => !paper.pdf ? '论文正文已保存' : paper.pageCount ? `论文解析完成，共 ${paper.pageCount} 页` : '论文解析完成';

/** A step can be opened when it is the current one, or done and before the current one. */
function reachable(progress: DraftProgress, id: SetupStepId): boolean {
  return id === progress.current || (progress.done[id] && SETUP_ORDER.indexOf(id) < SETUP_ORDER.indexOf(progress.current));
}

/** Each step shows the previous step's result. */
function feedbackFor(step: SetupStepId, facts: DraftFacts, progress: DraftProgress): StepFeedback | null {
  if (step === 'paper') return facts.paper && progress.done.paper ? { tone: 'ok', text: paperResult(facts.paper) } : null;
  if (step === 'objective') return facts.paper && progress.done.paper ? { tone: 'ok', text: paperResult(facts.paper) } : null;
  if (step === 'model') return progress.done.objective ? { tone: 'ok', text: '实验目标已保存' } : null;
  return progress.done.model ? { tone: 'ok', text: '已保存。实际连接在启动时由服务端检查。' } : null;
}

// Draft workbench (#/new, #/draft/<projectId>): the four setup steps of the
// nine-step timeline. Every fact comes from the server (draft.ts derives the
// current step); this page loads, uploads and parses, and switches steps. Each
// step writes its own part of the configuration.
export default function DraftPage({ projectId, api = autonomous, papers = papersApi }: { projectId: string | null; api?: AutonomousApi; papers?: PapersApi }) {
  const [data, setData] = useState<DraftData | null>(null);
  const [loading, setLoading] = useState(projectId !== null);
  const [viewing, setViewing] = useState<SetupStepId | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(() => peekNotice(projectId));
  const [parseStatus, setParseStatus] = useState<DocParseStatus | null>(null);
  const [modeDialog, setModeDialog] = useState(false);
  const [polling, setPolling] = useState<string | null>(null);
  const dialogOffered = useRef(false);
  const createdProject = useRef<string | null>(null);
  const loadAbort = useRef<AbortController | null>(null);

  // Reads everything again; a project that already has a task opens that task instead.
  const reload = useCallback(async (): Promise<DraftData | null> => {
    if (!projectId) return null;
    loadAbort.current?.abort();
    const controller = new AbortController();
    loadAbort.current = controller;
    try {
      const next = await loadDraft(api, papers, projectId, await api.profiles(), controller.signal);
      if (controller.signal.aborted) return null;
      const newest = newestTaskId(next.tasks);
      if (newest) { window.location.replace(`#/task/${encodeURIComponent(newest)}`); return null; }
      setData(next);
      const paper = next.facts.paper;
      if (paper?.pdf && paper.textStatus !== 'ready') {
        const status = await papers.getDocParseStatus(controller.signal);
        if (controller.signal.aborted) return null;
        setParseStatus(status);
        if (status.mode === null && paper.textStatus !== 'parsing' && !dialogOffered.current) { dialogOffered.current = true; setModeDialog(true); }
      }
      if (paper?.pdf && paper.textStatus === 'parsing') setPolling(paper.paperId);
      return next;
    } catch (e) {
      if (controller.signal.aborted) return null;
      throw e;
    }
  }, [api, papers, projectId]);

  useEffect(() => { clearNotice(projectId); }, [projectId]);

  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    api.initializeSession()
      .then(() => reload())
      .then(next => { if (alive && next) setViewing(v => v ?? draftProgress(next.facts).current); })
      .catch(e => { if (alive) setError(errorDetail(e)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; loadAbort.current?.abort(); };
  }, [api, projectId, reload]);

  // Follow a running parse until it is ready or failed, then read the draft again.
  useEffect(() => {
    if (!polling) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const text = await papers.getPaperText(polling);
        if (!alive) return;
        if (text.status === 'parsing' || text.status === 'none') {
          setData(d => d && d.facts.paper?.paperId === polling ? { ...d, paperText: text } : d);
          timer = setTimeout(() => void tick(), 2000);
        } else {
          setPolling(null);
          reload().catch(e => setError(errorDetail(e)));
        }
      } catch {
        if (alive) timer = setTimeout(() => void tick(), 4000);
      }
    };
    timer = setTimeout(() => void tick(), 1500);
    return () => { alive = false; clearTimeout(timer); };
  }, [polling, papers, reload]);

  const act = useCallback(async (work: () => Promise<void>) => {
    setBusy(true); setError('');
    try { await work(); return true; } catch (e) { setError(errorDetail(e)); return false; } finally { setBusy(false); }
  }, []);

  const facts = data?.facts ?? EMPTY;
  const progress = draftProgress(facts);
  const shown = viewing && reachable(progress, viewing) ? viewing : progress.current;
  const paper = facts.paper;
  const profile = facts.config ? facts.profiles.find(p => p.id === facts.config!.model.provider_profile) : undefined;

  /** Move on after a step saved: read again, then open the requested step if it is reachable. */
  const advance = (step: SetupStepId) => async () => { await reload(); setViewing(step); };

  // ---- 01: project creation, upload and parsing ----
  async function ensureProject(hint: string): Promise<string> {
    if (projectId) return projectId;
    // Reused on retry, so a failed upload does not leave a second empty draft behind.
    if (!createdProject.current) createdProject.current = (await papers.createProject(projectName(hint))).id;
    return createdProject.current;
  }
  async function startParse(paperId: string) {
    const status = await papers.getDocParseStatus();
    setParseStatus(status);
    if (canParseNow(status)) {
      await papers.parsePaper(paperId, crypto.randomUUID());
      setPolling(paperId);
    }
  }
  async function accepted(id: string) {
    if (!projectId) { window.location.replace(draftHref(id)); return; }
    dialogOffered.current = false;
    await reload();
    setViewing('paper');
  }
  const uploadPdf = (file: File) => act(async () => {
    const name = file.name.replace(/\.pdf$/i, '');
    const id = await ensureProject(name);
    const upload = await papers.uploadPaperPdf(id, file, name.trim() ? { titleHint: projectName(name) } : undefined);
    await finishUpload({
      startParse: () => startParse(upload.paper_id),
      accepted: async failure => {
        // On #/new the route change remounts the page, so hand the notice over first.
        if (failure !== undefined) {
          const text = `论文已上传，但解析没有开始：${errorDetail(failure)}。可在第 1 步点“开始解析”重试。`;
          if (projectId) setError(text); else carryNotice(id, text);
        }
        await accepted(id);
      },
    });
  });
  const submitText = (input: { text: string; titleHint?: string }) => act(async () => {
    const id = await ensureProject(input.titleHint ?? '');
    await papers.createTextPaper(id, input);
    await accepted(id);
  });
  const parseAgain = (mode?: DocParseMode) => {
    const paperId = paper?.paperId;
    if (!paperId) return;
    void act(async () => {
      let status = parseStatus;
      if (mode) { status = await papers.setDocParseMode(mode); setParseStatus(status); setModeDialog(false); }
      if (!mode || (status && canParseNow(status, mode))) {
        await papers.parsePaper(paperId, crypto.randomUUID());
        setPolling(paperId);
      }
      await reload();
    });
  };

  const removeDraft = () => {
    if (!projectId || !window.confirm('删除这个草稿？论文和配置将不再显示。')) return;
    void act(async () => { await papers.deleteProject(projectId); window.location.hash = '#/'; });
  };

  // ---- timeline ----
  const summary = (id: SetupStepId): string | null => {
    if (id === 'paper') return !paper ? null : progress.done.paper ? paper.title : paper.textStatus === 'failed' ? '解析失败' : paper.textStatus === 'parsing' ? '解析中…' : '待解析';
    if (id === 'objective') return progress.objective ? clip(progress.objective, 40) : null;
    if (id === 'model') return profile ? connectionLabel(profile) : progress.connectionLost ? '连接已清空，需重新填写' : null;
    return null;
  };
  const setup: SetupView[] = SETUP_ORDER.map(id => ({ id, done: progress.done[id] && reachable(progress, id) && id !== progress.current, summary: summary(id) }));
  const { stages, doneCount } = flowStages({ setup, current: progress.current, task: null, events: [] });

  // ---- files (before start): the parse preview and the saved config ----
  const fileText = useMemo(() => {
    const map = new Map<string, string>();
    if (data?.paperText?.preview) map.set('paper/preview.md', data.paperText.preview);
    if (data?.saved) map.set('project/config.json', JSON.stringify(data.saved.config, null, 2));
    return map;
  }, [data]);
  const files = useMemo<WbFile[] | null>(() => projectId && !data ? null : [...fileText].map(([path, text]) => ({
    path, bytes: utf8Bytes(text), mediaType: path.endsWith('.json') ? 'application/json' : 'text/markdown', badge: path.startsWith('paper/') ? '预览' : '记录',
  })), [data, fileText, projectId]);
  const loadFile = useCallback(async (file: WbFile): Promise<WbContent> => ({ text: fileText.get(file.path) ?? '' }), [fileText]);

  const disabledNext = (reason: string) => busy ? '处理中…' : reason || undefined;
  let step: ReactNode = null;
  if (shown === 'paper') {
    step = <PaperStep paper={paper} paperText={data?.paperText ?? null} parseStatus={parseStatus} busy={busy}
      feedback={feedbackFor('paper', facts, progress)}
      next={{ label: '下一步', disabled: disabledNext(progress.done.paper ? '' : !paper ? '请先上传论文 PDF 或粘贴正文' : '论文解析完成后才能继续'), onClick: () => setViewing('objective') }}
      onUploadPdf={uploadPdf} onSubmitText={submitText}
      parse={{ onRetry: () => parseAgain(), onChoose: mode => parseAgain(mode), onReopenChoice: () => setModeDialog(true), onRefresh: () => { reload().catch(e => setError(errorDetail(e))); } }} />;
  } else if (projectId && data && shown === 'objective') {
    step = <ObjectiveStep projectId={projectId} projectVersion={data.project.version} initial={progress.objective} hasConfig={!!data.saved}
      api={api} papers={papers} busy={busy} act={act} feedback={feedbackFor('objective', facts, progress)} onDone={advance('model')} />;
  } else if (projectId && data && shown === 'model') {
    step = <ModelStep projectId={projectId} config={facts.config} profiles={facts.profiles} connectionLost={progress.connectionLost} objective={progress.objective}
      api={api} busy={busy} act={act} feedback={feedbackFor('model', facts, progress)} onDone={advance('launch')} />;
  } else if (projectId && data?.saved && shown === 'launch') {
    step = <LaunchStep projectId={projectId} paper={paper} paperText={data.paperText} saved={data.saved} profile={profile} objective={progress.objective}
      api={api} busy={busy} act={act} feedback={feedbackFor('launch', facts, progress)} onSaved={async () => { await reload(); }} />;
  }

  return <div className="flow-page">
    <Workbench
      railHead={<RailHead />}
      title={paper?.title || '新建实验'}
      status={{ text: `草稿 · 第 ${SETUP_ORDER.indexOf(progress.current) + 1} 步`, tone: 'idle' }}
      actions={projectId ? <button type="button" className="btn btn-sm btn-secondary" disabled={busy} onClick={removeDraft}>删除草稿</button> : undefined}
      progress={`已完成 ${doneCount} / ${stages.length} 步`}
      stages={stages}
      activeStageId={shown}
      onSelectStage={id => { const target = SETUP_ORDER.find(s => s === id); if (target && reachable(progress, target)) setViewing(target); }}
      files={files}
      loadFile={loadFile}
      filesEmpty="上传论文后，这里会出现解析预览与项目配置。"
    >
      {error && <div className="ui-callout tone-bad flow-error" role="alert"><Icon name="alert" /><span>{error}</span></div>}
      {loading ? <p role="status" className="ui-help">读取草稿…</p> : step}
    </Workbench>
    {modeDialog && <ModeDialog status={parseStatus} busy={busy} onChoose={mode => parseAgain(mode)} onLater={() => setModeDialog(false)} />}
  </div>;
}
