import { useId, type ReactNode } from 'react';

export interface StepFeedback { tone: 'ok' | 'bad' | 'idle'; text: string }
export interface StepNext { label: string; /** Reason the button is disabled; absent when it can be pressed. */ disabled?: string; onClick(): void }
/** Runs a write with the page's busy flag and top error line; resolves false when it failed. */
export type Act = (work: () => Promise<void>) => Promise<boolean>;

// The fixed rhythm of every step, before and after the start: the title with a
// "!" badge whose hover or focus bubble holds the guide, this step's content,
// then the footer pinned to the bottom of the centre column. Setup steps put
// one button there (next); the task page puts its controls there (actions).
// The previous step's result is not shown here: DraftPage raises it as a toast.
export default function StepFrame({ title, guide, children, next, actions }: { title: string; guide: string; children: ReactNode; next?: StepNext; actions?: ReactNode }) {
  const tipId = useId();
  return <section className="flow-step">
    <div className="flow-head">
      <h2 className="flow-title">{title}</h2>
      <span className="flow-hint-wrap">
        <button type="button" className="flow-hint" aria-label="本步说明" aria-describedby={tipId}>!</button>
        <span id={tipId} role="tooltip" className="flow-tip">{guide}</span>
      </span>
    </div>
    <div className="flow-fields">{children}</div>
    {(next || actions) && <div className="flow-next">
      {next && <button type="button" className="btn btn-primary" disabled={!!next.disabled} onClick={next.onClick}>{next.label}</button>}
      {next?.disabled && <span className="ui-help">{next.disabled}</span>}
      {actions}
    </div>}
  </section>;
}
