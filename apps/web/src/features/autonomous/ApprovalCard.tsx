import { Card, CardHeader } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import type { ProtocolApproval } from '../../api-client/autonomous';

// 审批卡（P0-4）：人在环中的决策做成一等公民。
// 批准 / 拒绝 / 先暂停是三个独立动作；拒绝在后端提供显式接口（契约 C2）之前
// 禁用并写明原因，不用自由文本消息冒充拒绝。
export function ApprovalCard({ approval, busy, onApprove, onPause }: {
  approval: ProtocolApproval;
  busy: boolean;
  onApprove: () => void;
  onPause: () => void;
}) {
  const sha = approval.proposed.sha256;
  return <Card className="auto-approval" aria-label="等待方案审批">
    <CardHeader icon="alert" title="需要你决定：方案变更" status={{ text: '待审批', tone: 'warn' }}
      subtitle={approval.reason} />
    <div className="ui-card-section auto-approval-body">
      <dl className="ui-kv">
        <div className="ui-kv-row">
          <dt>决策 ID</dt>
          <dd><span className="ui-mono">{approval.id}</span><CopyButton value={approval.id} label="复制决策 ID" /></dd>
        </div>
        <div className="ui-kv-row">
          <dt>方案 SHA256</dt>
          <dd><span className="ui-mono" title={sha}>{sha.slice(0, 16)}…</span><CopyButton value={sha} label="复制方案 SHA256" /></dd>
        </div>
      </dl>
      <div>
        <h3 className="ui-subhead">变更字段（{approval.changed.length}）</h3>
        {approval.changed.length === 0
          ? <p className="ui-help">无逐项变更；批准采用整个拟议方案。</p>
          : <ul className="ui-list auto-approval-changed">
              {approval.changed.map(field => <li key={field} className="ui-row">
                <span className="ui-row-main"><span className="ui-row-title ui-mono">{field}</span>
                  <span className="ui-row-sub ui-mono">{formatValue(approval.proposed.protocol[field])}</span></span>
              </li>)}
            </ul>}
      </div>
      <Disclosure variant="sub" title="拟议协议全文">
        <pre className="ui-code" aria-label="拟议协议 JSON（只读）">{JSON.stringify(approval.proposed.protocol, null, 2)}</pre>
      </Disclosure>
      <p className="ui-help">批准将采用上述冻结方案并继续实验；普通「继续」和追加指令不会批准此方案。</p>
      <div className="auto-actions">
        <button type="button" className="btn btn-primary" disabled={busy} onClick={onApprove}>批准此方案并继续</button>
        <button type="button" className="btn btn-secondary" disabled title="后端尚未提供显式拒绝接口（契约 C2），暂不可用">拒绝</button>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={onPause}>先暂停</button>
      </div>
    </div>
  </Card>;
}

function formatValue(value: unknown): string {
  if (value === undefined) return '（未提供新值）';
  try {
    const text = JSON.stringify(value);
    return text.length > 120 ? `${text.slice(0, 119)}…` : text;
  } catch {
    return String(value);
  }
}
