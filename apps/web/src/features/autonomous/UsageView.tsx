import { Badge, Card, CardHeader } from '../../ui/Card';

// 用量视图（P0-7）：每个数字标明度量性质。
// wall_time / 模型调用是硬限制；token 是 provider 上报；存储是观测值；
// unverified_token_reservation 是预留未核实（估算）。
// 后端契约 C5 给出 kind 字段后，改为按字段读取，不再使用这里的写死映射。

interface UsageRow {
  label: string;
  used: number | null;
  limit: number | null;
  kind: 'enforced' | 'provider' | 'observed' | 'estimated';
  format: (v: number) => string;
}

const KIND_BADGE: Record<UsageRow['kind'], { text: string } | null> = {
  enforced: null,
  provider: { text: 'provider 上报' },
  observed: { text: '观测' },
  estimated: { text: '估算' },
};

const plain = (v: number) => v.toLocaleString('zh-CN');
const seconds = (v: number) => {
  if (v < 60) return `${v} 秒`;
  const m = Math.floor(v / 60);
  return m < 60 ? `${m} 分钟` : `${Math.floor(m / 60)} 小时 ${m % 60} 分钟`;
};
export const formatBytes = (v: number) => {
  if (!Number.isFinite(v) || v <= 0) return '0 MB';
  const gb = v / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.max(1, Math.round(v / 1024 ** 2))} MB`;
};

const usageNum = (usage: Record<string, unknown>, ...keys: string[]): number | null => {
  for (const key of keys) {
    const value = usage[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return null;
};

export function usageRows(usage: Record<string, unknown>, limits: Record<string, unknown> | undefined): UsageRow[] {
  const limit = (key: string) => {
    const value = limits?.[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };
  const rows: UsageRow[] = [
    { label: '运行时长', used: usageNum(usage, 'wall_time_seconds'), limit: limit('wall_time_seconds'), kind: 'enforced', format: seconds },
    { label: '模型调用', used: usageNum(usage, 'calls', 'model_calls'), limit: limit('max_model_calls'), kind: 'enforced', format: plain },
    { label: 'Token', used: usageNum(usage, 'total_tokens', 'model_tokens'), limit: limit('max_model_tokens'), kind: 'provider', format: plain },
    { label: '预留 Token', used: usageNum(usage, 'unverified_token_reservation'), limit: null, kind: 'estimated', format: plain },
    { label: '存储', used: usageNum(usage, 'storage_bytes'), limit: limit('storage_bytes'), kind: 'observed', format: formatBytes },
  ];
  return rows.filter(row => row.used !== null || row.limit !== null);
}

export function UsageView({ usage, limits, budgetExhausted }: { usage: Record<string, unknown>; limits: Record<string, unknown> | undefined; budgetExhausted?: string | null }) {
  const rows = usageRows(usage, limits);
  return <Card>
    <CardHeader icon="chart" title="使用量" subtitle="数字性质已逐项标注；观测与估算不代表精确计量。" />
    <div className="ui-card-section auto-usage">
      {rows.length === 0 && <p className="ui-help">后端尚未上报用量。</p>}
      {rows.map(row => {
        const ratio = row.used !== null && row.limit !== null && row.limit > 0 ? row.used / row.limit : null;
        const hot = ratio !== null && ratio >= 0.8;
        const badge = KIND_BADGE[row.kind];
        return <div className="auto-usage-row" key={row.label}>
          <span className="auto-usage-label">{row.label}{badge && <Badge tone={row.kind === 'enforced' ? 'idle' : 'warn'}>{badge.text}</Badge>}</span>
          <span className="auto-usage-value">
            <b className="ui-mono">{row.used !== null ? row.format(row.used) : '—'}</b>
            {row.limit !== null && <span className="auto-usage-limit ui-mono">/ {row.format(row.limit)}</span>}
          </span>
          {ratio !== null && <span className={`auto-usage-bar${hot ? ' is-hot' : ''}`} role="img" aria-label={`已用 ${Math.round(ratio * 100)}%`}><i style={{ width: `${Math.min(100, Math.round(ratio * 100))}%` }} /></span>}
        </div>;
      })}
      {budgetExhausted && <p className="ui-callout tone-warn" role="alert">预算已耗尽：{budgetExhausted}。在途的模型响应可能已超出上限。</p>}
    </div>
  </Card>;
}
