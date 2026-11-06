import { useId, type ReactNode } from 'react';

export interface StepFeedback { tone: 'ok' | 'bad' | 'idle'; text: string }
export interface StepNext { label: string; /** Reason the button is disabled; absent when it can be pressed. */ disabled?: string; onClick(): void }
/** Runs a write with the page's busy flag and top error line; resolves false when it failed. */
export type Act = (work: () => Promise<void>) => Promise<boolean>;

// The fixed rhythm of every setup step: the title with a "!" badge whose hover
// or focus bubble holds the guide, the previous step's result, this step's few
// fields, then the button that moves on. The previous step's result is not
// shown here: DraftPage raises it as a toast (StepFeedback).
export default function StepFrame({ title, guide, children, next }: { title: string; guide: string; children: ReactNode; next?: StepNext }) {
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
    {next && <div className="flow-next">
      <button type="button" className="btn btn-primary" disabled={!!next.disabled} onClick={next.onClick}>{next.label}</button>
      {next.disabled && <span className="ui-help">{next.disabled}</span>}
    </div>}
  </section>;
}
