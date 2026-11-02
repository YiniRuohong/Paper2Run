import { useEffect, useId, useRef, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import type { DocParseMode, DocParseStatus } from '../../api-client/guided-types';
import './docparse.css';

// Mode choice for PDF parsing. The choice is required, so Escape and clicking
// outside do nothing; the only ways out are an explicit choice or the visible
// 「稍后选择」 button, which closes without choosing (the caller keeps showing
// an inline prompt). Rendered in a portal because the step body animates with
// a transform, which would otherwise break position: fixed.
//   <ModeDialog status={status} busy={busy} onChoose={mode => …} onLater={() => …} />
const FOCUSABLE = 'button:not(:disabled)';

export default function ModeDialog({ status, busy, onChoose, onLater }: {
  status: DocParseStatus | null;
  busy: boolean;
  onChoose: (mode: DocParseMode) => void;
  onLater: () => void;
}) {
  const titleId = useId();
  const descId = useId();
  const dialog = useRef<HTMLDivElement>(null);
  // Offline is only offered where the platform can run it.
  const offlineAvailable = status?.offline.state !== 'unsupported';

  useEffect(() => {
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    dialog.current?.focus();
    return () => { previous?.focus(); };
  }, []);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); return; }
    if (event.key !== 'Tab') return;
    const items = Array.from(dialog.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (items.length === 0) { event.preventDefault(); dialog.current?.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === dialog.current)) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus(); }
  };

  return createPortal(
    <div className="dp-overlay">
      <div ref={dialog} role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descId} tabIndex={-1} className="dp-dialog" onKeyDown={onKeyDown}>
        <h2 id={titleId} className="dp-dialog-title">选择 PDF 解析方式</h2>
        <p id={descId} className="dp-dialog-intro">PDF 需要先解析成文字才能抽取实验方案。请选择一种解析方式，之后可在「设置」中更改。</p>
        <div className="dp-choices">
          <section className="dp-choice" aria-label="在线解析">
            <div>
              <h3>在线解析</h3>
              <p>无需安装，马上可用。论文会上传到 MinerU 官方服务器（mineru.net）解析；超过 20 页会自动分段。</p>
            </div>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => onChoose('online')}>选择在线解析</button>
          </section>
          {offlineAvailable && <section className="dp-choice" aria-label="离线解析">
            <div>
              <h3>离线解析</h3>
              <p>需下载约 1–3 GB（视本机硬件），之后完全在本机解析，不会上传论文。CPU 上解析较慢。</p>
            </div>
            <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => onChoose('offline')}>选择离线解析</button>
          </section>}
        </div>
        <div className="dp-dialog-foot">
          <button type="button" className="btn btn-ghost" disabled={busy} onClick={onLater}>稍后选择</button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
