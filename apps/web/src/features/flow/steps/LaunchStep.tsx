import { useCallback, useEffect, useRef, useState } from 'react';
import type { AutonomousApi, ModelProfile, ProjectConfig } from '../../../api-client/autonomous';
import type { PaperText } from '../../../api-client/papers';
import { Switch } from '../../../ui/Controls';
import { errorDetail } from '../../autonomous/errors';
import { launchBlockReason } from '../../autonomous/launch';
import { parseProbe, probeMissingNotice } from '../../autonomous/probe';
import TargetProbeView, { type ProbeState } from '../../autonomous/TargetProbeView';
import { budgetLine, mergeConfig, saveConfigWithRetry, type DraftPaper } from '../draft';
import { readConfig, type SavedConfig } from '../draftData';
import StepFrame, { type Act, type StepFeedback } from '../StepFrame';
import { connectionLabel } from './ModelStep';
import '../../autonomous/autonomous.css';

const clip = (text: string, n: number) => Array.from(text).length > n ? `${Array.from(text).slice(0, n).join('')}…` : text;

function withTarget(base: ProjectConfig['target'], ssh: boolean, alias: string, remote: string): ProjectConfig['target'] {
  const { ssh_config_alias: _alias, remote_workspace: _remote, ...common } = base;
  return ssh ? { ...common, kind: 'ssh', ssh_config_alias: alias, remote_workspace: remote } : { ...common, kind: 'local' };
}

// 04 确认并启动: read-only summary, execution location (local by default, SSH
// on request), the saved budget, authorization and start. The target probe is
// a hint only; it never blocks the start button.
export default function LaunchStep({ projectId, paper, paperText, saved, profile, objective, api, busy, act, feedback, onSaved }: {
  projectId: string;
  paper: DraftPaper | null;
  paperText: PaperText | null;
  saved: SavedConfig;
  profile: ModelProfile | undefined;
  objective: string;
  api: Pick<AutonomousApi, 'config' | 'saveConfig' | 'probeTarget' | 'start'>;
  busy: boolean;
  act: Act;
  feedback: StepFeedback | null;
  onSaved(): Promise<void>;
}) {
  const target = saved.config.target;
  const [ssh, setSsh] = useState(target.kind === 'ssh');
  const [alias, setAlias] = useState(target.ssh_config_alias ?? '');
  const [remote, setRemote] = useState(target.remote_workspace ?? `/workspace/paper2run/${projectId}`);
  const [authorized, setAuthorized] = useState(false);
  const [probe, setProbe] = useState<ProbeState>({ phase: 'idle' });
  const probeAbort = useRef<AbortController | null>(null);
  const savedRef = useRef(saved);
  savedRef.current = saved;

  const runProbe = useCallback(async () => {
    const probed = savedRef.current;
    probeAbort.current?.abort();
    const controller = new AbortController();
    probeAbort.current = controller;
    setProbe({ phase: 'loading' });
    try {
      const parsed = parseProbe(await api.probeTarget(projectId, controller.signal));
      if (controller.signal.aborted) return;
      setProbe(parsed ? { phase: 'ready', probe: parsed, version: probed.version, config: probed.config } : { phase: 'error', error: '探测结果格式无法识别' });
    } catch (e) {
      if (!controller.signal.aborted) setProbe({ phase: 'error', error: errorDetail(e) });
    }
  }, [api, projectId]);
  // Probe what is saved: on entry, and again after every save of the config.
  useEffect(() => { void runProbe(); }, [runProbe, saved.version]);
  useEffect(() => () => probeAbort.current?.abort(), []);

  const dirty = ssh !== (target.kind === 'ssh')
    || (ssh && (alias.trim() !== (target.ssh_config_alias ?? '') || remote.trim() !== (target.remote_workspace ?? '')));
  const targetReason = !ssh ? '' : !alias.trim() ? '请填写 SSH 配置别名' : !remote.trim().startsWith('/') ? '远端目录须为绝对路径' : '';
  const saveTarget = () => void act(async () => {
    const next = withTarget(target, ssh, alias.trim(), remote.trim());
    await saveConfigWithRetry(() => readConfig(api, projectId), (config, version) => api.saveConfig(projectId, config, version),
      latest => mergeConfig(latest, projectId, { target: next }));
    await onSaved();
  });

  const startBlockReason = launchBlockReason({
    projectId, paperVersion: paper?.versionId ?? '', pdf: paper?.pdf ?? false, text: paperText, authorized,
    mainProfile: saved.config.model.provider_profile, objective,
  }) || (dirty ? '请先保存执行位置' : '') || (busy ? '处理中…' : '');
  const probeNotice = probe.phase === 'ready' ? probeMissingNotice(probe.probe) : '';
  const start = () => void act(async () => {
    const created = await api.start(projectId, { objective, paper_version_id: paper?.versionId ?? '', authorization: { confirmed: true } });
    window.location.replace(`#/task/${encodeURIComponent(created.id)}`);
  });

  return <StepFrame guide="确认论文、目标和模型，选择执行位置后授权启动。" feedback={feedback}
    next={{ label: '授权并启动', disabled: startBlockReason || undefined, onClick: start }}>
    <dl className="flow-summary">
      <div><dt>论文</dt><dd>{paper?.title ?? '—'}</dd></div>
      <div><dt>目标</dt><dd>{clip(objective, 120)}</dd></div>
      <div><dt>模型</dt><dd>{profile ? connectionLabel(profile) : saved.config.model.provider_profile}</dd></div>
    </dl>

    <div className="flow-block">
      <div className="flow-row">
        <span className="flow-row-label">执行位置</span>
        <span className="flow-row-value">{ssh ? 'SSH 隔离环境' : '本地隔离环境'}</span>
      </div>
      <Switch checked={ssh} disabled={busy} onChange={setSsh} hint="使用本机已配置的 SSH 别名与凭据，界面不收集私钥或密码。">使用 SSH</Switch>
      {ssh && <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">SSH 配置别名</span>
          <input value={alias} disabled={busy} placeholder="gpu-box" onChange={e => setAlias(e.target.value)} />
        </label>
        <label className="ui-field"><span className="ui-field-label">远端目录</span>
          <input value={remote} disabled={busy} onChange={e => setRemote(e.target.value)} />
        </label>
      </div>}
      {dirty && <div className="flow-actions">
        <button type="button" className="btn btn-secondary" disabled={busy || !!targetReason} onClick={saveTarget}>保存执行位置</button>
        {targetReason && <span className="ui-help">{targetReason}</span>}
      </div>}
      <TargetProbeView state={probe} ssh={target.kind === 'ssh'} disabledReason={dirty ? '保存执行位置后才能探测。' : ''} onProbe={() => void runProbe()} />
      {probeNotice && <p className="ui-help auto-probe-missing" role="status">{probeNotice}</p>}
    </div>

    <p className="flow-budget">预算：{budgetLine(saved.config.limits)}</p>

    <Switch checked={authorized} onChange={setAuthorized} disabled={busy} hint="超出协议授权的变更需暂停并等待批准。">
      我授权在上述目标与预算内联网、获取数据与代码、安装依赖和运行实验
    </Switch>
  </StepFrame>;
}
