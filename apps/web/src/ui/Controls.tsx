import { useEffect, useState, type ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

// Toggle switch: a native checkbox with role="switch", so label clicks,
// Space and form semantics all come for free.
//   <Switch checked={on} onChange={setOn} hint="…">我同意…</Switch>
export function Switch({ checked, onChange, disabled, hint, children }: { checked: boolean; onChange: (checked: boolean) => void; disabled?: boolean; hint?: ReactNode; children: ReactNode }) {
  return <label className={`ui-switch-row${disabled ? ' is-disabled' : ''}`}>
    <span className="ui-switch-text">{children}{hint && <small>{hint}</small>}</span>
    <input type="checkbox" role="switch" className="ui-switch" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />
  </label>;
}

// Segmented control for small mode switches (本机 | 局域网).
//   <Segmented label="主题" value={v} onChange={setV} options={[{ value: 'a', label: 'A' }]} />
export interface SegmentOption<T extends string> { value: T; label?: ReactNode; icon?: IconName; ariaLabel?: string }
export function Segmented<T extends string>({ label, value, options, onChange, disabled }: { label: string; value: T; options: ReadonlyArray<SegmentOption<T>>; onChange: (value: T) => void; disabled?: boolean }) {
  return <div className="ui-seg" role="group" aria-label={label}>
    {options.map(o => <button key={o.value} type="button" disabled={disabled} className={o.value === value ? 'on' : ''} aria-pressed={o.value === value} aria-label={o.ariaLabel} title={o.ariaLabel} onClick={() => onChange(o.value)}>
      {o.icon && <Icon name={o.icon} size={15} />}{o.label}
    </button>)}
  </div>;
}

async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall back below (e.g. insecure context) */ }
  try {
    const area = document.createElement('textarea');
    area.value = text; area.setAttribute('readonly', ''); area.style.position = 'fixed'; area.style.opacity = '0';
    document.body.appendChild(area); area.select();
    const ok = document.execCommand('copy');
    area.remove();
    return ok;
  } catch { return false; }
}

// Tiny copy-to-clipboard icon button. Copies the full value even when the
// visible text is truncated.
export function CopyButton({ value, label = '复制' }: { value: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'done' | 'fail'>('idle');
  useEffect(() => {
    if (state === 'idle') return;
    const id = window.setTimeout(() => setState('idle'), 1400);
    return () => window.clearTimeout(id);
  }, [state]);
  const text = state === 'done' ? '已复制' : state === 'fail' ? '复制失败' : label;
  return <button type="button" className={`ui-copy${state === 'done' ? ' is-done' : ''}`} aria-label={text} title={text} onClick={() => { void writeClipboard(value).then(ok => setState(ok ? 'done' : 'fail')); }}>
    <Icon name={state === 'done' ? 'check' : 'copy'} size={13} />
  </button>;
}
