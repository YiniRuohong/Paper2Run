import type { ReactNode } from 'react';

// Grouped-row building blocks shared by the settings page and its feature
// groups (kept apart from Settings.tsx so a group can import them without a cycle).
export function Group({ title, help, children }: { title: string; help?: string; children: ReactNode }) {
  return <section className="settings-group" aria-label={title}>
    <h2 className="settings-title">{title}</h2>
    <div className="settings-card">{children}</div>
    {help && <p className="ui-help settings-help">{help}</p>}
  </section>;
}

export function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="settings-row">
    <div className="settings-label"><span>{label}</span>{hint && <small>{hint}</small>}</div>
    <div className="settings-value">{children}</div>
  </div>;
}
