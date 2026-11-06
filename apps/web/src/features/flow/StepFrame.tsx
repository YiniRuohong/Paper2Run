import { useId, type ReactNode } from 'react';
import { Icon } from '../../ui/Icon';

export interface StepFeedback { tone: 'ok' | 'bad' | 'idle'; text: string }
export interface StepNext { label: string; /** Reason the button is disabled; absent when it can be pressed. */ disabled?: string; onClick(): void }
/** Runs a write with the page's busy flag and top error line; resolves false when it failed. */
export type Act = (work: () => Promise<void>) => Promise<boolean>;

// The fixed rhythm of every setup step: the title with a "!" badge whose hover
// or focus bubble holds the guide, the previous step's result, this step's few
// fields, then the button that moves on.
export default function StepFrame({ title, guide, feedback, children, next }: { title: string; guide: string; feedback: StepFeedback | null; children: ReactNode; next?: StepNext }) {
  const tipId = useId();
  return <section className="flow-step">
    <div className="flow-head">
      <h2 className="flow-title">{title}</h2>
      <span className="flow-hint-wrap">
        <button type="button" className="flow-hint" aria-label="本步说明" aria-describedby={tipId}>!</button>
        <span id={tipId} role="tooltip" className="flow-tip">{guide}</span>
      </span>
    </div>
    {feedback && <p className={`ui-callout tone-${feedback.tone}`} role="status">
      <Icon name={feedback.tone === 'ok' ? 'check-circle' : feedback.tone === 'bad' ? 'alert' : 'info'} /><span>{feedback.text}</span>
    </p>}
    <div className="flow-fields">{children}</div>
    {next && <div className="flow-next">
      <button type="button" className="btn btn-primary" disabled={!!next.disabled} onClick={next.onClick}>{next.label}</button>
      {next.disabled && <span className="ui-help">{next.disabled}</span>}
    </div>}
  </section>;
}
