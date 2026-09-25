// 展示格式化：单位与 null 语义遵循契约（micros/1e6、percent 不再乘 100、
// percent 的差显示 pp、ratio 不加 %；未知显示"未知"，不是 0）。
import type { MetricRow, Run } from '../api-client/types';

export function formatMicros(micros: number | null): string {
  if (micros === null) return '未知';
  return `¥${(micros / 1_000_000).toFixed(2)}`;
}

export function formatBytes(bytes: number | null): string {
  if (bytes === null) return '未知';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '未知';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`;
  return `${Math.floor(seconds / 3600)} 时 ${Math.floor((seconds % 3600) / 60)} 分`;
}

export function formatLocalTime(utc: string | null): string {
  if (!utc) return '未知';
  const d = new Date(utc);
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function timeAgo(utc: string | null): string {
  if (!utc) return '未知';
  const diff = Date.now() - new Date(utc).getTime();
  if (diff < 60_000) return '刚刚';
  if (diff < 3600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 24 * 3600_000) return `${Math.floor(diff / 3600_000)} 小时前`;
  return `${Math.floor(diff / (24 * 3600_000))} 天前`;
}

export function formatElapsed(run: Pick<Run, 'started_at' | 'ended_at'>): string {
  if (!run.started_at) return '未开始';
  const end = run.ended_at ? new Date(run.ended_at).getTime() : Date.now();
  const sec = Math.max(0, Math.round((end - new Date(run.started_at).getTime()) / 1000));
  return formatDuration(sec);
}

export function formatMetricValue(value: number | null, unit: MetricRow['unit']): string {
  if (value === null) return '未测';
  if (unit === 'percent') return `${value.toFixed(2)}%`;
  if (unit === 'ratio') return value.toFixed(4);
  if (unit === 'count') return String(Math.round(value));
  return value.toFixed(2);
}

export function formatDelta(value: number | null, unit: MetricRow['unit']): string {
  if (value === null) return '不可比较';
  const sign = value > 0 ? '+' : '';
  if (unit === 'percent') return `${sign}${value.toFixed(2)} pp`;
  if (unit === 'ratio') return `${sign}${value.toFixed(4)}`;
  return `${sign}${value.toFixed(2)}`;
}

export function formatTolerance(tolerance: number | null, unit: MetricRow['unit']): string {
  if (tolerance === null) return '未约定';
  if (unit === 'percent') return `±${tolerance.toFixed(2)} pp`;
  return `±${tolerance.toFixed(4)}`;
}

export function formatRatioScore(value: number | null): string {
  if (value === null) return '不可比较';
  return value.toFixed(2);
}
