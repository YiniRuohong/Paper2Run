import { canRealViewStep, findCandidate, isSandboxRecipeSelected, realFlowSteps, type RealFlowState, type RealFlowStep } from './realflow-state.ts';

// Presentation-only derivation for the vertical step stack: what each step's
// collapsed row shows. Reads state, never changes it. A green check ("done")
// is only given when the step's outcome really allows moving on on its merits:
// a blocked environment, a fake SSH verification or a failed run are marked
// "attention" instead, so the collapsed row can't read as a success.

export type StepMark = 'current' | 'done' | 'attention' | 'open' | 'locked';
export interface StepRow { step: RealFlowStep; mark: StepMark; summary: string | null; viewable: boolean }
interface Outcome { mark: 'done' | 'attention' | null; summary: string | null }

const NONE: Outcome = { mark: null, summary: null };

function stepOutcome(s: RealFlowState, step: RealFlowStep): Outcome {
  if (step === 'input') {
    if (!s.paper) return NONE;
    const label = `${s.paper.title || s.paper.paper_id} · ${s.paper.status}`;
    return { mark: s.paper.error ? 'attention' : 'done', summary: label };
  }
  if (step === 'provider') {
    if (s.provider) return { mark: 'done', summary: `${s.provider.modelId} · ${s.provider.endpointAlias}` };
    return { mark: null, summary: s.paper ? '未创建会话（可跳过）' : null };
  }
  if (step === 'extract') {
    if (s.extractionPhase === 'ready' && s.extraction) return { mark: 'done', summary: `${s.extraction.candidate_experiments.length} 个候选实验` };
    if (s.extractionPhase === 'error') return { mark: 'attention', summary: '抽取失败' };
    return { mark: null, summary: s.extractionPhase === 'loading' ? '抽取进行中…' : null };
  }
  if (step === 'spec') {
    const candidate = findCandidate(s.extraction, s.selectedCandidateId);
    const label = candidate ? candidate.label || candidate.id : null;
    if (s.confirmationId) return { mark: 'done', summary: `${label ?? '方案'} · 已确认` };
    return { mark: null, summary: label ? `已选 ${label}，未确认` : null };
  }
  if (step === 'environment') {
    if (s.ssh) return { mark: 'attention', summary: 'SSH target 已登记（fake 验证，未发起真实连接）' };
    if (s.envCheck) return { mark: s.envCheck.decision === 'ready' ? 'done' : 'attention', summary: `decision=${s.envCheck.decision}` };
    if (s.envPhase === 'error') return { mark: 'attention', summary: '环境检查失败' };
    return { mark: null, summary: s.envPhase === 'loading' ? '环境检查进行中…' : null };
  }
  if (step === 'codegen') {
    if (s.codegen) return { mark: 'done', summary: `attempt ${s.codegen.attemptIndex} · sha256 ${s.codegen.codeSha256.slice(0, 12)}…` };
    if (s.codegenPhase === 'error') return { mark: 'attention', summary: '代码生成失败' };
    if (s.codegenPhase === 'loading') return { mark: null, summary: '代码生成中…' };
    if (s.confirmationId === null) return NONE;
    return isSandboxRecipeSelected(s)
      ? { mark: 'attention', summary: 'sandbox_python 需先生成代码' }
      : { mark: null, summary: '可选：当前 recipe 不要求生成代码' };
  }
  if (!s.runId) return NONE;
  const status = s.run?.status ?? null;
  if (status === 'completed') return { mark: 'done', summary: `Run completed${s.reportStatus ? ` · 报告 ${s.reportStatus}` : ''}` };
  if (status === 'failed' || status === 'cancelled') return { mark: 'attention', summary: `Run ${status}` };
  // An SSH run only records the verified target; nothing has been executed.
  if (status === 'created') return { mark: 'attention', summary: 'Run created：仅登记目标，没有任何执行' };
  return { mark: null, summary: status ? `Run ${status}` : 'Run 已创建，等待读取状态' };
}

export function stepRows(s: RealFlowState): StepRow[] {
  return realFlowSteps(s).map(step => {
    const viewable = canRealViewStep(s, step);
    const outcome = stepOutcome(s, step);
    const mark: StepMark = step === s.step ? 'current' : !viewable ? 'locked' : outcome.mark ?? 'open';
    return { step, mark, summary: outcome.summary, viewable };
  });
}

// The step after the current one, if the existing gate already allows viewing it.
export function nextViewableStep(s: RealFlowState): RealFlowStep | null {
  const steps = realFlowSteps(s);
  const next = steps[steps.indexOf(s.step) + 1];
  return next !== undefined && canRealViewStep(s, next) ? next : null;
}
