import { useEffect, useRef, type ReactNode } from 'react';
import { Icon } from '../Icon';

export interface WbDrawer {
  /** Small chip before the title, e.g. 「步骤 06 · 编写与执行」. */
  label: string;
  title: string;
  body: ReactNode;
  onClose: () => void;
  /** A file the drawer is about; the workbench offers to open it in the centre column. */
  openPath?: string;
}

// Right-side drawer over the files column; the rest of the workbench is dimmed.
// Esc, ✕ or the dimmed area closes it. Focus moves in on open and back to the
// clicked element on close.
export function Drawer({ drawer, footer }: { drawer: WbDrawer; footer?: ReactNode }) {
  const panel = useRef<HTMLElement>(null);
  const onClose = useRef(drawer.onClose); onClose.current = drawer.onClose;
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose.current(); };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); opener?.focus?.(); };
  }, []);
  return <>
    <div className="wb-drawer-scrim" aria-hidden="true" onClick={() => onClose.current()} />
    <aside className="wb-drawer" role="dialog" aria-label={drawer.title} tabIndex={-1} ref={panel}>
      <header className="wb-drawer-head">
        <span className="wb-drawer-chip">{drawer.label}</span>
        <b className="wb-drawer-title" title={drawer.title}>{drawer.title}</b>
        <button type="button" className="btn btn-icon wb-drawer-close" aria-label="关闭" onClick={() => onClose.current()}><Icon name="x" size={15} /></button>
      </header>
      <div className="wb-drawer-body">{drawer.body}</div>
      {footer && <footer className="wb-drawer-foot">{footer}</footer>}
    </aside>
  </>;
}
