import { useEffect, useRef, useState } from 'react';
import {
  ApiError, createAutonomousApi, type AutonomousApi, type AutonomousEvent, type AutonomousFiles, type AutonomousTask, type Capability, type ModelProfile,
} from '../../api-client/autonomous';
import { createPapersApi, type PapersApi } from '../../api-client/papers';
import { Workbench, type WbContent, type WbDrawer, type WbFile, type WbStage, type WbStageItem } from '../../ui/workbench/Workbench';
import { ToastStack, type ToastItem } from '../../ui/Toast';
import { Lightbox } from '../../ui/Lightbox';
import { commandName } from '../../ui/workbench/tree';
import type { ToastTone } from '../../ui/toastTiming';
import { Badge, Card, CardHeader } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { PHASES, buildStages, currentProtocolSha, flattenItems, progressNotes, railItems, recentActivity, safetyBlock, toWbFiles, type PhaseId } from './stages';
import { ProgressNotes, SpecialistCalls } from './ActivityViews';
import { decisionSuperseded, decisionVisible, errorExplanation, mergeEvents, protocolApproval, taskDisplay, terminalState } from './config';
import { errorDetail } from './errors';
import { connectionLabel } from '../flow/draft';
import RailHead from '../flow/RailHead';
import StepFrame from '../flow/StepFrame';
import { flowStages, type SetupView } from '../flow/timeline';
import { ApprovalCard } from './ApprovalCard';
import { CandidateCard } from './CandidateCard';
import { FiguresView, pickFigures } from './FiguresView';
import { ReportBrief, ReportDetails, ReportEvidence, ReportSummary } from './ReportView';
import { ItemDrawerBody, itemTitle } from './StepDrawer';
import { itemDetail } from './stepDetail';
import { UsageView } from './UsageView';
import { frozenMainProfile, recoveryProfileRequest, recoveryState, type Recovery } from './recovery';
import '../flow/flow.css';
import './autonomous.css';

const defaultApi = createAutonomousApi();
const defaultPapers = createPapersApi('demo-user');
const STAGE_NOTE = '阶段由智能体自报进度与事件归纳得出，不是程序判定';
const pretty = (value: unknown) => JSON.stringify(value, null, 2);

/** 一句话说明任务错误：有已知代码时用中文解释，否则用后端消息；原始 JSON 另放折叠区。 */
function errorSummary(error: unknown): string {
  const e = error && typeof error === 'object' ? error as Record<string, unknown> : {};
  const code = typeof e.code === 'string' ? e.code : '';
  const message = typeof e.message === 'string' ? e.message : '';
  return (code && errorExplanation(code)) || message || code || '任务报告了一个错误，详情见原始错误。';
}

// 任务工作台（#/task/<id>）：阶段时间线 + 当前阶段 + 文件树，与准备阶段同一套版式：
// 中间栏没有页签栏，内容铺满，标题旁「!」放阶段说明，底部固定操作栏放控制按钮。
// 抽屉里显示什么：一条操作（可带返回）、一组操作（一段进度或一个阶段）、08 / 09 阶段、审批、图片、错误或任务详情。
type DrawerState =
  | { kind: 'item'; stageId: string; itemId: string; back?: DrawerState }
  | { kind: 'group'; stageId: string; noteId?: string }
  | { kind: 'stage'; stageId: 'review' | 'report' }
  | { kind: 'details' } | { kind: 'approval' } | { kind: 'error' };

// 状态说明进入该状态时在右下角弹一次。
const STATE_HINTS = {
  paused: '智能体已暂停；后台训练作业可能仍在运行，暂停不会停止远端计费。',
  stopped: '任务已结束；停止任务不会停止远端实例计费。',
  failed: '任务失败。导出 ZIP 可保留全部证据与日志。',
} as const;

const phaseOf = (stageId: string): PhaseId | null => stageId === 'launch' ? 'setup' : PHASES.some(p => p.id === stageId) ? stageId as PhaseId : null;

