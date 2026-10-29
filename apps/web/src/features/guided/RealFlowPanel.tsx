import { useEffect, useRef, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import type { GuidedExport, GuidedTrainingForm } from '../../api-client/guided-types';
import { RealFlowController } from './realflow';
import { canRealViewStep, candidateBlockedReason, candidateKey, isSandboxRecipeSelected, realFlowSteps, REALFLOW_STEP_LABELS, REALFLOW_TRAINING_FIELDS, REALFLOW_TRAINING_LABELS, type RealFlowState } from './realflow-state';
import { pollExports, pollRunAndReport, readReport } from './readback';

// Real article flow panel: 文章输入 → Provider → 抽取 → 方案 → 环境检查 → 执行。
// sandbox_python 候选在环境与执行之间多出「复现代码」步骤（生成复现代码 → 沙箱执行）。
// It shares the honest-wording rules of the guided demo: nothing here may
// present a fake/unverified result as real, and the provider api_key only
// lives in React state (never localStorage).

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
  if (!controller || !state) return <p role="status">正在初始化真实流程…</p>;
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
  const sandboxRecipe = isSandboxRecipeSelected(state);
  // sandbox_python 执行的是模型生成代码：代码未生成前不提供任何创建 Run 的入口。
  const sandboxCodeMissing = sandboxRecipe && state.codegen === null;
  return <section className="guided-realflow">
    <div className="guided-progress" aria-hidden="true"><i style={{ width: `${((steps.findIndex(s => s === state.step) + 1) / steps.length) * 100}%` }} /></div>
    <nav className="guided-steps" aria-label="真实流程步骤">{steps.map((step, i) => <button key={step} aria-current={state.step === step ? 'step' : undefined} className={state.step === step ? 'active' : ''} disabled={busy || !canRealViewStep(state, step)} onClick={() => controller.viewStep(step)}><b>{i + 1}</b>{REALFLOW_STEP_LABELS[step]}</button>)}</nav>
    <section key={state.step} ref={card} tabIndex={-1} className="guided-card" aria-busy={busy || state.busy}>
      <span className="guided-kicker">第 {steps.findIndex(s => s === state.step) + 1} 步 / 共 {steps.length} 步 · {STEP_HINT[state.step]}</span>
      {state.step === 'input' && <>
        <h2>上传论文</h2>
        <p>选择 PDF / TXT / JSON 文件，或直接粘贴文本。上传后会显示论文的唯一编号与 SHA-256，便于之后核验。</p>
        <label className="guided-field">文件（.txt / .pdf / .json）
          <input type="file" accept=".txt,.pdf,.json" disabled={busy} onChange={e => { setFile(e.target.files?.[0] ?? null); setPasteText(''); }} />
        </label>
        <label className="guided-field">或粘贴文本（与文件二选一）
          <textarea rows={4} value={pasteText} disabled={busy || file !== null} onChange={e => { setPasteText(e.target.value); if (e.target.value) setFile(null); }} placeholder={file ? '已选择文件，粘贴输入已禁用' : '粘贴论文摘要或全文文本'} />
        </label>
        <label className="guided-field">标题提示（可选）<input type="text" value={titleHint} disabled={busy} onChange={e => setTitleHint(e.target.value)} /></label>
        <label className="guided-check"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> 我同意处理该文章内容（source_processing_consent）</label>
        <button disabled={busy || (!consent && state.paper === null) || (!file && pasteText.trim() === '' && state.paper === null)} className="guided-primary" onClick={() => void call(() => controller.uploadArticle({ consent, file: file ?? undefined, text: pasteText || undefined, titleHint: titleHint || undefined }))}>{state.paper ? '重新上传（新 Paper）' : '上传文章'}</button>
        {state.paper && <div className="guided-paper-card">
          <p>paper_id：<code>{state.paper.paper_id}</code></p>
          <p>sha256：<code>{state.paper.sha256}</code></p>
          <p>status：{state.paper.status} · page_count：{state.paper.page_count ?? 0} · project：<code>{state.paper.project_id}</code></p>
          {state.paper.error && <p className="guided-error" role="alert">{state.paper.error.code}: {state.paper.error.message}</p>}
        </div>}
      </>}
      {state.step === 'provider' && <>
        <h2>连接模型</h2>
        <p>填写兼容 OpenAI 协议的模型服务。api_key 只保存在当前页面内存，不会落盘，刷新后需要重新输入。</p>
        {state.provider
          ? <div className="guided-paper-card">
            <p>provider_session_id：<code>{state.provider.providerSessionId}</code></p>
            <p>endpoint_alias：{state.provider.endpointAlias} · model_id：<code>{state.provider.modelId}</code></p>
            <p>base_url：<code>{state.provider.baseUrl}</code> · key 掩码：<code>{state.provider.secretLast4 || '••••'}</code></p>
            <button disabled={busy} className="guided-secondary" onClick={() => void call(() => controller.clearSession())}>清除会话</button>
            <p>会话是后端进程内状态：清除后或后端重启后失效，需重新创建。</p>
          </div>
          : <div className="guided-form-grid">
            <label className="guided-field">base_url（必填）<input type="text" value={providerForm.baseUrl} disabled={busy} onChange={e => setProviderForm({ ...providerForm, baseUrl: e.target.value })} placeholder="https://api.example.com/v1" /></label>
            <label className="guided-field">model_id（必填）<input type="text" value={providerForm.modelId} disabled={busy} onChange={e => setProviderForm({ ...providerForm, modelId: e.target.value })} /></label>
            <label className="guided-field">api_key（必填，仅内存）<input type="password" value={providerForm.apiKey} disabled={busy} autoComplete="off" onChange={e => setProviderForm({ ...providerForm, apiKey: e.target.value })} /></label>
            <label className="guided-field">endpoint_alias（可选，缺省自动派生）<input type="text" value={providerForm.endpointAlias} disabled={busy} onChange={e => setProviderForm({ ...providerForm, endpointAlias: e.target.value })} /></label>
            <button disabled={busy || !providerForm.baseUrl || !providerForm.modelId || !providerForm.apiKey} className="guided-primary" onClick={() => void call(async () => { await controller.createSession(providerForm); setProviderForm({ ...providerForm, apiKey: '' }); })}>创建会话</button>
            <p>也可以先跳过：PDF 会用服务器确定性抽取，纯文本文章则无法抽取。</p>
          </div>}
      </>}
      {state.step === 'extract' && <>
        <h2>结构化抽取</h2>
        {!state.paper && <p role="alert">需要先上传文章。</p>}
        {state.paper && <button disabled={busy} className="guided-primary" onClick={() => void call(() => controller.runExtraction())}>{state.extractionPhase === 'ready' ? '重新抽取（新记录）' : '开始抽取'}</button>}
        {state.extractionPhase === 'loading' && <p role="status">抽取请求进行中…</p>}
        {state.extractionPhase === 'error' && <p className="guided-error" role="alert">抽取失败：{state.error ?? '未知错误'}。可显式重试；写入结果未知时用同 key 重放。</p>}
        {state.extractionPhase === 'ready' && state.extraction && <ExtractionView extraction={state.extraction} selectedId={state.selectedCandidateId} onSelect={key => controller.selectCandidate(key)} />}
        {state.extractionPhase === 'idle' && <p>尚未抽取。抽取会调用上方 Provider 会话（如已创建），请求与响应都经过后端校验。</p>}
      </>}
      {state.step === 'spec' && <>
        <h2>确认实验方案</h2>
        {state.extractionPhase !== 'ready' && <p role="alert">需要先完成抽取。</p>}
        {state.extractionPhase === 'ready' && state.extraction && (() => {
          const candidate = state.extraction.candidate_experiments.find(c => candidateKey(c) === state.selectedCandidateId) ?? null;
          return <>
            <p>已选候选：{candidate ? <>{candidate.label || candidate.id}（recipe <code>{candidate.recipe_id}/{candidate.recipe_version}</code>）</> : '未选择'}</p>
            <p>training 参数可留空走 Recipe 默认值；填写即覆盖。参数只会写入受控 spec，不会变成任意命令。</p>
            <div className="guided-form-grid">
              {REALFLOW_TRAINING_FIELDS.map(field => <label key={field} className="guided-field">{REALFLOW_TRAINING_LABELS[field]}
                <input type="text" inputMode="decimal" value={state.training[field]} disabled={busy || state.confirmationId !== null} onChange={e => controller.setTraining({ [field]: e.target.value } as Partial<GuidedTrainingForm>)} placeholder="默认" />
              </label>)}
            </div>
            <label className="guided-check"><input type="checkbox" checked={budget} onChange={e => setBudget(e.target.checked)} /> 接受服务器预算约束，确认此实验方案</label>
            <button disabled={busy || !budget || !state.selectedCandidateId} className="guided-primary" onClick={() => void call(() => controller.confirmSpec(budget))}>{state.confirmationId ? '方案已确认' : '创建 Experiment + Revision 并确认'}</button>
            {(state.experimentId || state.revisionId || state.confirmationId) && <p className="guided-paper-card">experiment <code>{state.experimentId}</code> · revision <code>{state.revisionId}</code> · confirmation <code>{state.confirmationId}</code></p>}
          </>;
        })()}
      </>}
      {state.step === 'environment' && <>
        <h2>环境检查</h2>
        {state.confirmationId === null && <p role="alert">需要先确认实验方案。</p>}
        {state.confirmationId !== null && <>
          <button disabled={busy} className="guided-primary" onClick={() => void call(() => controller.checkEnvironment())}>{state.envCheck ? '重新检查（新记录）' : '触发环境检查'}</button>
          {state.envPhase === 'loading' && <p role="status">环境检查进行中…</p>}
          {state.envPhase === 'error' && <p className="guided-error" role="alert">环境检查失败：{state.error ?? '未知错误'}</p>}
          {state.envCheck && <EnvironmentView env={state.envCheck} ssh={state.ssh} />}
          {state.envCheck?.decision === 'blocked' && !sshVerified && <div className="guided-branch">
            <h3>分支 A：改用 SSH（仅验证）</h3>
            <p>SSH 验证当前是 <strong>fake 边界</strong>：后端只做结构化校验并记录目标，不发起真实 SSH 连接，也不执行任何远程命令。验证成功仅表示 target 已登记为 awaiting_confirmation。</p>
            <div className="guided-form-grid">
              <label className="guided-field">host<input type="text" value={sshForm.host} disabled={busy} onChange={e => setSshForm({ ...sshForm, host: e.target.value })} /></label>
              <label className="guided-field">port<input type="text" inputMode="numeric" value={sshForm.port} disabled={busy} onChange={e => setSshForm({ ...sshForm, port: e.target.value })} /></label>
              <label className="guided-field">user<input type="text" value={sshForm.user} disabled={busy} onChange={e => setSshForm({ ...sshForm, user: e.target.value })} /></label>
              <label className="guided-field">workdir<input type="text" value={sshForm.workdir} disabled={busy} onChange={e => setSshForm({ ...sshForm, workdir: e.target.value })} /></label>
              <label className="guided-field">认证方式
                <select value={sshForm.authType} disabled={busy} onChange={e => setSshForm({ ...sshForm, authType: e.target.value as 'agent' | 'key_reference' })}>
                  <option value="agent">SSH agent id</option>
                  <option value="key_reference">key reference</option>
                </select>
              </label>
              {sshForm.authType === 'agent'
                ? <label className="guided-field">agent_id<input type="text" value={sshForm.agentId} disabled={busy} onChange={e => setSshForm({ ...sshForm, agentId: e.target.value })} /></label>
                : <label className="guided-field">key reference（受控引用，非私钥正文）<input type="text" value={sshForm.keyReference} disabled={busy} onChange={e => setSshForm({ ...sshForm, keyReference: e.target.value })} /></label>}
            </div>
            <p>只接受 host/port/user/workdir 与受控 key reference 或 agent id；密码、私钥正文、命令一律拒绝。</p>
            <button disabled={busy} className="guided-primary" onClick={() => void call(() => controller.verifySsh(sshForm))}>验证 SSH target（fake）</button>
            <h3>分支 B：只保存方案</h3>
            <p>不启动 Run。已确认的 revision 与 confirmation 已保存在服务器（revision <code>{state.revisionId}</code>），之后可重新进入环境检查或改用 SSH。</p>
          </div>}
          {state.envCheck?.decision === 'ready' && <p>decision=ready：可以进入执行步骤创建本机 Run。</p>}
          {state.envCheck?.decision === 'unsupported' && <p role="alert">decision=unsupported：当前候选方案不可执行，回到方案步骤选择 supported 候选。</p>}
        </>}
      </>}
      {state.step === 'codegen' && <>
        <h2>生成复现代码</h2>
        {state.confirmationId === null && <p role="alert">需要先确认实验方案。</p>}
        {state.confirmationId !== null && <>
          <p>调用 Provider 为当前实验方案生成可执行的复现代码。代码由模型生成，是对论文的近似复现；生成后可在下方预览完整代码，执行发生在后端受控环境中。</p>
          <button disabled={busy} className="guided-primary" onClick={() => void call(() => controller.generateCode())}>{state.codegen ? '重新生成复现代码（新幂等键）' : '生成复现代码'}</button>
          <button disabled={busy || !state.codegen} className="guided-secondary" onClick={() => void call(() => controller.readCode())}>读回校验（只读）</button>
          {state.codegenPhase === 'loading' && <p role="status">代码生成中…{state.codegen ? '（新结果返回前仍显示当前版本。）' : ''}</p>}
          {state.codegenPhase === 'error' && <p className="guided-error" role="alert">代码生成失败：{state.error ?? '未知错误'}。可显式重试；写入结果未知时先以原 Idempotency-Key 重放。</p>}
          {state.codegenPhase === 'idle' && <p>尚未生成。点击上方按钮调用 Provider 生成复现代码，每次生成都会产生新的 attempt 记录。</p>}
          {state.codegen && <div className="guided-paper-card">
            <p>attempt：<code>{state.codegen.attemptId}</code>（index {state.codegen.attemptIndex}）</p>
            <p>code_sha256：<code>{state.codegen.codeSha256}</code></p>
            {state.codegen.notes && <p>notes：{state.codegen.notes}</p>}
            <pre aria-label="生成的复现代码全文（只读）">{state.codegen.code}</pre>
            <p className="guided-note">只读展示；代码执行由后端沙箱负责，前端不运行任何代码。</p>
          </div>}
        </>}
      </>}
      {state.step === 'execute' && <>
        <h2>执行与产出</h2>
        {sandboxCodeMissing && <p role="alert">当前候选 recipe 为 sandbox_python，需先在「复现代码」步骤生成代码后才能创建 Run。</p>}
        {state.envCheck?.decision === 'ready' && <button disabled={busy || sandboxCodeMissing} className="guided-primary" onClick={() => void call(() => controller.startRun('local-cpu'))}>{state.runId ? '刷新 Run 状态' : '启动本机 Run（local-cpu）'}</button>}
        {state.envCheck?.decision === 'blocked' && sshVerified && <button disabled={busy || sandboxCodeMissing} className="guided-primary" onClick={() => void call(() => controller.startRun('ssh'))}>{state.runId ? '刷新 Run 状态' : '创建 SSH Run（仅登记已验证目标）'}</button>}
        {state.envCheck?.decision === 'blocked' && !sshVerified && <p role="alert">环境 blocked：未完成 SSH 验证前不提供执行入口。</p>}
        <p role="status">{runStatusText}</p>
        {state.ssh && <p>SSH Run 说明：远程执行当前没有受控适配器，Run 只会登记已验证目标（status=created），不会执行任何远程命令。</p>}
        <pre>{state.events.map(e => `${e.seq} · ${e.type} · ${JSON.stringify(e.payload)}`).join('\n') || '尚无事件'}</pre>
        {runCompleted && <>
          <h3>报告</h3>
          <p role="status">状态：{state.reportStatus ?? '未构建'}</p>
          <button disabled={busy || ['pending', 'generating', 'ready'].includes(state.reportStatus ?? '')} className="guided-primary" onClick={() => void call(() => controller.buildReport())}>{state.reportStatus === 'failed' ? '显式重试报告' : '构建报告'}</button>
          {evidenceClass && <p className="guided-evidence">evidence_class：<strong>{EVIDENCE_CLASS_TEXT[evidenceClass] ?? evidenceClass}</strong></p>}
          {report && <pre>{JSON.stringify(report, null, 2)}</pre>}
          {state.reportStatus === 'ready' && <>
            <h3>导出</h3>
            <p>下载前核对 MIME、字节数与 SHA-256。</p>
            <button disabled={busy} className="guided-primary" onClick={() => void call(() => controller.createExports())}>创建导出</button>
            {exports.map(bundle => <div key={bundle.id}><p>{bundle.status} · <code>{bundle.id}</code></p><ul>{bundle.items.map(item => <li key={item.format}>{item.format} — {item.status}{item.error && <p>{item.error.code}: {item.error.message}</p>}{item.status === 'ready' && item.artifact_id && item.filename && item.bytes !== null && item.sha256 && item.media_type && <button className="guided-secondary" disabled={busy} onClick={() => void call(() => api.download({ artifact_id: item.artifact_id!, filename: item.filename!, bytes: item.bytes!, sha256: item.sha256!, media_type: item.media_type! }))}>校验并下载（{item.bytes} bytes）</button>}</li>)}</ul></div>)}
          </>}
        </>}
      </>}
      {(error || state.error) && <p className="guided-error" role="alert">{error ?? state.error}</p>}
      {(error?.includes('UNCONFIRMED') || state.error?.includes('WRITE_UNCONFIRMED') || controller.hasRetry()) && <button disabled={busy} className="guided-secondary" onClick={() => void call(() => controller.retryWrite())}>先以原 Idempotency-Key 与请求体重放，再继续其他写入</button>}
    </section>
    <footer className="guided-footer">
      <button disabled={busy} className="guided-link" onClick={() => void call(async () => { try { await controller.pollRun(); await controller.pollReport(); } finally { setReadRev(x => x + 1); } })}>刷新（只读）</button>
      {state.note && <span role="note">{state.note}</span>}
    </footer>
  </section>;
}

