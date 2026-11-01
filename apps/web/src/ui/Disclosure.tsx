import { useId, useState, type ReactNode } from 'react';
import { Icon } from './Icon';

// Hierarchical disclosure row: "› 标题 3" collapsed, "⌄ 标题 3" expanded.
// Variants: "row" (bare row, for nesting inside cards/sections), "card"
// (bordered container whose header is the toggle) and "sub" (compact, for
// technical details). Controlled when `open` is passed.
//
//   <Disclosure title="技术细节" variant="sub">…</Disclosure>
//   <Disclosure title="提醒" count={2} defaultOpen meta={<a>…</a>}>…</Disclosure>
export interface DisclosureProps {
  title: ReactNode;
  count?: number | string | null;
  subtitle?: ReactNode;
  meta?: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onToggle?: (open: boolean) => void;
  variant?: 'row' | 'card' | 'sub';
  className?: string;
  children: ReactNode;
}

export function Disclosure({ title, count, subtitle, meta, defaultOpen = false, open, onToggle, variant = 'row', className, children }: DisclosureProps) {
  const [inner, setInner] = useState(defaultOpen);
  const isOpen = open ?? inner;
  const id = useId();
  const toggle = () => { const next = !isOpen; if (open === undefined) setInner(next); onToggle?.(next); };
  return <div className={`ui-disc ui-disc-${variant}${isOpen ? ' is-open' : ''}${className ? ` ${className}` : ''}`}>
    <div className="ui-disc-head">
      <button type="button" className="ui-disc-toggle" aria-expanded={isOpen} aria-controls={id} onClick={toggle}>
        <Icon name="chevron-right" size={14} className="ui-disc-chev" />
        <span className="ui-disc-title">{title}</span>
        {count !== undefined && count !== null && <span className="ui-disc-count">{count}</span>}
        {subtitle && <span className="ui-disc-subtitle">{subtitle}</span>}
      </button>
      {meta && <div className="ui-disc-meta">{meta}</div>}
    </div>
    <div id={id} className="ui-disc-body" hidden={!isOpen}>{isOpen && children}</div>
  </div>;
}
