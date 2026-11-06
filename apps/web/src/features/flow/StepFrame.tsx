import type { ReactNode } from 'react';
import { Icon } from '../../ui/Icon';

export interface StepFeedback { tone: 'ok' | 'bad' | 'idle'; text: string }
export interface StepNext { label: string; /** Reason the button is disabled; absent when it can be pressed. */ disabled?: string; onClick(): void }
/** Runs a write with the page's busy flag and top error line; resolves false when it failed. */
export type Act = (work: () => Promise<void>) => Promise<boolean>;

// The fixed rhythm of every setup step: one guide line, the previous step's
// result, this step's few fields, then the button that moves on.
export default function StepFrame({ guide, feedback, children, next }: { guide: string; feedback: StepFeedback | null; children: ReactNode; next?: StepNext }) {
  return <section className="flow-step">
    <p className="flow-guide">{guide}</p>
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