function ExtractionView({ extraction, selectedId, onSelect }: { extraction: NonNullable<RealFlowState['extraction']>; selectedId: string | null; onSelect: (key: string) => void }) {
  return <>
    <p>extraction_id：<code>{extraction.extraction_id}</code> · schema {extraction.schema_version}</p>
    <h3>字段与证据状态</h3>
    <ul className="guided-itemlist">
      {Object.entries(extraction.fields).map(([name, field]) => <li key={name}><b>{name}</b>：{field.value === null || field.value === undefined ? '（空）' : String(field.value)} — <span className="guided-badge">{EVIDENCE_STATUS_TEXT[field.evidence_status ?? ''] ?? field.evidence_status ?? 'unknown'}</span>（refs: {field.source_refs.length}）</li>)}
    </ul>
    {extraction.source_refs.length > 0 && <>
      <h3>来源引用（source refs）</h3>
      <ul className="guided-itemlist">{extraction.source_refs.map((ref, i) => <li key={i}>page {ref.page ?? '—'} · sha256 <code>{ref.pdf_sha256 ? `${ref.pdf_sha256.slice(0, 12)}…` : '—'}</code>{ref.quote ? ` · “${ref.quote.slice(0, 80)}${ref.quote.length > 80 ? '…' : ''}”` : ''}</li>)}</ul>
    </>}
    {extraction.warnings.length > 0 && <p className="guided-note">warnings：{extraction.warnings.join('；')}</p>}
    <h3>候选实验（来自抽取结果，未硬编码）</h3>
    {extraction.candidate_experiments.length === 0 && <p>本次抽取没有返回候选实验。</p>}
    <ul className="guided-candidates">
      {extraction.candidate_experiments.map(candidate => {
        const key = candidateKey(candidate);
        const blocked = candidateBlockedReason(candidate);
        return <li key={key}>
          <label className={blocked ? 'guided-candidate is-disabled' : 'guided-candidate'}>
            <input type="radio" name="guided-real-candidate" disabled={blocked !== null} checked={selectedId === key} onChange={() => onSelect(key)} />
            <span>
              <b>{candidate.label || candidate.id}</b> — recipe <code>{candidate.recipe_id ?? '—'}/{candidate.recipe_version ?? '—'}</code>{' '}
              <span className={`guided-badge guided-badge-${candidate.execution_support}`}>{SUPPORT_BADGE[candidate.execution_support] ?? candidate.execution_support}</span>
              {blocked && <span className="guided-note"> {blocked === 'REVIEW_REQUIRED_CANDIDATE' ? '需人工复核来源，不能直接创建受控实验。' : '该候选当前不可执行。'}{candidate.unsupported_reasons.length > 0 ? `（${candidate.unsupported_reasons.join('；')}）` : ''}</span>}
              {candidate.parameters && Object.keys(candidate.parameters).length > 0 && <span className="guided-note">参数：{Object.entries(candidate.parameters).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')}</span>}
              {candidate.required_dependencies.length > 0 && <span className="guided-note">依赖：{candidate.required_dependencies.map(d => JSON.stringify(d)).join(', ')}</span>}
              {candidate.source_refs.length > 0 && <span className="guided-note">引用 {candidate.source_refs.length} 条</span>}
            </span>
          </label>
        </li>;
      })}
    </ul>
  </>;
}

function EnvironmentView({ env, ssh }: { env: NonNullable<RealFlowState['envCheck']>; ssh: RealFlowState['ssh'] }) {
  return <>
    <p>decision：<strong>{env.decision}</strong>（checker {env.checker_version} · check <code>{env.environment_check_id}</code>）</p>
    <ul className="guided-itemlist">
      {env.items.map(item => <li key={item.key}><b>{item.key}</b> — <span className={`guided-badge guided-env-${item.status}`}>{ENV_ITEM_TEXT[item.status] ?? item.status}</span>{item.required ? '（required）' : ''}{item.observed !== null && item.observed !== undefined ? ` · observed=${JSON.stringify(item.observed)}` : ''}{item.required_value !== null && item.required_value !== undefined ? ` · required=${JSON.stringify(item.required_value)}` : ''}{item.suggestion ? ` · 建议：${item.suggestion}` : ''}</li>)}
    </ul>
    {env.repair_suggestions.length > 0 && <p className="guided-note">修复建议（检查器只说明，不代执行）：{env.repair_suggestions.join('；')}</p>}
    {ssh && <div className="guided-paper-card">
      <p>ssh_verification_id：<code>{ssh.ssh_verification_id}</code> · status：{ssh.status}</p>
      <p>target（安全投影）：{JSON.stringify(ssh.target)}</p>
      <p className="guided-note">如前所述：这是 fake 验证边界，不代表远程可用。</p>
    </div>}
  </>;
}