// 中间栏一屏放下、不滚动：追加指令（折叠）、需要动手的一行（恢复 / 审批）、实时卡、报告摘要与图表。
// 左栏只放节点；过程在右侧抽屉里看：点左栏节点、阶段标题，或标题旁的「任务详情」。
// 提示与操作结果在右下角弹出（5 秒收起，报错要手动关）。
// 审批、继续、暂停、停止是四个独立动作；报告的程序判定与模型叙述分区。
export default function TaskPage({ taskId, api = defaultApi, papers = defaultPapers }: { taskId: string; api?: AutonomousApi; papers?: Pick<PapersApi, 'getProject'> }) {
  const [task, setTask] = useState<AutonomousTask | null>(null);
  const [events, setEvents] = useState<AutonomousEvent[]>([]);
  const [files, setFiles] = useState<AutonomousFiles | null>(null);
  const [filesError, setFilesError] = useState('');
  const [filesTick, setFilesTick] = useState(0);
  const [skills, setSkills] = useState<Capability[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [profilesLoaded, setProfilesLoaded] = useState(false);
  const [profilesTick, setProfilesTick] = useState(0);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const toastSeq = useRef(0);
  const pushToast = (tone: ToastTone, text: string) => setToasts(prev => prev.some(t => t.text === text)
    ? prev : [...prev, { id: `t${++toastSeq.current}`, tone, text }]);
  const closeToast = (id: string) => setToasts(prev => prev.filter(t => t.id !== id));
  const pollError = useRef('');
  const [loadFailed, setLoadFailed] = useState(false);
  const [composeOpen, setComposeOpen] = useState(false);
  // 点图表放大：页面正中的大图，Esc 或点击任意处关闭。
  const [zoom, setZoom] = useState<{ url: string; name: string } | null>(null);
  const [keyInput, setKeyInput] = useState('');
  const [keysInjected, setKeysInjected] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState<DrawerState | null>(null);
  useEffect(() => { setDrawerOpen(null); setZoom(null); setToasts([]); setLoadFailed(false); pollError.current = ''; }, [taskId]);
  // 最近一次操作失败的错误码：CREDENTIAL_REQUIRED 时显示「需要恢复」。
  const [lastControlError, setLastControlError] = useState<string | null>(null);
  const [paperTitle, setPaperTitle] = useState<{ projectId: string; name: string } | null>(null);
  const taskScope = useRef({ id: taskId });
  if (taskScope.current.id !== taskId) taskScope.current = { id: taskId };
  useEffect(() => { setKeysInjected(false); setKeyInput(''); setLastControlError(null); }, [taskId]);

  async function act(work: () => Promise<void>) {
    setBusy(true);
    try {
      await work();
      setLastControlError(null);
    } catch (e) {
      pushToast('bad', errorDetail(e));
      if (e instanceof ApiError) {
        setLastControlError(e.code);
        if (e.code === 'CREDENTIAL_REQUIRED') setKeysInjected(false);
      }
    } finally { setBusy(false); }
  }

  async function controlTask(id: string, action: 'pause' | 'resume' | 'stop' | 'message', text?: string) {
    const scope = taskScope.current;
    const result = await api.control(id, action, text);
    if (applyTask(scope, id, result) && action === 'message') { setMessage(''); pushToast('ok', '指令已发送，智能体会在下一步读取。'); }
  }

  function applyTask(scope: typeof taskScope.current, id: string, result: AutonomousTask) {
    if (taskScope.current !== scope || taskScope.current.id !== id || result.id !== id) return false;
    setTask(result);
    return true;
  }

  // 显式决定：批准或拒绝。两次点击竞争、或决定已被替换时后端返回
  // INVALID_STATE / DECISION_MISMATCH：这不是故障，刷新任务并说明即可。
  async function decideTask(id: string, decisionId: string, action: 'approve' | 'reject', note?: string) {
    const scope = taskScope.current;
    try {
      const result = await api.decide(id, decisionId, action, note);
      if (applyTask(scope, id, result)) pushToast('ok', action === 'approve' ? '已批准方案变更，任务已排队继续。' : '已拒绝方案变更，当前方案保持不变，任务已排队继续。');
    } catch (e) {
      if (!(e instanceof ApiError) || !decisionSuperseded(e.code)) throw e;
      applyTask(scope, id, await api.task(id));
      pushToast('idle', errorExplanation('DECISION_MISMATCH')!);
    }
  }

  // 任务 + 事件流轮询；断线重连从事件游标继续。
  useEffect(() => {
    setTask(null); setEvents([]);
    if (!taskId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    async function poll() {
      try {
        const scope = taskScope.current;
        const [current, stream] = await Promise.all([api.task(taskId, controller.signal), api.events(taskId, cursor, controller.signal)]);
        if (controller.signal.aborted || taskScope.current !== scope || current.id !== scope.id) return;
        setTask(current);
        setEvents(old => mergeEvents(old, stream.events));
        cursor = stream.next_cursor;
        pollError.current = ''; setLoadFailed(false);
      } catch (e) {
        // 轮询每 2 秒一次：同一个错误只弹一次，恢复后再出错才再弹。
        if (!controller.signal.aborted) {
          const text = `任务读取失败：${errorDetail(e)}`;
          if (pollError.current !== text) { pollError.current = text; pushToast('bad', text); }
          setLoadFailed(true);
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [taskId, api]);

  // 标题用论文名（项目名），与准备阶段的页头一致；读不到时回退为实验目标。
  const projectId = task?.project_id ?? '';
  useEffect(() => {
    if (!projectId) return;
    let alive = true;
    papers.getProject(projectId).then(project => { if (alive && project.name) setPaperTitle({ projectId, name: project.name }); }).catch(() => { /* 回退为实验目标 */ });
    return () => { alive = false; };
  }, [papers, projectId]);

  // Skills / 模型配置（快照展示与密钥重注入用）。模型配置只在服务端内存里：任务切换、显示恢复卡片、注入密钥后都重新读取。
  useEffect(() => {
    let alive = true;
    Promise.all([api.capabilities(), api.profiles()]).then(([caps, list]) => {
      if (!alive) return;
      setSkills(caps); setProfiles(list); setProfilesLoaded(true);
    }).catch(() => { /* 非关键信息，忽略 */ });
    return () => { alive = false; };
  }, [api, taskId, profilesTick]);

  // 文件列表：新冻结证据或工具调用完成后重新拉取。
  const fileSignal = events.filter(e => e.type === 'artifact_frozen' || e.type === 'tool_completed' || e.type === 'report_created').length;
  useEffect(() => {
    setFilesError('');
    if (!taskId) { setFiles(null); return; }
    const controller = new AbortController();
    Promise.resolve().then(() => api.files(taskId, controller.signal)).then(result => {
      if (!controller.signal.aborted && taskScope.current.id === taskId) setFiles(result);
    }).catch(e => {
      if (controller.signal.aborted) return;
      setFiles(old => old ?? { items: [], truncated: false, workspace_available: false });
      setFilesError(`文件列表读取失败：${errorDetail(e)}`);
    });
    return () => controller.abort();
  }, [taskId, filesTick, fileSignal, api]);

  // 九步时间线：前四步是已完成的用户步骤（摘要只来自任务快照），后五步由事件归纳。
  const snapshot = task?.config_snapshot;
  const snapshotProfileId = snapshot?.model?.provider_profile || '';
  const snapshotProfile = snapshotProfileId ? profiles.find(p => p.id === snapshotProfileId) : undefined;
  const setup: SetupView[] = [
    { id: 'paper', done: !!task, summary: null },
    { id: 'objective', done: !!task, summary: task?.objective ? task.objective.slice(0, 60) : null },
    { id: 'model', done: !!task, summary: snapshotProfile ? connectionLabel(snapshotProfile) : snapshotProfileId || null },
    { id: 'launch', done: !!task, summary: !snapshot ? null : snapshot.target?.kind === 'ssh' ? `SSH · ${snapshot.target.ssh_config_alias ?? ''}` : '本地隔离环境' },
  ];
  const { stages, activeId } = flowStages({ setup, current: null, task, events });
  // 左栏节点由完整操作列表筛出；抽屉列出全部操作。
  const operations = task ? buildStages(task, events).operations : new Map<PhaseId, WbStageItem[]>();
  const opsOf = (stageId: string) => { const phase = phaseOf(stageId); return phase ? operations.get(phase) ?? [] : []; };
  const stepNumber = stages.findIndex(st => st.id === activeId) + 1;
  const activeTitle = stages[stepNumber - 1]?.title ?? '';
  const latestNote = progressNotes(events)[0]?.summary ?? null;
  const toolSteps = events.filter(e => e.type === 'tool_started').length;
  const eventsLog = events.map(e => `#${e.sequence}  ${e.created_at}  ${e.type}  ${JSON.stringify(e.payload)}`).join('\n');
  const records = new Map<string, string>();
  if (task && events.length) records.set('logs/events.log', eventsLog);
  if (task?.report != null) records.set('task/report.json', pretty(task.report));
  if (task) records.set('task/config.json', pretty(task.config_snapshot));
  const sources = new Map((files?.items ?? []).map(f => [f.path, f.source]));
  const wbFiles: WbFile[] | null = files === null ? null : [
    ...toWbFiles(files.items, { round: typeof task?.repair_round === 'number' ? task.repair_round : null, protocolSha256: currentProtocolSha(task, events) }),
    ...[...records].map(([path, body]) => ({ path, bytes: new TextEncoder().encode(body).length, mediaType: path.endsWith('.json') ? 'application/json' : 'text/plain', badge: '记录' })),
  ];
  const loadFile = async (file: WbFile, signal: AbortSignal): Promise<WbContent> => {
    const body = records.get(file.path);
    if (body !== undefined) return { text: body };
    const source = sources.get(file.path);
    if (!source || !taskId) throw new Error('该文件已不在当前任务中');
    return { blob: await api.fileBlob(taskId, { source, path: file.path }, signal) };
  };

  // ---- 三态与恢复（P0-5）----
  // 需恢复：recovery_required 事件、CREDENTIAL_REQUIRED 错误，或暂停任务的主模型配置不在内存中（见 recovery.ts）。
  const recovery: Recovery = task
    ? recoveryState(task, events, { profiles, profilesLoaded, lastControlError, keysInjected })
    : { needed: false, retainedJobs: [] };
  // 有结局的任务已结束：不再提示恢复、暂停或停止。
  const hasOutcome = task?.outcome != null;
  const needsRecovery = recovery.needed;
  useEffect(() => { if (needsRecovery) setProfilesTick(x => x + 1); }, [needsRecovery]);

  const display = task ? taskDisplay(task) : null;
  const safety = safetyBlock(task, events);
  // 已停止、已结束或有结局的任务不再显示「需要你决定」（后端停止时撤回决定）。
  const approval = task && decisionVisible(task) ? protocolApproval(task.pending_decision) : null;
  // 只有真正在运行且没有结局时才说「运行中 / 正在」；排队、暂停、已有结局都用状态文字与「最近进度」。
  const running = task?.state === 'running' && !hasOutcome;
  const terminal = task ? terminalState(task.state) : false;
  const paused = task?.state === 'paused';
  const stopped = task ? task.state === 'stopped' || task.state === 'cancelled' : false;
  const hint: keyof typeof STATE_HINTS | null = !task || safety ? null
    : task.state === 'failed' ? 'failed'
    : paused && !approval && !hasOutcome ? 'paused'
    : stopped && !hasOutcome ? 'stopped' : null;
  const hintShown = useRef('');
  useEffect(() => {
    const key = hint ? `${taskId}:${hint}` : '';
    if (key === hintShown.current) return;
    hintShown.current = key;
    if (hint) pushToast('idle', STATE_HINTS[hint]);
  }, [hint, taskId]); // eslint-disable-line react-hooks/exhaustive-deps

  const railStages: WbStage[] = stages.map(stage => {
    const phase = phaseOf(stage.id);
    if (!phase || !task) return stage;
    const ops = opsOf(stage.id);
    const nodes = railItems(ops).slice(-14);
    if (stage.id === activeId && running) nodes.push({ id: 'pending', label: '正在整理进展…', variant: 'pending' });
    return { ...stage, items: nodes, disabled: ops.length === 0 && nodes.length === 0 };
  });
  const sentMessages = events.filter(e => e.type === 'user_message').map(e => {
    const payload = (e.payload ?? {}) as Record<string, unknown>;
    return { sequence: e.sequence, text: typeof payload.message === 'string' ? payload.message : '', time: e.created_at.slice(11, 16) };
  }).filter(m => m.text);
  const figureFiles = pickFigures(files?.items ?? []);
  // 报告出来之前，左半边放「最近动态」：最新的操作与进度，随轮询更新，点开看详情。
  const recent = recentActivity(operations, 8);
  const stageOfPhase = (phase: PhaseId) => phase === 'setup' ? 'launch' : phase;

  const budgetExhausted = [...events].reverse().find(e => e.type === 'budget_exhausted');
  // reason 是 {code, message}（worker.record_budget_cancel）；旧事件可能是字符串。
  const budgetReason = budgetExhausted && budgetExhausted.payload && typeof budgetExhausted.payload === 'object'
    ? (() => { const reason = (budgetExhausted.payload as Record<string, unknown>).reason; return typeof reason === 'string' ? reason || null : reason != null ? errorSummary(reason) : null; })() : null;

  const mainProfileId = task?.config_snapshot?.model?.provider_profile ?? '';
  const mainProfile = profiles.find(p => p.id === mainProfileId) ?? null;
  // 在线配置缺失（应用重启后）时，用任务快照中冻结的公开配置重新保存密钥。
  const frozenProfile = frozenMainProfile(task?.config_snapshot);
  const recoveryProfile = mainProfile ?? (frozenProfile?.auth_mode === 'api-key' ? frozenProfile : null);
  const injectHint = !recoveryProfile ? '任务快照中没有可用的主模型配置，无法注入密钥；可以导出 ZIP 后新建任务'
    : !keyInput.trim() ? '请先输入 API Key' : undefined;
  const canResume = !!task && paused && !hasOutcome && task.pending_decision == null && !needsRecovery;
  const resumeHint = hasOutcome ? '任务已有结局，不能继续'
    : !paused ? ''
    : task?.pending_decision != null ? '请先处理上方决策'
    : needsRecovery ? '请先重新输入模型密钥'
    : '';
  // 后端允许暂停 queued 与 running（control.py）；已暂停的任务不需要再暂停。
  const pausable = !!task && (task.state === 'queued' || task.state === 'running');
  const pauseHint = hasOutcome ? '任务已有结局' : task && !terminal && !pausable ? '只有排队中或运行中的任务可以暂停' : undefined;
  // 结局已定但生命周期仍在暂停：停止仍可用，用来终止保留的后台作业。
  const stopHint = hasOutcome && !terminal ? '结局已定；停止会终止仍保留的后台作业' : '停止任务（不会停止远端实例计费）';

  const taskSkills = task?.config_snapshot?.harness?.skills ?? [];
  const skillSnapshot = taskSkills.map(id => skills.find(s => s.id === id) ?? null);

  const title = paperTitle && paperTitle.projectId === projectId ? paperTitle.name : task?.objective ?? '正在读取任务…';
  const phase = PHASES.find(p => p.id === (activeId === 'launch' ? 'setup' : activeId));
  const guide = `${phase?.idle ? `${phase.idle}。` : ''}${STAGE_NOTE}。`;

  const summaryList = task ? <dl className="flow-summary">
    {paperTitle && paperTitle.projectId === projectId && <div><dt>论文</dt><dd>{paperTitle.name}</dd></div>}
    <div><dt>目标</dt><dd>{task.objective}</dd></div>
    <div><dt>模型</dt><dd>{snapshotProfile ? connectionLabel(snapshotProfile) : snapshotProfileId || '—'}</dd></div>
    <div><dt>任务</dt><dd className="ui-mono">{task.id}</dd></div>
  </dl> : null;
  const stepLabel = (stageId: string) => {
    const index = stages.findIndex(st => st.id === stageId);
    return index < 0 ? '' : `步骤 ${String(index + 1).padStart(2, '0')} · ${stages[index].title}`;
  };
  const closeDrawer = () => setDrawerOpen(null);
  const opRows = (stageId: string, items: WbStageItem[], from: DrawerState) => items.length === 0
    ? <p className="ui-help">这里还没有操作记录。</p>
    : <ul className="auto-ops" aria-label="操作">{items.map(item => item.variant === 'note'
      ? <li key={item.id}><button type="button" className="auto-op-note" onClick={() => setDrawerOpen({ kind: 'group', stageId, noteId: item.id })}>{item.label}</button></li>
      : <li key={item.id}><button type="button" className="auto-op" onClick={() => setDrawerOpen({ kind: 'item', stageId, itemId: item.id, back: from })}>
        <span className={`wb-sub-dot${item.tone ? ` tone-${item.tone}` : ''}`} aria-hidden="true" />
        <span className="auto-op-text"><span className="auto-op-label">{item.label}</span>{(item.detail || item.time) && <small>{[item.detail, item.time].filter(Boolean).join(' · ')}</small>}</span>
        <Icon name="chevron-right" size={12} className="wb-sub-go" />
      </button></li>)}</ul>;
  let drawer: WbDrawer | null = null;
  if (task && drawerOpen?.kind === 'item') {
    const detail = itemDetail(events, drawerOpen.itemId, task);
    const back = drawerOpen.back;
    if (detail) drawer = { label: stepLabel(drawerOpen.stageId), title: itemTitle(detail), onClose: closeDrawer, openPath: detail.kind === 'artifact' ? detail.path : undefined, body: <>
      {back && <button type="button" className="btn btn-ghost btn-sm auto-drawer-back" onClick={() => setDrawerOpen(back)}><Icon name="chevron-left" size={13} />返回列表</button>}
      <ItemDrawerBody detail={detail} />
    </> };
  } else if (task && drawerOpen?.kind === 'group') {
    const from = drawerOpen;
    const all = flattenItems(opsOf(drawerOpen.stageId));
    const note = drawerOpen.noteId ? all.find(item => item.id === drawerOpen.noteId) : undefined;
    drawer = note
      ? { label: stepLabel(drawerOpen.stageId), title: '智能体进度', onClose: closeDrawer, body: <>
        <p className="auto-drawer-note"><Badge tone="idle">模型叙述（不是执行证据）</Badge>{note.time && <time className="ui-help"> {note.time}</time>}</p>
        <p className="auto-drawer-text">{note.label}</p>
        <span className="auto-drawer-label">这段进度下的操作</span>
        {opRows(drawerOpen.stageId, flattenItems(note.children ?? []), from)}
      </> }
      : { label: stepLabel(drawerOpen.stageId), title: '这一步的全部操作', onClose: closeDrawer, body: opRows(drawerOpen.stageId, all, from) };
  } else if (task && drawerOpen?.kind === 'approval' && approval) {
    drawer = { label: stepLabel('plan'), title: '方案审批', onClose: closeDrawer, body: <ApprovalCard approval={approval} busy={busy}
      blockedReason={task.state !== 'paused' ? '任务暂停后才能处理决定' : undefined}
      onApprove={() => { closeDrawer(); void act(() => decideTask(task.id, approval.id, 'approve')); }}
      onReject={note => { closeDrawer(); void act(() => decideTask(task.id, approval.id, 'reject', note)); }} /> };
  } else if (task && drawerOpen?.kind === 'error' && task.error != null) {
    drawer = { label: '任务', title: '阻塞 / 错误', onClose: closeDrawer, body: <>
      <p className="auto-drawer-text">{errorSummary(task.error)}</p>
      <section className="auto-drawer-block"><span className="auto-drawer-label">原始错误</span><pre className="ui-code is-wrap">{pretty(task.error)}</pre></section>
    </> };
  } else if (task && drawerOpen?.kind === 'stage' && drawerOpen.stageId === 'review') {
    drawer = { label: stepLabel('review'), title: '证据与独立复核', onClose: closeDrawer, body: <>
      {task.report != null && <ReportEvidence report={task.report} />}
      <CandidateCard task={task} events={events} />
    </> };
  } else if (task && drawerOpen?.kind === 'stage' && drawerOpen.stageId === 'report') {
    drawer = { label: stepLabel('report'), title: '实验报告', onClose: closeDrawer, body: task.report != null
      ? <><ReportSummary report={task.report} /><ReportDetails report={task.report} /></>
      : <p className="ui-help">报告尚未生成；生成后这里显示结局、结论、指标、完成门槛、执行观测、模型叙述与局限。</p> };
  } else if (task && drawerOpen?.kind === 'details') {
    drawer = { label: '任务', title: '任务详情', onClose: closeDrawer, body: <>
      {summaryList}
      <UsageView usage={(task.usage ?? {}) as Record<string, unknown>} limits={task.config_snapshot?.limits as unknown as Record<string, unknown> | undefined} budgetExhausted={budgetReason} />
      <ProgressNotes events={events} />
      <SpecialistCalls events={events} task={task} configured={task.config_snapshot?.specialists?.length ?? 0} />
      {taskSkills.length > 0 && <Card>
        <CardHeader icon="spark" title="本任务固定的 Skills 快照" subtitle="运行期间固定使用以下版本，不随后续更新变化。" />
        <div className="ui-card-section">
          <ul className="ui-list">
            {taskSkills.map((id, i) => {
              const skill = skillSnapshot[i];
              return <li key={id} className="ui-row">
                <span className="ui-row-main"><span className="ui-row-title">{skill?.name ?? id}</span>
                  <span className="ui-row-sub ui-mono">{id}</span></span>
                <span className="ui-row-end">{skill ? <Badge>{skill.version}</Badge> : <Badge tone="idle">版本未知</Badge>}</span>
              </li>;
            })}
          </ul>
        </div>
      </Card>}
      <Disclosure variant="card" title="任务配置快照">
        <pre className="ui-code">{pretty(task.config_snapshot)}</pre>
      </Disclosure>
      <Disclosure variant="card" title={`原始事件流（${events.length}）`}>
        {events.length === 0 && <p className="ui-help">等待任务事件…</p>}
        {events.length > 0 && <ul className="ui-list auto-events" aria-label="任务事件">
          {events.map(event => <li key={event.sequence} className="ui-row auto-event">
            <span className="ui-row-main">
              <span className="ui-row-title ui-mono">#{event.sequence} {event.type}</span>
              <span className="ui-row-sub"><time>{event.created_at}</time></span>
            </span>
            <Disclosure variant="sub" title="payload"><pre className="ui-code">{pretty(event.payload)}</pre></Disclosure>
          </li>)}
        </ul>}
      </Disclosure>
    </> };
  }

  const controls = task ? <>
    <button type="button" className="btn btn-secondary" disabled={busy || terminal || hasOutcome || !pausable} title={pauseHint} onClick={() => void act(async () => { await controlTask(task.id, 'pause'); })}>暂停</button>
    <button type="button" className={`btn ${canResume ? 'btn-primary' : 'btn-secondary'}`} disabled={busy || terminal || !canResume} title={resumeHint || undefined} onClick={() => void act(async () => { await controlTask(task.id, 'resume'); })}>继续</button>
    <button type="button" className="btn btn-secondary" disabled={busy || terminal} title={stopHint} onClick={() => void act(async () => { await controlTask(task.id, 'stop'); })}>停止</button>
    <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void act(() => api.exportTask(task.id))}><Icon name="download" size={14} />导出 ZIP</button>
    {resumeHint && <span className="ui-help">{resumeHint}</span>}
    {hasOutcome && !terminal && <span className="ui-help">{stopHint}</span>}
  </> : undefined;

  return <div className="flow-page"><Workbench
    railHead={<RailHead />}
    title={title}
    status={task && display ? {
      text: display.text,
      tone: !hasOutcome && needsRecovery && display.tone !== 'bad' ? 'warn' : display.tone,
      title: display.caption ?? undefined,
    } : null}
    actions={task ? <button type="button" className="btn btn-secondary btn-sm" aria-haspopup="dialog" onClick={() => setDrawerOpen({ kind: 'details' })}><Icon name="list" size={14} />任务详情</button> : undefined}
    bareLive
    liveLabel={activeTitle || '当前阶段'}
    onOpenItem={(stageId, item) => setDrawerOpen(item.variant === 'note' ? { kind: 'group', stageId, noteId: item.id } : { kind: 'item', stageId, itemId: item.id })}
    onOpenStage={stageId => {
      if (stageId === 'review' || stageId === 'report') setDrawerOpen({ kind: 'stage', stageId });
      else if (task && opsOf(stageId).length > 0) setDrawerOpen({ kind: 'group', stageId });
    }}
    selectedItemId={drawerOpen?.kind === 'item' ? drawerOpen.itemId : drawerOpen?.kind === 'group' ? drawerOpen.noteId ?? null : null}
    drawer={drawer}
    stages={railStages}
    activeStageId={task ? activeId : null}
    files={wbFiles}
    filesError={filesError || null}
    filesEmpty=""
    onRefreshFiles={taskId ? () => setFilesTick(x => x + 1) : undefined}
    loadFile={loadFile}
  >
    <StepFrame title={task ? activeTitle || '当前阶段' : '正在读取任务…'} guide={guide} actions={controls}>
      {!task && <div className="wb-empty-live"><span>{loadFailed ? '任务读取失败，正在重试…' : '正在读取任务…'}</span></div>}

      {task && <div className="auto-screen">
        {!terminal && !hasOutcome && <div className={`auto-compose-bar${composeOpen ? ' is-open' : ''}`}>
          <button type="button" className="auto-compose-toggle" aria-expanded={composeOpen} onClick={() => setComposeOpen(open => !open)}>
            <Icon name="chevron-right" size={13} className="auto-compose-chev" /><b>追加指令</b>
            {sentMessages.length > 0 && <span className="ui-help">已发送 {sentMessages.length} 条</span>}
          </button>
          {composeOpen && <div className="auto-compose-body">
            <div className="auto-compose-row">
              <input type="text" aria-label="追加指令" value={message} placeholder="例如：先用小数据集跑通，再扩大规模"
                onChange={e => setMessage(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter' && !e.nativeEvent.isComposing && message.trim() && !busy) void act(async () => { await controlTask(task.id, 'message', message.trim()); }); }} />
              <button type="button" className="btn btn-primary" disabled={busy || !message.trim()} onClick={() => void act(async () => { await controlTask(task.id, 'message', message.trim()); })}>发送指令</button>
            </div>
            {sentMessages.length > 0 && <ul className="auto-sent" aria-label="已发送的指令">
              {sentMessages.slice(-3).reverse().map(m => <li key={m.sequence}><time>{m.time}</time><span title={m.text}>{m.text}</span></li>)}
            </ul>}
          </div>}
        </div>}

        {safety && <div className="auto-bar tone-bad" role="alert"><Icon name="shield" size={16} />
          <span className="auto-bar-text"><b>容器隔离校验失败，任务已安全终止，不能恢复</b><span className="auto-clamp">代码 {safety.code ?? '未知'}；仍可导出 ZIP 保留证据。</span></span>
        </div>}

        {needsRecovery && <div className="auto-bar tone-warn" role="group" aria-label="需要恢复">
          <Icon name="key" size={16} />
          <span className="auto-bar-text"><b>需要恢复</b><span className="auto-clamp">{recoveryProfile
            ? `应用曾退出，密钥已失效：重新输入「${recoveryProfile.model}」的 API Key（只存在内存里），再点「继续」。`
            : '任务快照中没有可用的主模型配置，无法注入密钥；可以导出 ZIP 后新建任务。'}</span></span>
          {recoveryProfile && <>
            <input className="auto-bar-input" type="password" autoComplete="off" aria-label="API Key" placeholder="API Key" value={keyInput} disabled={busy} onChange={e => setKeyInput(e.target.value)} />
            <button type="button" className="btn btn-primary" disabled={busy || !!injectHint} title={injectHint} onClick={() => void act(async () => {
              const request = recoveryProfileRequest(mainProfile, task.config_snapshot, keyInput);
              if (!request) return;
              await api.saveProfile(request);
              setKeyInput('');
              setKeysInjected(true);
              setProfilesTick(x => x + 1);
              pushToast('ok', '密钥已重新注入；现在可以点击「继续」恢复任务。');
            })}>注入密钥</button>
          </>}
        </div>}

        {approval && <ApprovalCard compact approval={approval} busy={busy}
          blockedReason={task.state !== 'paused' ? '任务暂停后才能处理决定' : undefined}
          onExpand={() => setDrawerOpen({ kind: 'approval' })}
          onApprove={() => void act(() => decideTask(task.id, approval.id, 'approve'))}
          onReject={note => void act(() => decideTask(task.id, approval.id, 'reject', note))} />}

        <div className="auto-live-status" role="status">
          <p className="wb-live-head">
            {running && <span className="wb-live-dot" aria-hidden="true" />}
            <b>{running ? '运行中' : display?.text}</b>{display?.lifecycle ? `（生命周期：${display.lifecycle}）` : ''} · 第 {stepNumber} / 9 步 · {activeTitle}
            <Badge tone="idle" title={STAGE_NOTE}>按事件归纳</Badge>
          </p>
          {latestNote && <p className="auto-live-now" title={latestNote}>{running ? '正在：' : '最近进度：'}{latestNote}</p>}
          <p className="ui-help">已执行 {toolSteps} 步操作{display?.caption ? ` · ${display.caption}` : ''}</p>
          {task.error != null && <p className="auto-live-error" role="alert"><Icon name="alert" size={14} /><span title={errorSummary(task.error)}>{errorSummary(task.error)}</span>
            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setDrawerOpen({ kind: 'error' })}>原始错误</button></p>}
        </div>

        <div className={`auto-results${figureFiles.length > 0 ? ' has-figures' : ''}`}>
          {task.report != null
            ? <ReportBrief report={task.report} onOpen={() => setDrawerOpen({ kind: 'stage', stageId: 'report' })} />
            : <section className="auto-feed" aria-label="最近动态">
              <header className="auto-feed-head">
                {running && <span className="wb-live-dot" aria-hidden="true" />}<b>最近动态</b>
                <span className="ui-help">报告生成后这里换成结局、结论与指标</span>
              </header>
              {recent.length === 0
                ? <p className="ui-help auto-feed-empty">智能体开始操作后，这里实时显示最新的进展。</p>
                : <ul className="auto-feed-list">{recent.map(({ phase, item }) => {
                  const stageId = stageOfPhase(phase);
                  const label = item.variant === 'command' ? commandName(item.label) : item.label;
                  return <li key={item.id}><button type="button" className={`auto-feed-row${item.variant ? ` is-${item.variant}` : ''}`}
                    onClick={() => setDrawerOpen(item.variant === 'note' ? { kind: 'group', stageId, noteId: item.id } : { kind: 'item', stageId, itemId: item.id })}>
                    <span className={`wb-sub-dot${item.tone ? ` tone-${item.tone}` : ''}`} aria-hidden="true" />
                    <span className="auto-feed-label" title={label}>{item.variant === 'note' ? <><span className="auto-feed-tag">进度</span>{label}</> : label}</span>
                    {item.time && <time>{item.time}</time>}
                  </button></li>;
                })}</ul>}
            </section>}
          <FiguresView compact items={files?.items ?? []} load={(file, signal) => api.fileBlob(task.id, { source: file.source, path: file.path }, signal)}
            onOpen={(file, url) => setZoom({ url, name: file.path.slice(file.path.lastIndexOf('/') + 1) })} />
        </div>
      </div>}
    </StepFrame>
  </Workbench>
    {zoom && <Lightbox url={zoom.url} name={zoom.name} onClose={() => setZoom(null)} />}
    <ToastStack items={toasts} onClose={closeToast} /></div>;
}
