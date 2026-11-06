import { useState } from 'react';
import type { AutonomousApi } from '../../../api-client/autonomous';
import type { PapersApi } from '../../../api-client/papers';
import { OBJECTIVE_LIMIT, mergeConfig, objectiveError, refreshOnFailure, saveConfigWithRetry } from '../draft';
import { readConfig } from '../draftData';
import StepFrame, { type Act, type StepFeedback } from '../StepFrame';

const EXAMPLE = '例如：复现论文表 2 中模型在 CIFAR-10 上的测试准确率，只用 CPU 跑缩小版本（1 个 epoch），与论文数值相差 5 个百分点以内视为一致。';

// 02 实验目标: one text box. Saved into the project description (the config
// cannot be written before a model exists); an existing config gets it too.
// When either write fails the page reads again, so a retry sends the current
// project version instead of the one the first PATCH already used up.
export default function ObjectiveStep({ projectId, projectVersion, initial, hasConfig, api, papers, busy, act, feedback, refresh, onDone }: {
  projectId: string;
  projectVersion: number;
  initial: string;
  hasConfig: boolean;
  api: Pick<AutonomousApi, 'config' | 'saveConfig'>;
  papers: Pick<PapersApi, 'updateProjectDescription'>;
  busy: boolean;
  act: Act;
  feedback: StepFeedback | null;
  /** Reads the draft again (project version, config) after a failed save. */
  refresh(): Promise<unknown>;
  onDone(): Promise<void>;
}) {
  const [text, setText] = useState(initial);
  const invalid = objectiveError(text);
  const save = () => void act(() => refreshOnFailure(async () => {
    const objective = text.trim();
    await papers.updateProjectDescription(projectId, objective, projectVersion);
    if (hasConfig) {
      await saveConfigWithRetry(() => readConfig(api, projectId), (config, version) => api.saveConfig(projectId, config, version),
        saved => mergeConfig(saved, projectId, { objective }));
    }
  }, refresh).then(onDone));

  return <StepFrame guide="写清要验证什么、看哪个指标、允许多大范围。" feedback={feedback}
    next={{ label: '下一步', disabled: busy ? '处理中…' : invalid || undefined, onClick: save }}>
    <label className="flow-textarea">
      <span className="sr-only">实验目标</span>
      <textarea rows={7} value={text} maxLength={OBJECTIVE_LIMIT} disabled={busy} placeholder={EXAMPLE} onChange={e => setText(e.target.value)} />
      <small className="flow-count" aria-live="polite">{text.length} / {OBJECTIVE_LIMIT}</small>
    </label>
  </StepFrame>;
}
