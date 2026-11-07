import { useEffect, useRef } from 'react';
import { Icon } from './Icon';

import { toastLifetime, type ToastTone } from './toastTiming';

export interface ToastItem { id: string; tone: ToastTone; text: string }

function Toast({ item, onClose }: { item: ToastItem; onClose: (id: string) => void }) {
  const ms = toastLifetime(item.tone);
  // The latest handler is read through a ref so a parent re-render never restarts the timer.
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    if (ms === null) return;
    const timer = window.setTimeout(() => close.current(item.id), ms);
    return () => window.clearTimeout(timer);
  }, [item.id, ms]);
  return <div className={`ui-toast tone-${item.tone}`} role={item.tone === 'bad' ? 'alert' : 'status'}>
    <Icon name={item.tone === 'ok' ? 'check-circle' : item.tone === 'bad' ? 'alert' : 'info'} />
    <span className="ui-toast-text">{item.text}</span>
    <button type="button" className="ui-toast-close" aria-label="关闭提示" onClick={() => onClose(item.id)}><Icon name="x" size={13} /></button>
  </div>;
}

// Bottom-right stack of transient notices (fixed to the viewport).
export function ToastStack({ items, onClose }: { items: ToastItem[]; onClose: (id: string) => void }) {
  if (items.length === 0) return null;
  return <div className="ui-toasts">{items.map(item => <Toast key={item.id} item={item} onClose={onClose} />)}</div>;
}
