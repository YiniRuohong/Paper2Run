import type { AutonomousEvent, AutonomousTask } from '../../api-client/autonomous.ts';
import type { StageMark, WbStage, WbStageItem } from '../../ui/workbench/Workbench.tsx';
import { PHASES, buildStages } from '../autonomous/stages.ts';
import type { PhaseId } from '../autonomous/stages.ts';
import { SETUP_ORDER } from './draft.ts';
import type { SetupStepId } from './draft.ts';

// Nine-step timeline: four user setup steps, then the five AI phases derived
// from the task's event journal. Presentation only; the phases themselves come
// from buildStages and nothing here assigns a program judgement.

export const SETUP_TITLES: Record<SetupStepId, string> = { paper: '上传论文', objective: '实验目标', model: '连接模型', launch: '确认并启动' };
export const RUN_TITLES = { plan: '阅读与方案', execute: '编写与执行', evidence: '冻结证据', review: '候选与复核', report: '报告' } as const;
export const AI_GROUP = '启动后由 AI 推进';

export interface SetupView { id: SetupStepId; done: boolean; summary: string | null }

type RunPhase = keyof typeof RUN_TITLES;
const RUN_ORDER: RunPhase[] = ['plan', 'execute', 'evidence', 'review', 'report'];

export function flowStages(input: { setup: SetupView[]; current: SetupStepId | null; task: AutonomousTask | null; events: AutonomousEvent[] }): { stages: WbStage[]; activeId: string; doneCount: number } {
  const { setup, current, task, events } = input;
  const built = task ? buildStages(task, events) : null;
  const byPhase = new Map<PhaseId, WbStage>((built?.stages ?? []).map(s => [s.id as PhaseId, s]));
  const setupItems: WbStageItem[] = byPhase.get('setup')?.items ?? [];

  const stages: WbStage[] = SETUP_ORDER.map(id => {
    const view = setup.find(s => s.id === id);
    const mark: StageMark = task || view?.done ? 'done' : id === current ? 'current' : 'locked';
    const stage: WbStage = { id, title: SETUP_TITLES[id], mark, summary: view?.summary ?? null, disabled: mark === 'locked' };
    if (id === 'launch') stage.items = setupItems;
    return stage;
  });

  for (const id of RUN_ORDER) {
    const title = RUN_TITLES[id];
    const real = byPhase.get(id);
    const stage: WbStage = real
      ? { ...real, title }
      : { id, title, mark: 'locked', summary: PHASES.find(p => p.id === id)!.idle, items: [], disabled: true };
    if (id === 'plan') stage.group = AI_GROUP;
    stages.push(stage);
  }

  const activeId = task && built ? (built.active === 'setup' ? 'launch' : built.active) : current ?? 'launch';
  return { stages, activeId, doneCount: stages.filter(s => s.mark === 'done').length };
}
