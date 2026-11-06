import { useEffect, useRef } from 'react';
import { Icon } from './Icon';

// Click-to-zoom for an image: it scales up into the middle of the window over a
// dimmed page. Esc, ✕ or a click anywhere closes it; focus returns to the opener.
export function Lightbox({ url, name, onClose }: { url: string; name: string; onClose: () => void }) {
  const close = useRef(onClose); close.current = onClose;
  const button = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    button.current?.focus();
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close.current(); };
    document.addEventListener('keydown', onKey);
    return () => { document.removeEventListener('keydown', onKey); opener?.focus?.(); };
  }, []);
  return <div className="ui-lightbox" role="dialog" aria-modal="true" aria-label={name} onClick={() => close.current()}>
    <figure className="ui-lightbox-figure">
      <img src={url} alt={name} />
      <figcaption className="ui-mono">{name}</figcaption>
    </figure>
    <button type="button" ref={button} className="ui-lightbox-close" aria-label="关闭" onClick={() => close.current()}><Icon name="x" size={18} /></button>
  </div>;
}
