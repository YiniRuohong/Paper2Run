import { useState } from 'react';
import { Card, CardHeader } from '../../ui/Card';
import { CopyButton } from '../../ui/Controls';
import { Disclosure } from '../../ui/Disclosure';
import type { ProtocolApproval } from '../../api-client/autonomous';

export const REJECT_NOTE_LIMIT = 4000;

// 审批卡（P0-4）：人在环中的决策做成一等公民。
// 批准与拒绝是两个独立动作（暂停在任务页主操作栏）；批准与拒绝都走显式决定接口（契约 C2），
// 不用自由文本消息冒充。拒绝保留当前方案，可选理由会作为你的指令转达给 Pi。
export function ApprovalCard({ approval, busy, blockedReason, onApprove, onReject }: {
  approval: ProtocolApproval;
  busy: boolean;
  /** 决定按钮不可用的原因（例如任务尚未暂停）。 */
  blockedReason?: string;
  onApprove: () => void;
  onReject: (note: string) => void;
}) {
  const [note, setNote] = useState('');
  const sha = approval.proposed.sha256;
  const blocked = busy || !!blockedReason;
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
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">拒绝理由（可选）<small>拒绝时作为你的指令转达给 Pi，最多 {REJECT_NOTE_LIMIT} 字</small></span>
          <textarea rows={2} maxLength={REJECT_NOTE_LIMIT} value={note} disabled={blocked} onChange={e => setNote(e.target.value)} placeholder="例如：保持原评估指标，先排查数据划分" />
        </label>
      </div>
      <p className="ui-help">批准采用上述冻结方案并继续实验；拒绝保留当前方案并继续。普通「继续」和追加指令不会批准此方案。</p>
      <div className="auto-actions">
        <button type="button" className="btn btn-primary" disabled={blocked} title={blockedReason} onClick={onApprove}>批准此方案并继续</button>
        <button type="button" className="btn btn-secondary" disabled={blocked} title={blockedReason} onClick={() => onReject(note)}>拒绝变更并继续</button>
        {blockedReason && <span className="ui-help">{blockedReason}</span>}
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
