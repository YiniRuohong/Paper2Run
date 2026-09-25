import type { ReactNode } from 'react';
import { ApiClientError } from '../api-client';

export function ErrorBanner({
  error,
  onRetry,
  action,
}: {
  error: unknown;
  onRetry?: () => void;
  action?: ReactNode;
}) {
  const e = error instanceof ApiClientError ? error : null;
  return (
    <div className="banner err" role="alert">
      <div>
        <div className="b-title">{e ? `${e.api.code} · ${e.api.message}` : '请求失败'}</div>
        <div className="b-sub">
          request_id={e?.requestId ?? 'unknown'}
          {e && e.api.details && 'latest_revision_id' in e.api.details && e.api.details.latest_revision_id
            ? ` · latest_revision_id=${e.api.details.latest_revision_id}`
            : ''}
        </div>
      </div>
      <div className="b-act" style={{ display: 'flex', gap: 8 }}>
        {onRetry && (
          <button type="button" className="btn sm" onClick={onRetry}>重试</button>
        )}
        {action}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  sub,
  action,
}: {
  title: string;
  sub?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="es-title">{title}</div>
      {sub && <div className="es-sub">{sub}</div>}
      {action && <div className="es-act">{action}</div>}
    </div>
  );
}

export function SkeletonBlock({ lines = 3 }: { lines?: number }) {
  return (
    <div aria-hidden="true" style={{ padding: '8px 0' }}>
      {Array.from({ length: lines }).map((_, i) => (
        <div key={i} className="skeleton s-line" style={{ width: `${92 - (i % 3) * 18}%` }}></div>
      ))}
    </div>
  );
}

export function PageLoading({ label = '加载中' }: { label?: string }) {
  return <div className="loading-page">{label}…</div>;
}
