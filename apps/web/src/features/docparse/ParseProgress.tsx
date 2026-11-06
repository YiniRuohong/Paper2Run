import { StatusWord, type Tone } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { OFFLINE_UNREADY, docParseErrorReason, docParseLabel, hasMissingChunks, offlineNotices, parseActionNotes, type DocParseView } from './docparse';
import './docparse.css';

// Parse status of the current PDF inside the upload step. PDFs are parsed only
// on this machine; the page downloads the local component the first time. This
// renders what the state says and offers explicit buttons: 重试 asks for the
// parse again, 下载本地解析组件 starts (or restarts) the component download.
// Raw server error text is never shown (docParseErrorReason maps fixed codes).
//   <ParseProgress docparse={view} busy={busy} onRetry={…} onInstall={…} onRefresh={…} />
const BACKEND_LABEL = { online_agent: '在线解析（mineru.net）', offline_mineru: '本地解析（本机）' } as const;

export default function ParseProgress({ docparse, busy, onRetry, onInstall, onRefresh }: {
  docparse: DocParseView;
  busy: boolean;
  onRetry: () => void;
  onInstall: () => void;
  onRefresh: () => void;
}) {
  const { pdf, status, text } = docparse;
  if (!pdf) return null;
  const label = docParseLabel(status, text);
  const offline = status?.mode === 'offline' ? status.offline : null;
  const installing = offline?.state === 'installing';
  const needsInstall = offline !== null && OFFLINE_UNREADY.has(offline.state);
  const failedText = text?.status === 'failed' ? text : null;
  const readyText = text?.status === 'ready' ? text : null;
  const parsing = text?.status === 'parsing';
  const tone: Tone = readyText ? 'ok' : failedText ? 'bad' : parsing || installing ? 'run' : 'idle';
  const total = text?.chunks.length ?? 0;
  const done = text?.chunks.filter(chunk => chunk.status === 'done').length ?? 0;
  const partial = hasMissingChunks(text);
  const actions = failedText !== null || partial || (needsInstall && readyText === null && !parsing);
  const notices = offline ? offlineNotices(offline) : [];
  const notes = actions ? parseActionNotes({ install: needsInstall }) : [];

  return <div className="dp-progress">
    <div className="dp-line" role="status" aria-live="polite">
      <StatusWord tone={tone}>{label}</StatusWord>
      {text && text.status !== 'none' && text.page_count > 0 && <span className="dp-meta">已解析 {text.pages_parsed}/{text.page_count} 页</span>}
      {text?.backend && <span className="dp-meta">{BACKEND_LABEL[text.backend]}</span>}
    </div>
    {parsing && <div className="dp-line">
      {total > 0 && <progress className="dp-bar" value={done} max={total} aria-label="解析进度" />}
      <button type="button" className="btn btn-ghost" disabled={busy} onClick={onRefresh}><Icon name="refresh" size={14} />刷新解析状态</button>
    </div>}
    {installing && offline && <div className="dp-line">
      <progress className="dp-bar" value={offline.progress} max={1} aria-label="本地解析组件下载进度" />
      <span className="dp-meta">{Math.round(offline.progress * 100)}% · 完成后自动开始解析</span>
    </div>}
    {failedText && <p className="ui-callout tone-bad" role="alert"><Icon name="alert" /><span>{docParseErrorReason(failedText.error_code)}</span></p>}
    {notices.map(notice => <p key={notice} className="dp-hint" role="status">{notice}</p>)}
    {actions && <div className="flow-actions">
      {failedText && !needsInstall && <button type="button" className="btn btn-primary" disabled={busy} onClick={onRetry}>重试</button>}
      {partial && <button type="button" className="btn btn-secondary" disabled={busy} onClick={onRetry}>重试失败页</button>}
      {needsInstall && <button type="button" className="btn btn-primary" disabled={busy} onClick={onInstall}>下载本地解析组件</button>}
    </div>}
    {notes.length > 0 && <p className="dp-hint">{notes.join('；')}</p>}
    {readyText && readyText.warnings.length > 0 && <div className="ui-callout tone-warn" role="status">
      <Icon name="alert" />
      <div className="dp-warnings">
        <strong>解析有提示，结果可能不完整</strong>
        <ul>{readyText.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>
      </div>
    </div>}
    {readyText?.preview && <Disclosure variant="sub" title="解析预览">
      <pre className="ui-code" aria-label="解析出的 Markdown 预览（只读）">{readyText.preview}</pre>
    </Disclosure>}
  </div>;
}
