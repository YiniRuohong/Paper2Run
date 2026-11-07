import type { ProjectConfig, TargetProbe } from '../../api-client/autonomous';
import { Badge, Dot, StatusWord } from '../../ui/Card';
import { Icon } from '../../ui/Icon';
import { probeMeta, probeRows, SSH_EXPERIMENTAL } from './probe';

export type ProbeState =
  | { phase: 'idle' }
  | { phase: 'loading' }
  | { phase: 'ready'; probe: TargetProbe; version: number; config: ProjectConfig }
  | { phase: 'error'; error: string };

// 执行位置能力（P1-4 / 契约 C6）：只显示探测确认过的能力，其余写「未验证」并说明原因。
// 探测读取已保存的项目配置，不创建容器；启动不以此为准，准备阶段才是关卡。
export default function TargetProbeView({ state, ssh, disabledReason, onProbe }: {
  state: ProbeState; ssh: boolean; disabledReason: string; onProbe: () => void;
}) {
  const loading = state.phase === 'loading';
  return <div className="auto-probe" aria-busy={loading}>
    <div className="auto-probe-head">
      <h3 className="ui-subhead">执行位置能力</h3>
      {ssh && <Badge tone="warn">{SSH_EXPERIMENTAL}</Badge>}
      <button type="button" className="btn btn-sm btn-secondary" disabled={!!disabledReason || loading}
        title={disabledReason || (loading ? '正在探测，最长约 20 秒' : undefined)} onClick={onProbe}>
        <Icon name="refresh" />{loading ? '探测中…' : '探测执行位置'}
      </button>
    </div>
    {state.phase === 'idle' && <div className="ui-list ui-empty">
      <h3>尚未探测</h3>
      <p>{disabledReason || '点击「探测执行位置」检查 Docker、实验镜像与 GPU。'}探测读取已保存的项目配置，不创建容器。</p>
    </div>}
    {loading && <ul className="ui-list" aria-label="正在探测执行位置">
      {[0, 1, 2, 3, 4].map(i => <li key={i} className="ui-row"><span className="ui-skel w30" /><span className="ui-skel w20" /></li>)}
    </ul>}
    {state.phase === 'error' && <div className="ui-callout tone-bad" role="alert">
      <Icon name="alert" />
      <span>探测失败：{state.error}。能力未知，启动后由准备阶段检查。</span>
      <button type="button" className="btn btn-sm btn-secondary" disabled={!!disabledReason} title={disabledReason || undefined} onClick={onProbe}>重新探测</button>
    </div>}
    {state.phase === 'ready' && <>
      <ul className="ui-list" aria-label="执行位置能力">
        {probeRows(state.probe, state.config.limits.gpu_devices ?? []).map(row => <li key={row.key} className="ui-row">
          <span className="ui-row-main">
            <span className="ui-row-title">{row.label}</span>
            {row.reason && <span className="ui-row-sub">{row.reason}</span>}
          </span>
          <span className="ui-row-end"><Dot tone={row.tone} /><StatusWord tone={row.tone}>{row.status}</StatusWord></span>
        </li>)}
      </ul>
      <p className="ui-help auto-probe-meta">{probeMeta(state.probe)}，基于已保存配置 v{state.version}</p>
    </>}
  </div>;
}
