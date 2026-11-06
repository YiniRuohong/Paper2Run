import { useEffect, useRef, useState } from 'react';
import {
  ApiError, createAutonomousApi, type AutonomousApi, type AutonomousEvent, type AutonomousFiles, type AutonomousTask, type Capability, type ModelProfile,
} from '../../api-client/autonomous';
import { Workbench, type WbContent, type WbFile } from '../../ui/workbench/Workbench';
import { Badge, Card, CardHeader } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { buildStages, currentProtocolSha, safetyBlock, toWbFiles } from './stages';
import { ProgressNotes, SpecialistCalls } from './ActivityViews';
import { decisionSuperseded, errorExplanation, mergeEvents, protocolApproval, taskDisplay, terminalState } from './config';
import { errorDetail } from './errors';
import { ApprovalCard } from './ApprovalCard';
import { CandidateCard } from './CandidateCard';
import { ReportView } from './ReportView';
import { UsageView } from './UsageView';
import './autonomous.css';

const defaultApi = createAutonomousApi();
const pretty = (value: unknown) => JSON.stringify(value, null, 2);
export { errorDetail };

type Recovery = { needed: boolean; retainedJobs: string[] };

// 任务工作台（#/task/<id>）：阶段时间线 + 实时页签 + 文件树。
// 审批、继续、暂停、停止是四个独立动作；报告的程序判定与模型叙述分区。
export default function TaskPage({ taskId, api = defaultApi }: { taskId: string; api?: AutonomousApi }) {
  const [task, setTask] = useState<AutonomousTask | null>(null);
  const [tasks, setTasks] = useState<AutonomousTask[]>([]);
  const [events, setEvents] = useState<AutonomousEvent[]>([]);
  const [files, setFiles] = useState<AutonomousFiles | null>(null);
  const [filesError, setFilesError] = useState('');
  const [filesTick, setFilesTick] = useState(0);
  const [skills, setSkills] = useState<Capability[]>([]);
  const [profiles, setProfiles] = useState<ModelProfile[]>([]);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [keyInput, setKeyInput] = useState('');
  const [keysInjected, setKeysInjected] = useState(false);
  const taskScope = useRef({ id: taskId, projectId: '' });
  if (taskScope.current.id !== taskId) taskScope.current = { id: taskId, projectId: '' };
  useEffect(() => { setKeysInjected(false); setKeyInput(''); }, [taskId]);

  async function act(work: () => Promise<void>) {
    setBusy(true); setError(''); setNotice('');
    try { await work(); } catch (e) { setError(errorDetail(e)); } finally { setBusy(false); }
  }

  async function controlTask(id: string, action: 'pause' | 'resume' | 'stop' | 'message', text?: string) {
    const scope = taskScope.current;
    const result = await api.control(id, action, text);
    if (applyTask(scope, id, result) && action === 'message') setMessage('');
  }

  function applyTask(scope: typeof taskScope.current, id: string, result: AutonomousTask) {
    if (taskScope.current !== scope || taskScope.current.id !== id || result.id !== id) return false;
    setTask(result);
    setTasks(old => old.map(item => (item.id === result.id ? result : item)));
    return true;
  }

  // 显式决定：批准或拒绝。两次点击竞争、或决定已被替换时后端返回
  // INVALID_STATE / DECISION_MISMATCH：这不是故障，刷新任务并说明即可。
  async function decideTask(id: string, decisionId: string, action: 'approve' | 'reject', note?: string) {
    const scope = taskScope.current;
    try {
      const result = await api.decide(id, decisionId, action, note);
      if (applyTask(scope, id, result)) setNotice(action === 'approve' ? '已批准方案变更，任务已排队继续。' : '已拒绝方案变更，当前方案保持不变，任务已排队继续。');
    } catch (e) {
      if (!(e instanceof ApiError) || !decisionSuperseded(e.code)) throw e;
      applyTask(scope, id, await api.task(id));
      setNotice(errorExplanation('DECISION_MISMATCH')!);
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
        setTasks(old => {
          if (old.some(item => item.id === current.id)) return old.map(item => (item.id === current.id ? current : item));
          return old;
        });
        setEvents(old => mergeEvents(old, stream.events));
        cursor = stream.next_cursor;
      } catch (e) {
        if (!controller.signal.aborted) setError(errorDetail(e));
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 2000);
    }
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [taskId, api]);

  // 同项目的任务列表（切换器用）与 Skills / 模型配置（快照展示与密钥重注入用）。
  const projectId = task?.project_id ?? '';
  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    api.tasks(projectId).then(list => {
      if (!controller.signal.aborted && (taskScope.current.projectId === '' || taskScope.current.projectId === projectId)) setTasks(list);
    }).catch(() => { /* 切换器降级为只显示当前任务 */ });
    return () => controller.abort();
  }, [projectId, api]);
  useEffect(() => {
    let alive = true;
    Promise.all([api.capabilities(), api.profiles()]).then(([caps, list]) => {
      if (!alive) return;
      setSkills(caps); setProfiles(list);
    }).catch(() => { /* 非关键信息，忽略 */ });
    return () => { alive = false; };
  }, [api]);

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

  const timeline = buildStages(task, events);
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
  // 需恢复：最近一次恢复类事件是 recovery_required（应用曾退出，密钥未重注入）。
  let recovery: Recovery = { needed: false, retainedJobs: [] };
  for (const event of events) {
    if (event.type === 'recovery_required') recovery = { needed: true, retainedJobs: [] };
    if (event.type === 'task_resumed' || event.type === 'task_finished') recovery = { needed: false, retainedJobs: [] };
    if ((event.type === 'task_paused' || event.type === 'application_exit') && recovery.needed) {
      const retained = (event.payload as Record<string, unknown> | null)?.background_jobs_retained;
      if (Array.isArray(retained)) recovery = { ...recovery, retainedJobs: retained.map(String) };
    }
  }
  // 有结局的任务已结束：不再提示恢复、暂停或停止。
  const hasOutcome = task?.outcome != null;
  if (task && (terminalState(task.state) || hasOutcome)) recovery = { needed: false, retainedJobs: [] };
  const needsRecovery = recovery.needed && !keysInjected;

  const display = task ? taskDisplay(task) : null;
  const safety = safetyBlock(task, events);
  const approval = protocolApproval(task?.pending_decision);
  const running = task ? !terminalState(task.state) && task.state !== 'paused' && !hasOutcome : false;
  const terminal = task ? terminalState(task.state) : false;
  const paused = task?.state === 'paused';
  const stopped = task ? task.state === 'stopped' || task.state === 'cancelled' : false;
  const budgetExhausted = [...events].reverse().find(e => e.type === 'budget_exhausted');
  const budgetReason = budgetExhausted && budgetExhausted.payload && typeof budgetExhausted.payload === 'object'
    ? String((budgetExhausted.payload as Record<string, unknown>).reason ?? '') || null : null;

  const mainProfileId = task?.config_snapshot?.model?.provider_profile ?? '';
  const mainProfile = profiles.find(p => p.id === mainProfileId) ?? null;
  const canResume = !!task && paused && !hasOutcome && task.pending_decision == null && !needsRecovery;
  const resumeHint = hasOutcome ? '任务已有结局，不能继续'
    : !paused ? ''
    : task?.pending_decision != null ? '请先处理上方决策'
    : needsRecovery ? '请先重新输入模型密钥'
    : '';
  const pauseHint = hasOutcome ? '任务已有结局' : task && !terminal && task.state !== 'running' ? '只有运行中的任务可以暂停' : undefined;
  // 结局已定但生命周期仍在暂停：停止仍可用，用来终止保留的后台作业。
  const stopHint = hasOutcome && !terminal ? '结局已定；停止会终止仍保留的后台作业' : '停止任务（不会停止远端实例计费）';
  const messageHint = hasOutcome ? '任务已有结局，不再接收指令' : terminal ? '任务已结束' : undefined;

  const taskSkills = task?.config_snapshot?.harness?.skills ?? [];
  const skillSnapshot = taskSkills.map(id => skills.find(s => s.id === id) ?? null);

  return <Workbench
    eyebrow={<>自主实验{task ? <> · <span className="ui-mono">{task.id}</span></> : null}</>}
    title={task?.objective ?? '正在读取任务…'}
    status={task && display ? {
      text: display.text,
      tone: !hasOutcome && needsRecovery && display.tone !== 'bad' ? 'warn' : display.tone,
    } : null}
    meta={task && display ? <>
      {display.lifecycle && <span>生命周期：{display.lifecycle}</span>}
      {display.caption && <span>{display.caption}</span>}
      <span>更新于 {task.updated_at || '—'}</span><span>{events.length} 条事件</span>
    </> : <span>任务的阶段、命令输出和产物会在这里持续更新。</span>}
    progress={<span className="auto-progress">进度 · {timeline.stages.filter(st => st.mark === 'done').length} / {timeline.stages.length}<Badge tone="idle" title="阶段由 Pi 自报进度（note_progress）与事件归纳得出，不是程序判定">按事件归纳</Badge></span>}
    stages={timeline.stages}
    activeStageId={task ? timeline.active : null}
    files={wbFiles}
    filesError={filesError || null}
    filesEmpty="还没有文件。实验写入工作区或冻结证据后会出现在这里。"
    filesNote={files ? <>「证据」是已冻结并按 SHA-256 校验的产物{files.workspace_available ? '；workspace 为本机隔离工作区的实时文件' : '；远程工作区不可浏览，仅显示冻结证据'}{files.truncated ? '；文件过多，仅列出前 2000 个' : ''}。</> : null}
    onRefreshFiles={taskId ? () => setFilesTick(x => x + 1) : undefined}
    loadFile={loadFile}
  >
    <div className="auto-live">
      <div className="auto-taskbar">
        <label className="ui-field auto-taskbar-field"><span className="ui-field-label">项目任务</span>
          <select value={taskId} disabled={busy} onChange={e => { if (e.target.value) window.location.hash = `#/task/${encodeURIComponent(e.target.value)}`; }}>
            {!tasks.some(t => t.id === taskId) && <option value={taskId}>{taskId}</option>}
            {tasks.map(t => <option key={t.id} value={t.id}>{t.objective}（{taskDisplay(t).text}）</option>)}
          </select>
        </label>
        <a className="btn btn-secondary" href="#/new">新建实验</a>
      </div>

      {error && <div className="ui-callout tone-bad" role="alert"><Icon name="alert" /><span>{error}</span><button type="button" className="btn btn-sm" onClick={() => window.location.reload()}>重新读取</button></div>}
      {notice && <p role="status" className="ui-callout tone-idle"><Icon name="info" /><span>{notice}</span></p>}

      {!task && !error && <div className="wb-empty-live"><span>正在读取任务…</span></div>}

      {task && <>
        <p className="wb-live-head">
          {running && <span className="wb-live-dot" aria-hidden="true" />}
          <b>{display?.text}</b>{display?.lifecycle ? `（生命周期：${display.lifecycle}）` : ''} · 第 {timeline.stages.findIndex(st => st.id === timeline.active) + 1}/{timeline.stages.length} 阶段 · {events.length} 条事件 · 断线重连会从事件游标继续读取
        </p>

        {safety && <div className="ui-callout tone-bad" role="alert"><Icon name="shield" />
          <span>容器隔离校验失败，任务已安全终止，不能恢复。代码：<code className="ui-mono">{safety.code ?? '未知'}</code>。仍可导出 ZIP 保留证据。</span>
        </div>}
        {paused && !approval && !hasOutcome && <div className="ui-callout tone-warn" role="status"><Icon name="clock" /><span>AI 调度已暂停；后台训练作业可能仍在运行，暂停不会停止远端计费。</span></div>}
        {stopped && !hasOutcome && <div className="ui-callout tone-idle" role="status"><Icon name="info" /><span>任务已结束；停止任务不会停止远端实例计费。</span></div>}
        {task.state === 'failed' && <div className="ui-callout tone-bad" role="alert"><Icon name="alert" /><span>任务失败。导出 ZIP 可保留全部证据与日志。</span></div>}

        {needsRecovery && <Card className="auto-recovery">
          <CardHeader icon="key" title="需要恢复" status={{ text: '需重新注入密钥', tone: 'warn' }}
            subtitle="应用曾退出：AI 调度已停止，密钥仅存于内存已失效。按顺序完成以下三步后可继续。" />
          <div className="ui-card-section auto-recovery-body">
            <ol className="auto-steps">
              <li>查看后台作业状态{recovery.retainedJobs.length > 0 ? `（保留 ${recovery.retainedJobs.length} 个作业）` : '（文件树与事件日志可回看）'}</li>
              <li>重新输入模型密钥（仅保存在内存，不写入浏览器存储）</li>
              <li>点击「继续」恢复任务</li>
            </ol>
            <div className="ui-form">
              <label className="ui-field"><span className="ui-field-label">模型配置</span>
                <select value={mainProfileId} disabled>{mainProfile
                  ? <option value={mainProfile.id}>{mainProfile.id} · {mainProfile.model}</option>
                  : <option value={mainProfileId}>{mainProfileId || '（读取中…）'}</option>}</select>
              </label>
              <label className="ui-field"><span className="ui-field-label">API Key<small>仅本次内存注入，重启应用后需重新输入</small></span>
                <input type="password" autoComplete="off" value={keyInput} disabled={busy || !mainProfile} onChange={e => setKeyInput(e.target.value)} />
              </label>
            </div>
            <div className="auto-actions">
              <button type="button" className="btn btn-primary" disabled={busy || !mainProfile || !keyInput.trim()} onClick={() => void act(async () => {
                if (!mainProfile) return;
                await api.saveProfile({ ...mainProfile, api_key: keyInput.trim() });
                setKeyInput('');
                setKeysInjected(true);
                setNotice('密钥已重新注入；现在可以点击「继续」恢复任务。');
              })}>注入密钥</button>
            </div>
          </div>
        </Card>}

        {approval && <ApprovalCard approval={approval} busy={busy}
          blockedReason={task.state !== 'paused' ? '任务暂停后才能处理决定' : undefined}
          onApprove={() => void act(() => decideTask(task.id, approval.id, 'approve'))}
          onReject={note => void act(() => decideTask(task.id, approval.id, 'reject', note))}
          onPause={() => void act(async () => { await controlTask(task.id, 'pause'); })} />}

        {task.error != null && <div className="ui-callout tone-bad" role="alert"><Icon name="alert" />
          <div className="auto-error"><b>阻塞 / 错误</b><pre className="ui-code">{pretty(task.error)}</pre></div>
        </div>}

        <div className="auto-actions">
          <button type="button" className="btn btn-secondary" disabled={busy || terminal || hasOutcome || task.state !== 'running'} title={pauseHint} onClick={() => void act(async () => { await controlTask(task.id, 'pause'); })}>暂停</button>
          <button type="button" className="btn btn-secondary" disabled={busy || terminal || !canResume} title={resumeHint || undefined} onClick={() => void act(async () => { await controlTask(task.id, 'resume'); })}>继续</button>
          {resumeHint && <span className="ui-help">{resumeHint}</span>}
          <button type="button" className="btn btn-secondary" disabled={busy || terminal} title={stopHint} onClick={() => void act(async () => { await controlTask(task.id, 'stop'); })}>停止</button>
          {hasOutcome && !terminal && <span className="ui-help">{stopHint}</span>}
          <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void act(() => api.exportTask(task.id))}><Icon name="download" size={14} />导出 ZIP</button>
        </div>

        <div className="ui-form auto-compose">
          <label className="ui-field"><span className="ui-field-label">追加指令</span>
            <textarea rows={2} value={message} onChange={e => setMessage(e.target.value)} placeholder="例如：先用小数据集跑通，再扩大规模" />
          </label>
          <div className="auto-actions">
            <button type="button" className="btn btn-primary" disabled={busy || !message.trim() || terminal || hasOutcome} title={messageHint} onClick={() => void act(async () => { await controlTask(task.id, 'message', message.trim()); })}>发送指令</button>
            {messageHint && <span className="ui-help">{messageHint}</span>}
          </div>
        </div>

        <ProgressNotes events={events} />

        <CandidateCard task={task} events={events} />

        {task.report != null && <ReportView report={task.report} />}

        <UsageView usage={(task.usage ?? {}) as Record<string, unknown>} limits={task.config_snapshot?.limits as unknown as Record<string, unknown> | undefined} budgetExhausted={budgetReason} />

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
      </>}
    </div>
  </Workbench>;
}
