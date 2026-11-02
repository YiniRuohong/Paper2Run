import { useEffect, useRef, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import type { GuidedExport, GuidedTrainingForm } from '../../api-client/guided-types';
import { RealFlowController } from './realflow';
import { candidateBlockedReason, candidateKey, isSandboxRecipeSelected, realFlowSteps, REALFLOW_TRAINING_FIELDS, REALFLOW_TRAINING_LABELS, type RealFlowState, type RealFlowStep } from './realflow-state';
import { nextViewableStep, stepRows, type StepRow } from './realflow-summary';
import { pollExports, pollRunAndReport, readReport } from './readback';
import { Badge, KV, KVList, StatusWord, type Tone } from '../../ui/Card';
import { CopyButton, Switch } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import ModeDialog from '../docparse/ModeDialog';
import ParseProgress from '../docparse/ParseProgress';
import { canExtract } from '../docparse/docparse';

// Real article flow panel: 文章输入 → Provider → 抽取 → 方案 → 环境检查 → 执行。
// sandbox_python 候选在环境与执行之间多出「复现代码」步骤（生成复现代码 → 沙箱执行）。
// It shares the honest-wording rules of the guided demo: nothing here may
// present a fake/unverified result as real, and the provider api_key only
// lives in React state (never localStorage).
// Presentation: a vertical stack of step cards. The current step is expanded,
// other viewable steps collapse to a one-line summary, locked steps are dimmed.

const SUPPORT_BADGE: Record<string, string> = {
  supported: 'supported（可执行）',
  requires_review: 'requires_review（需人工复核，禁用）',
  unsupported: 'unsupported（不可执行，禁用）',
};
const EVIDENCE_STATUS_TEXT: Record<string, string> = {
  supported: 'supported（有来源支撑）',
  inferred: 'inferred（推断，未直接给出）',
  missing: 'missing（缺失）',
  conflicting: 'conflicting（冲突）',
};
const ENV_ITEM_TEXT: Record<string, string> = {
  pass: 'pass', fail: 'fail', unknown: 'unknown（未知，不视为通过）', not_needed: 'not_needed（不需要）',
};
const RUN_STATUS_TEXT: Record<string, string> = {
  queued: 'queued（已排队）：已接受，等待受控执行；尚未开始。',
  running: 'running（执行中）：正在执行受控流水线。',
  evaluating: 'evaluating（评估中）：正在评估结果。',
  cancel_requested: 'cancel_requested（已请求取消）：等待取消确认。',
  completed: 'completed（已完成）：本次 Run 已完成；证据等级以下方 evidence_class 为准。',
  failed: 'failed（失败）：受控执行未完成；可查看事件定位原因。',
  cancelled: 'cancelled（已取消）：本次 Run 未完成。',
  awaiting_revision: 'awaiting_revision（待修订）：需返回编辑并新建 revision。',
  created: 'created（目标已记录）：仅记录已验证目标，没有任何执行；远程执行当前无受控适配器。',
};
const EVIDENCE_CLASS_TEXT: Record<string, string> = {
  real_local_cpu: 'real_local_cpu（真实本机 CPU 实验）',
  controlled_smoke: 'controlled_smoke（受控教学 smoke，不是论文复现）',
  sandboxed_model_code: 'sandboxed_model_code（沙箱内执行模型生成代码（断网、限额、仅标准库）——近似复现，非论文原代码原数据）',
  unclassified: 'unclassified（未分类：非受控 smoke 亦非真实本机 CPU recipe）',
};

const STEP_HINT: Record<RealFlowState['step'], string> = {
  input: '先放入要复现的论文',
  provider: '连接用于抽取的模型',
  extract: '让模型读出实验方案',
  spec: '确认要运行的实验与参数',
  environment: '通过环境检查',
  codegen: '生成并审阅复现代码',
  execute: '运行、查看报告并导出',
};

const STEP_TITLE: Record<RealFlowStep, string> = {
  input: '上传论文',
  provider: '连接模型',
  extract: '结构化抽取',
  spec: '确认实验方案',
  environment: '环境检查',
  codegen: '生成复现代码',
  execute: '执行与产出',
};
const SUPPORT_TONE: Record<string, Tone> = { supported: 'ok', requires_review: 'warn', unsupported: 'idle' };
const EVIDENCE_STATUS_TONE: Record<string, Tone> = { supported: 'ok', inferred: 'warn', missing: 'idle', conflicting: 'bad' };
const ENV_ITEM_TONE: Record<string, Tone> = { pass: 'ok', fail: 'bad', unknown: 'warn', not_needed: 'idle' };
const DECISION_TONE: Record<string, Tone> = { ready: 'ok', blocked: 'warn', unsupported: 'bad' };
const TECH_TITLE = '详情 / 技术细节';

export default function RealFlowPanel({ api }: { api: GuidedApi }) {
  const [controller, setController] = useState<RealFlowController | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [exports, setExports] = useState<GuidedExport[]>([]);
  const [readRev, setReadRev] = useState(0);
  const [, redraw] = useState(0);
  // --- form state (never persisted) ---
  const [consent, setConsent] = useState(false);
  const [pasteText, setPasteText] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [titleHint, setTitleHint] = useState('');
  const [providerForm, setProviderForm] = useState({ baseUrl: '', modelId: '', apiKey: '', endpointAlias: '' });
  const [budget, setBudget] = useState(false);
  // 「稍后选择」 closes the mode dialog for one paper without choosing; needsChoice stays true.
  const [laterFor, setLaterFor] = useState<string | null>(null);
  const [sshForm, setSshForm] = useState({ host: '', port: '22', user: '', workdir: '', authType: 'agent' as 'agent' | 'key_reference', agentId: '', keyReference: '' });
  const card = useRef<HTMLElement>(null);
  useEffect(() => {
    const c = new RealFlowController({ api });
    let live = true;
    const off = c.subscribe(() => { if (live) redraw(x => x + 1); });
    setController(c);
    return () => { live = false; off(); c.dispose(); };
  }, [api]);
  const state = controller?.getSnapshot();
  useEffect(() => { card.current?.focus(); }, [state?.step]);
  useEffect(() => { setReport(null); setExports([]); }, [state?.runId, state?.selectedCandidateId]);
  useEffect(() => {
    if (!controller || !state?.runId) return;
    return pollRunAndReport({ controller, onError: () => setError('状态读取失败；不会自动重试写入，请显式刷新。') });
  }, [controller, state?.runId, state?.report?.build_id, readRev]);
  useEffect(() => {
    if (!state?.runId || state.reportStatus !== 'ready') return;
    return readReport({ api, runId: state.runId, onValue: setReport, onError: () => setError('报告正文读取失败。') });
  }, [api, state?.runId, state?.reportStatus, readRev]);
  useEffect(() => {
    if (!state?.runId || !state.exportId) return;
    return pollExports({ api, runId: state.runId, exportId: state.exportId, onValue: setExports, onError: () => setError('导出状态读取失败，请刷新。') });
  }, [api, state?.runId, state?.exportId, readRev]);
  if (!controller || !state) return <p role="status" className="page-loading">正在初始化真实流程…</p>;
  const call = async (fn: () => unknown | Promise<unknown>) => {
    if (busy) return;
    setBusy(true); setError(null);
    try { await fn(); } catch (e) { setError(e instanceof Error ? e.message : '操作未完成'); }
    finally { setBusy(false); }
  };
  const runStatus = state.run?.status ?? null;
  const runStatusText = !state.runId ? '尚未创建 Run。' : runStatus === null ? '已创建：等待读取执行状态。' : RUN_STATUS_TEXT[runStatus] ?? runStatus;
  const runCompleted = runStatus === 'completed';
  const evidenceClass = report && typeof report.evidence_class === 'string' ? report.evidence_class : null;
  const sshVerified = state.ssh !== null;
  const steps = realFlowSteps(state);
  const rows = stepRows(state);
  const nextStep = nextViewableStep(state);
  const sandboxRecipe = isSandboxRecipeSelected(state);
  // sandbox_python 执行的是模型生成代码：代码未生成前不提供任何创建 Run 的入口。
  const sandboxCodeMissing = sandboxRecipe && state.codegen === null;
  const body = <section key={state.step} ref={card} tabIndex={-1} className="guided-card flow-body" aria-busy={busy || state.busy} aria-label={STEP_TITLE[state.step]}>
    {state.step === 'input' && <>
      <p>选择 PDF / TXT / JSON 文件，或直接粘贴文本。上传后会显示论文的唯一编号与 SHA-256，便于之后核验。</p>
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">文件（.txt / .pdf / .json）{file && <small title={file.name}>已选择：{file.name}</small>}</span>
          <input type="file" accept=".txt,.pdf,.json" disabled={busy} onChange={e => { setFile(e.target.files?.[0] ?? null); setPasteText(''); }} />
        </label>
        <label className="ui-field flow-field-top"><span className="ui-field-label">或粘贴文本<small>与文件二选一</small></span>
          <textarea rows={4} value={pasteText} disabled={busy || file !== null} onChange={e => { setPasteText(e.target.value); if (e.target.value) setFile(null); }} placeholder={file ? '已选择文件，粘贴输入已禁用' : '粘贴论文摘要或全文文本'} />
        </label>
        <label className="ui-field"><span className="ui-field-label">标题提示<small>可选</small></span><input type="text" value={titleHint} disabled={busy} onChange={e => setTitleHint(e.target.value)} /></label>
        <Switch checked={consent} onChange={setConsent}>我同意处理该文章内容（source_processing_consent）</Switch>
      </div>
      <div className="flow-actions">
        <button disabled={busy || (!consent && state.paper === null) || (!file && pasteText.trim() === '' && state.paper === null)} className="btn btn-primary" onClick={() => void call(() => controller.uploadArticle({ consent, file: file ?? undefined, text: pasteText || undefined, titleHint: titleHint || undefined }))}>{state.paper ? '重新上传（新 Paper）' : '上传文章'}</button>
      </div>
      {state.paper && <div className="flow-group">
        <KVList>
          <KV label="status" value={state.paper.status} />
          <KV label="page_count" value={state.paper.page_count ?? 0} />
        </KVList>
        {state.paper.error && <p className="ui-callout tone-bad" role="alert">{state.paper.error.code}: {state.paper.error.message}</p>}
        <ParseProgress docparse={state.docparse} busy={busy || state.busy} onRetry={() => void call(() => controller.retryParse())} onChoose={mode => void call(() => controller.switchParseMode(mode))} onReopenChoice={() => setLaterFor(null)} onRefresh={() => void call(() => controller.pollPaperText())} />
        <Disclosure variant="sub" title={TECH_TITLE}>
          <KVList>
            <KV label="paper_id" value={state.paper.paper_id} copy />
            <KV label="sha256" value={state.paper.sha256} copy />
            <KV label="project" value={state.paper.project_id} copy />
          </KVList>
        </Disclosure>
      </div>}
    </>}
    {state.step === 'provider' && <>
      <p>填写兼容 OpenAI 协议的模型服务。api_key 只保存在当前页面内存，不会落盘，刷新后需要重新输入。</p>
      {state.provider
        ? <div className="flow-group">
          <KVList>
            <KV label="endpoint_alias" value={state.provider.endpointAlias} />
            <KV label="model_id" value={state.provider.modelId} copy />
            <KV label="base_url" value={state.provider.baseUrl} copy />
            <KV label="key 掩码" value={state.provider.secretLast4 || '••••'} />
          </KVList>
          <Disclosure variant="sub" title={TECH_TITLE}>
            <KVList><KV label="provider_session_id" value={state.provider.providerSessionId} copy /></KVList>
          </Disclosure>
          <div className="flow-actions"><button disabled={busy} className="btn btn-secondary" onClick={() => void call(() => controller.clearSession())}>清除会话</button></div>
          <p>会话是后端进程内状态：清除后或后端重启后失效，需重新创建。</p>
        </div>
        : <>
          <div className="ui-form">
            <label className="ui-field"><span className="ui-field-label">base_url<small>必填</small></span><input type="text" value={providerForm.baseUrl} disabled={busy} onChange={e => setProviderForm({ ...providerForm, baseUrl: e.target.value })} placeholder="https://api.example.com/v1" /></label>
            <label className="ui-field"><span className="ui-field-label">model_id<small>必填</small></span><input type="text" value={providerForm.modelId} disabled={busy} onChange={e => setProviderForm({ ...providerForm, modelId: e.target.value })} /></label>
            <label className="ui-field"><span className="ui-field-label">api_key<small>必填，仅内存</small></span><input type="password" value={providerForm.apiKey} disabled={busy} autoComplete="off" onChange={e => setProviderForm({ ...providerForm, apiKey: e.target.value })} /></label>
            <label className="ui-field"><span className="ui-field-label">endpoint_alias<small>可选，缺省自动派生</small></span><input type="text" value={providerForm.endpointAlias} disabled={busy} onChange={e => setProviderForm({ ...providerForm, endpointAlias: e.target.value })} /></label>
          </div>
          <div className="flow-actions"><button disabled={busy || !providerForm.baseUrl || !providerForm.modelId || !providerForm.apiKey} className="btn btn-primary" onClick={() => void call(async () => { await controller.createSession(providerForm); setProviderForm({ ...providerForm, apiKey: '' }); })}>创建会话</button></div>
          <p>也可以先跳过：PDF 会用服务器确定性抽取，纯文本文章则无法抽取。</p>
        </>}
    </>}
    {state.step === 'extract' && <>
      {!state.paper && <p role="alert">需要先上传文章。</p>}
      {state.paper && !canExtract(state) && <p className="ui-callout tone-warn" role="status"><Icon name="alert" /><span>PDF 需要先解析完成才能抽取；解析状态见「上传论文」步骤。</span></p>}
      {state.paper && <div className="flow-actions"><button disabled={busy || !canExtract(state)} className="btn btn-primary" onClick={() => void call(() => controller.runExtraction())}>{state.extractionPhase === 'ready' ? '重新抽取（新记录）' : '开始抽取'}</button></div>}
      {state.extractionPhase === 'loading' && <p role="status">抽取请求进行中…</p>}
      {state.extractionPhase === 'error' && <p className="ui-callout tone-bad" role="alert">抽取失败：{state.error ?? '未知错误'}。可显式重试；写入结果未知时用同 key 重放。</p>}
      {state.extractionPhase === 'ready' && state.extraction && <ExtractionView extraction={state.extraction} selectedId={state.selectedCandidateId} onSelect={key => controller.selectCandidate(key)} />}
      {state.extractionPhase === 'idle' && <p>尚未抽取。抽取会调用上方 Provider 会话（如已创建），请求与响应都经过后端校验。</p>}
    </>}
    {state.step === 'spec' && <>
      {state.extractionPhase !== 'ready' && <p role="alert">需要先完成抽取。</p>}
      {state.extractionPhase === 'ready' && state.extraction && (() => {
        const candidate = state.extraction.candidate_experiments.find(c => candidateKey(c) === state.selectedCandidateId) ?? null;
        return <>
          <p>已选候选：{candidate ? <>{candidate.label || candidate.id}（recipe <code>{candidate.recipe_id}/{candidate.recipe_version}</code>）</> : '未选择'}</p>
          <p>training 参数可留空走 Recipe 默认值；填写即覆盖。参数只会写入受控 spec，不会变成任意命令。</p>
          <div className="ui-form">
            {REALFLOW_TRAINING_FIELDS.map(field => <label key={field} className="ui-field"><span className="ui-field-label">{REALFLOW_TRAINING_LABELS[field]}</span>
              <input type="text" inputMode="decimal" value={state.training[field]} disabled={busy || state.confirmationId !== null} onChange={e => controller.setTraining({ [field]: e.target.value } as Partial<GuidedTrainingForm>)} placeholder="默认" />
            </label>)}
            <Switch checked={budget} onChange={setBudget}>接受服务器预算约束，确认此实验方案</Switch>
          </div>
          <div className="flow-actions"><button disabled={busy || !budget || !state.selectedCandidateId} className="btn btn-primary" onClick={() => void call(() => controller.confirmSpec(budget))}>{state.confirmationId ? '方案已确认' : '创建 Experiment + Revision 并确认'}</button></div>
          {(state.experimentId || state.revisionId || state.confirmationId) && <Disclosure variant="sub" title={TECH_TITLE}>
            <KVList>
              <KV label="experiment" value={state.experimentId} copy />
              <KV label="revision" value={state.revisionId} copy />
              <KV label="confirmation" value={state.confirmationId} copy />
            </KVList>
          </Disclosure>}
        </>;
      })()}
    </>}
    {state.step === 'environment' && <>
      {state.confirmationId === null && <p role="alert">需要先确认实验方案。</p>}
      {state.confirmationId !== null && <>
        <div className="flow-actions"><button disabled={busy} className="btn btn-primary" onClick={() => void call(() => controller.checkEnvironment())}>{state.envCheck ? '重新检查（新记录）' : '触发环境检查'}</button></div>
        {state.envPhase === 'loading' && <p role="status">环境检查进行中…</p>}
        {state.envPhase === 'error' && <p className="ui-callout tone-bad" role="alert">环境检查失败：{state.error ?? '未知错误'}</p>}
        {state.envCheck && <EnvironmentView env={state.envCheck} ssh={state.ssh} />}
        {state.envCheck?.decision === 'blocked' && !sshVerified && <div className="flow-branches">
          <Disclosure variant="card" title="分支 A：改用 SSH（仅验证）" defaultOpen>
            <div className="flow-group">
              <p className="ui-callout tone-warn"><Icon name="alert" /><span>SSH 验证当前是 <strong>fake 边界</strong>：后端只做结构化校验并记录目标，不发起真实 SSH 连接，也不执行任何远程命令。验证成功仅表示 target 已登记为 awaiting_confirmation。</span></p>
              <div className="ui-form">
                <label className="ui-field"><span className="ui-field-label">host</span><input type="text" value={sshForm.host} disabled={busy} onChange={e => setSshForm({ ...sshForm, host: e.target.value })} /></label>
                <label className="ui-field"><span className="ui-field-label">port</span><input type="text" inputMode="numeric" value={sshForm.port} disabled={busy} onChange={e => setSshForm({ ...sshForm, port: e.target.value })} /></label>
                <label className="ui-field"><span className="ui-field-label">user</span><input type="text" value={sshForm.user} disabled={busy} onChange={e => setSshForm({ ...sshForm, user: e.target.value })} /></label>
                <label className="ui-field"><span className="ui-field-label">workdir</span><input type="text" value={sshForm.workdir} disabled={busy} onChange={e => setSshForm({ ...sshForm, workdir: e.target.value })} /></label>
                <label className="ui-field"><span className="ui-field-label">认证方式</span>
                  <select value={sshForm.authType} disabled={busy} onChange={e => setSshForm({ ...sshForm, authType: e.target.value as 'agent' | 'key_reference' })}>
                    <option value="agent">SSH agent id</option>
                    <option value="key_reference">key reference</option>
                  </select>
                </label>
                {sshForm.authType === 'agent'
                  ? <label className="ui-field"><span className="ui-field-label">agent_id</span><input type="text" value={sshForm.agentId} disabled={busy} onChange={e => setSshForm({ ...sshForm, agentId: e.target.value })} /></label>
                  : <label className="ui-field"><span className="ui-field-label">key reference<small>受控引用，非私钥正文</small></span><input type="text" value={sshForm.keyReference} disabled={busy} onChange={e => setSshForm({ ...sshForm, keyReference: e.target.value })} /></label>}
              </div>
              <p>只接受 host/port/user/workdir 与受控 key reference 或 agent id；密码、私钥正文、命令一律拒绝。</p>
              <div className="flow-actions"><button disabled={busy} className="btn btn-primary" onClick={() => void call(() => controller.verifySsh(sshForm))}>验证 SSH target（fake）</button></div>
            </div>
          </Disclosure>
          <Disclosure variant="card" title="分支 B：只保存方案">
            <p>不启动 Run。已确认的 revision 与 confirmation 已保存在服务器（revision <code>{state.revisionId}</code>），之后可重新进入环境检查或改用 SSH。</p>
          </Disclosure>
        </div>}
        {state.envCheck?.decision === 'ready' && <p>decision=ready：可以进入执行步骤创建本机 Run。</p>}
        {state.envCheck?.decision === 'unsupported' && <p role="alert">decision=unsupported：当前候选方案不可执行，回到方案步骤选择 supported 候选。</p>}
      </>}
    </>}
    {state.step === 'codegen' && <>
      {state.confirmationId === null && <p role="alert">需要先确认实验方案。</p>}
      {state.confirmationId !== null && <>
        <p>调用 Provider 为当前实验方案生成可执行的复现代码。代码由模型生成，是对论文的近似复现；生成后可在下方预览完整代码，执行发生在后端受控环境中。</p>
        <div className="flow-actions">
          <button disabled={busy} className="btn btn-primary" onClick={() => void call(() => controller.generateCode())}>{state.codegen ? '重新生成复现代码（新幂等键）' : '生成复现代码'}</button>
          <button disabled={busy || !state.codegen} className="btn btn-secondary" onClick={() => void call(() => controller.readCode())}>读回校验（只读）</button>
        </div>
        {state.codegenPhase === 'loading' && <p role="status">代码生成中…{state.codegen ? '（新结果返回前仍显示当前版本。）' : ''}</p>}
        {state.codegenPhase === 'error' && <p className="ui-callout tone-bad" role="alert">代码生成失败：{state.error ?? '未知错误'}。可显式重试；写入结果未知时先以原 Idempotency-Key 重放。</p>}
        {state.codegenPhase === 'idle' && <p>尚未生成。点击上方按钮调用 Provider 生成复现代码，每次生成都会产生新的 attempt 记录。</p>}
        {state.codegen && <div className="flow-group">
          <KVList>
            <KV label="attempt" value={`${state.codegen.attemptId}（index ${state.codegen.attemptIndex}）`} copy={state.codegen.attemptId} />
            <KV label="code_sha256" value={state.codegen.codeSha256} copy />
            {state.codegen.notes && <KV label="notes" mono={false} value={state.codegen.notes} />}
          </KVList>
          <Disclosure variant="sub" title="生成的复现代码" defaultOpen count={`${state.codegen.code.split('\n').length} 行`} meta={<CopyButton value={state.codegen.code} label="复制代码" />}>
            <pre className="ui-code" aria-label="生成的复现代码全文（只读）">{state.codegen.code}</pre>
          </Disclosure>
          <p className="flow-note">只读展示；代码执行由后端沙箱负责，前端不运行任何代码。</p>
        </div>}
      </>}
    </>}
    {state.step === 'execute' && <>
      {sandboxCodeMissing && <p className="ui-callout tone-warn" role="alert">当前候选 recipe 为 sandbox_python，需先在「复现代码」步骤生成代码后才能创建 Run。</p>}
      {state.envCheck?.decision === 'ready' && <div className="flow-actions"><button disabled={busy || sandboxCodeMissing} className="btn btn-primary" onClick={() => void call(() => controller.startRun('local-cpu'))}>{state.runId ? '刷新 Run 状态' : '启动本机 Run（local-cpu）'}</button></div>}
      {state.envCheck?.decision === 'blocked' && sshVerified && <div className="flow-actions"><button disabled={busy || sandboxCodeMissing} className="btn btn-primary" onClick={() => void call(() => controller.startRun('ssh'))}>{state.runId ? '刷新 Run 状态' : '创建 SSH Run（仅登记已验证目标）'}</button></div>}
      {state.envCheck?.decision === 'blocked' && !sshVerified && <p className="ui-callout tone-warn" role="alert">环境 blocked：未完成 SSH 验证前不提供执行入口。</p>}
      <p role="status" className="flow-status">{runStatusText}</p>
      {state.ssh && <p className="ui-callout tone-warn"><Icon name="alert" /><span>SSH Run 说明：远程执行当前没有受控适配器，Run 只会登记已验证目标（status=created），不会执行任何远程命令。</span></p>}
      <Disclosure variant="sub" title="事件日志" count={state.events.length}>
        <pre className="ui-code">{state.events.map(e => `${e.seq} · ${e.type} · ${JSON.stringify(e.payload)}`).join('\n') || '尚无事件'}</pre>
      </Disclosure>
      {runCompleted && <>
        <div className="flow-group">
          <h3 className="flow-sub">报告</h3>
          <p role="status">状态：{state.reportStatus ?? '未构建'}</p>
          <div className="flow-actions"><button disabled={busy || ['pending', 'generating', 'ready'].includes(state.reportStatus ?? '')} className="btn btn-primary" onClick={() => void call(() => controller.buildReport())}>{state.reportStatus === 'failed' ? '显式重试报告' : '构建报告'}</button></div>
          {evidenceClass && <p className="ui-callout tone-accent flow-evidence">evidence_class：<strong>{EVIDENCE_CLASS_TEXT[evidenceClass] ?? evidenceClass}</strong></p>}
          {report && <Disclosure variant="sub" title="报告原始 JSON" meta={<CopyButton value={JSON.stringify(report, null, 2)} label="复制报告 JSON" />}><pre className="ui-code">{JSON.stringify(report, null, 2)}</pre></Disclosure>}
        </div>
        {state.reportStatus === 'ready' && <div className="flow-group">
          <h3 className="flow-sub">导出</h3>
          <p>下载前核对 MIME、字节数与 SHA-256。</p>
          <div className="flow-actions"><button disabled={busy} className="btn btn-primary" onClick={() => void call(() => controller.createExports())}>创建导出</button></div>
          {exports.map(bundle => <div key={bundle.id} className="flow-group">
            <p>{bundle.status} · <code>{bundle.id}</code></p>
            <ul className="ui-list">{bundle.items.map(item => <li key={item.format} className="ui-row">
              <span className="ui-row-main"><span className="ui-row-title ui-mono">{item.format}</span><span className="ui-row-sub">{item.status}{item.error && <> · {item.error.code}: {item.error.message}</>}</span></span>
              {item.status === 'ready' && item.artifact_id && item.filename && item.bytes !== null && item.sha256 && item.media_type && <span className="ui-row-end"><button className="btn btn-secondary btn-sm" disabled={busy} onClick={() => void call(() => api.download({ artifact_id: item.artifact_id!, filename: item.filename!, bytes: item.bytes!, sha256: item.sha256!, media_type: item.media_type! }))}><Icon name="download" size={14} />校验并下载（{item.bytes} bytes）</button></span>}
            </li>)}</ul>
          </div>)}
        </div>}
      </>}
    </>}
    {(error || state.error) && <p className="ui-callout tone-bad" role="alert">{error ?? state.error}</p>}
    {(error?.includes('UNCONFIRMED') || state.error?.includes('WRITE_UNCONFIRMED') || controller.hasRetry()) && <div className="flow-actions"><button disabled={busy} className="btn btn-secondary" onClick={() => void call(() => controller.retryWrite())}>先以原 Idempotency-Key 与请求体重放，再继续其他写入</button></div>}
    {nextStep && <div className="flow-next"><button disabled={busy} className="btn btn-secondary" onClick={() => controller.viewStep(nextStep)}>下一步：{STEP_TITLE[nextStep]}<Icon name="chevron-right" size={14} /></button></div>}
  </section>;
  return <section className="flow">
    <header className="flow-intro">
      <h1>新建复现任务</h1>
      <p>跟随下面的步骤，从上传论文一路走到可核验的复现报告。</p>
    </header>
    <ol className="flow-stack" aria-label="真实流程步骤">
      {rows.map((row, i) => row.step === state.step
        ? <li key={row.step} className="flow-step is-current" aria-current="step">
          <div className="flow-head">
            <StepMarker row={row} index={i} />
            <span className="flow-head-text"><b>{STEP_TITLE[row.step]}</b><small>第 {i + 1} 步 / 共 {steps.length} 步 · {STEP_HINT[row.step]}</small></span>
          </div>
          {body}
        </li>
        : <li key={row.step} className={`flow-step is-${row.mark}`}>
          <button type="button" className="flow-head" aria-expanded={false} disabled={busy || !row.viewable} onClick={() => controller.viewStep(row.step)}>
            <StepMarker row={row} index={i} />
            <span className="flow-head-text"><b>{STEP_TITLE[row.step]}</b>{row.summary && <small title={row.summary}>{row.summary}</small>}</span>
            {row.viewable && <Icon name="chevron-right" size={14} className="flow-chev" />}
          </button>
        </li>)}
    </ol>
    <footer className="guided-footer">
      <button disabled={busy} className="btn btn-ghost" onClick={() => void call(async () => { try { await controller.pollRun(); await controller.pollReport(); } finally { setReadRev(x => x + 1); } })}><Icon name="refresh" size={14} />刷新（只读）</button>
      {state.note && <span role="note">{state.note}</span>}
    </footer>
    {state.docparse.needsChoice && laterFor !== (state.paper?.paper_id ?? null) && <ModeDialog status={state.docparse.status} busy={busy || state.busy} onChoose={mode => void call(() => controller.chooseParseMode(mode))} onLater={() => setLaterFor(state.paper?.paper_id ?? '')} />}
  </section>;
}

function StepMarker({ row, index }: { row: StepRow; index: number }) {
  if (row.mark === 'done') return <span className="flow-mark is-done"><Icon name="check" size={13} /><span className="sr-only">已完成</span></span>;
  if (row.mark === 'attention') return <span className="flow-mark is-attention"><Icon name="alert" size={13} /><span className="sr-only">需要注意</span></span>;
  return <span className="flow-mark" aria-hidden="true">{index + 1}</span>;
}

function ExtractionView({ extraction, selectedId, onSelect }: { extraction: NonNullable<RealFlowState['extraction']>; selectedId: string | null; onSelect: (key: string) => void }) {
  const fields = Object.entries(extraction.fields);
  return <>
    <div className="flow-group">
      <h3 className="flow-sub">候选实验（来自抽取结果，未硬编码）</h3>
      {extraction.candidate_experiments.length === 0 && <p>本次抽取没有返回候选实验。</p>}
      {extraction.candidate_experiments.length > 0 && <ul className="ui-list flow-candidates">
        {extraction.candidate_experiments.map(candidate => {
          const key = candidateKey(candidate);
          const blocked = candidateBlockedReason(candidate);
          return <li key={key}>
            <label className={`ui-row flow-candidate${blocked ? ' is-disabled' : ''}`}>
              <input type="radio" name="guided-real-candidate" disabled={blocked !== null} checked={selectedId === key} onChange={() => onSelect(key)} />
              <span className="ui-row-main">
                <span className="flow-candidate-title"><b>{candidate.label || candidate.id}</b><Badge tone={SUPPORT_TONE[candidate.execution_support] ?? 'idle'}>{SUPPORT_BADGE[candidate.execution_support] ?? candidate.execution_support}</Badge></span>
                <span className="ui-row-sub">recipe <code>{candidate.recipe_id ?? '—'}/{candidate.recipe_version ?? '—'}</code></span>
                {blocked && <span className="ui-row-sub">{blocked === 'REVIEW_REQUIRED_CANDIDATE' ? '需人工复核来源，不能直接创建受控实验。' : '该候选当前不可执行。'}{candidate.unsupported_reasons.length > 0 ? `（${candidate.unsupported_reasons.join('；')}）` : ''}</span>}
                {candidate.parameters && Object.keys(candidate.parameters).length > 0 && <span className="ui-row-sub">参数：{Object.entries(candidate.parameters).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')}</span>}
                {candidate.required_dependencies.length > 0 && <span className="ui-row-sub">依赖：{candidate.required_dependencies.map(d => JSON.stringify(d)).join(', ')}</span>}
                {candidate.source_refs.length > 0 && <span className="ui-row-sub">引用 {candidate.source_refs.length} 条</span>}
              </span>
            </label>
          </li>;
        })}
      </ul>}
    </div>
    {extraction.warnings.length > 0 && <p className="ui-callout tone-warn"><Icon name="alert" /><span>warnings：{extraction.warnings.join('；')}</span></p>}
    <div className="flow-nested">
      <Disclosure title="字段与证据状态" count={fields.length} defaultOpen>
        <ul className="ui-list">
          {fields.map(([name, field]) => <li key={name} className="ui-row">
            <span className="ui-row-main"><span className="ui-row-title ui-mono">{name}</span><span className="ui-row-sub">{field.value === null || field.value === undefined ? '（空）' : String(field.value)}</span></span>
            <span className="ui-row-end"><Badge tone={EVIDENCE_STATUS_TONE[field.evidence_status ?? ''] ?? 'idle'}>{EVIDENCE_STATUS_TEXT[field.evidence_status ?? ''] ?? field.evidence_status ?? 'unknown'}</Badge><span>refs: {field.source_refs.length}</span></span>
          </li>)}
        </ul>
      </Disclosure>
      {extraction.source_refs.length > 0 && <Disclosure title="来源引用（source refs）" count={extraction.source_refs.length}>
        <ul className="ui-list">{extraction.source_refs.map((ref, i) => <li key={i} className="ui-row"><span className="ui-row-main"><span className="ui-row-sub">page {ref.page ?? '—'} · sha256 <code>{ref.pdf_sha256 ? `${ref.pdf_sha256.slice(0, 12)}…` : '—'}</code>{ref.quote ? ` · “${ref.quote.slice(0, 80)}${ref.quote.length > 80 ? '…' : ''}”` : ''}</span></span></li>)}</ul>
      </Disclosure>}
      <Disclosure variant="sub" title={TECH_TITLE}>
        <KVList>
          <KV label="extraction_id" value={extraction.extraction_id} copy />
          <KV label="schema" value={extraction.schema_version} />
        </KVList>
      </Disclosure>
    </div>
  </>;
}

function EnvironmentView({ env, ssh }: { env: NonNullable<RealFlowState['envCheck']>; ssh: RealFlowState['ssh'] }) {
  return <>
    <p>decision：<StatusWord tone={DECISION_TONE[env.decision] ?? 'idle'}>{env.decision}</StatusWord></p>
    <ul className="ui-list">
      {env.items.map(item => {
        const detail = [item.required ? 'required' : null, item.observed !== null && item.observed !== undefined ? `observed=${JSON.stringify(item.observed)}` : null, item.required_value !== null && item.required_value !== undefined ? `required=${JSON.stringify(item.required_value)}` : null, item.suggestion ? `建议：${item.suggestion}` : null].filter(Boolean).join(' · ');
        return <li key={item.key} className="ui-row">
          <span className="ui-row-main"><span className="ui-row-title ui-mono">{item.key}</span>{detail && <span className="ui-row-sub">{detail}</span>}</span>
          <span className="ui-row-end"><Badge tone={ENV_ITEM_TONE[item.status] ?? 'idle'}>{ENV_ITEM_TEXT[item.status] ?? item.status}</Badge></span>
        </li>;
      })}
    </ul>
    {env.repair_suggestions.length > 0 && <p className="flow-note">修复建议（检查器只说明，不代执行）：{env.repair_suggestions.join('；')}</p>}
    <Disclosure variant="sub" title={TECH_TITLE}>
      <KVList>
        <KV label="check" value={env.environment_check_id} copy />
        <KV label="checker" value={env.checker_version} />
      </KVList>
    </Disclosure>
    {ssh && <div className="flow-group">
      <KVList>
        <KV label="status" value={ssh.status} />
        <KV label="target（安全投影）" value={JSON.stringify(ssh.target)} />
      </KVList>
      <p className="ui-callout tone-warn"><Icon name="alert" /><span>如前所述：这是 fake 验证边界，不代表远程可用。</span></p>
      <Disclosure variant="sub" title={TECH_TITLE}>
        <KVList><KV label="ssh_verification_id" value={ssh.ssh_verification_id} copy /></KVList>
      </Disclosure>
    </div>}
  </>;
}
