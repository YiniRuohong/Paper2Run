import type { ReactNode } from 'react';
import { CopyButton } from './Controls';
import { Icon, type IconName } from './Icon';

export type Tone = 'ok' | 'run' | 'bad' | 'warn' | 'idle' | 'accent';

// Bordered rounded card (1px hairline, surface lifted from bg).
export function Card({ className, children, ...rest }: { className?: string; children: ReactNode; 'aria-busy'?: boolean; 'aria-label'?: string }) {
  return <section className={`ui-card${className ? ` ${className}` : ''}`} {...rest}>{children}</section>;
}

// Card header: icon tile + title + inline status word + muted subtitle, actions right.
//   <CardHeader icon="server" title="工作区" status={{ text: '运行中', tone: 'ok' }} subtitle="…" actions={<button/>} />
export function CardHeader({ icon, title, status, subtitle, actions, as: Heading = 'h2' }: { icon: IconName; title: ReactNode; status?: { text: string; tone: Tone } | null; subtitle?: ReactNode; actions?: ReactNode; as?: 'h1' | 'h2' | 'h3' }) {
  return <div className="ui-card-head">
    <span className="ui-tile" aria-hidden="true"><Icon name={icon} size={18} /></span>
    <div className="ui-card-titles">
      <div className="ui-card-title-row"><Heading className="ui-card-title">{title}</Heading>{status && <StatusWord tone={status.tone}>{status.text}</StatusWord>}</div>
      {subtitle && <p className="ui-card-sub">{subtitle}</p>}
    </div>
    {actions && <div className="ui-card-actions">{actions}</div>}
  </div>;
}

export function StatusWord({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`ui-status tone-${tone}`}>{children}</span>;
}

export function Badge({ tone = 'idle', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return <span className={`ui-badge tone-${tone}`} title={title}>{children}</span>;
}

export function Dot({ tone }: { tone: Tone }) {
  return <span className={`ui-dot tone-${tone}`} aria-hidden="true" />;
}

// Section heading outside cards with an optional right-aligned text action.
export function SectionHead({ title, count, action, id }: { title: ReactNode; count?: number | string; action?: ReactNode; id?: string }) {
  return <div className="ui-section-head">
    <h2 id={id}>{title}{count !== undefined && <span className="ui-section-count">{count}</span>}</h2>
    {action && <div className="ui-section-action">{action}</div>}
  </div>;
}

// Key-value rows: muted label column + (mono) value + optional copy.
//   <KVList><KV label="paper_id" value={id} copy /></KVList>
export function KVList({ children, className }: { children: ReactNode; className?: string }) {
  return <dl className={`ui-kv${className ? ` ${className}` : ''}`}>{children}</dl>;
}
export function KV({ label, value, mono = true, copy = false, children }: { label: ReactNode; value?: string | number | null; mono?: boolean; copy?: boolean | string; children?: ReactNode }) {
  const shown = value === null || value === undefined || value === '' ? '—' : String(value);
  const copyValue = typeof copy === 'string' ? copy : copy && shown !== '—' ? shown : null;
  return <div className="ui-kv-row">
    <dt>{label}</dt>
    <dd>
      {children ?? <span className={mono ? 'ui-mono' : undefined}>{shown}</span>}
      {copyValue && <CopyButton value={copyValue} label={`复制 ${typeof label === 'string' ? label : ''}`.trim()} />}
    </dd>
  </div>;
}
