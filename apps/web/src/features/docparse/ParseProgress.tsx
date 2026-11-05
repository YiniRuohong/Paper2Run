import type { DocParseMode } from '../../api-client/papers';
import { StatusWord, type Tone } from '../../ui/Card';
import { Disclosure } from '../../ui/Disclosure';
import { Icon } from '../../ui/Icon';
import { docParseErrorReason, docParseLabel, hasMissingChunks, offlineNotices, otherParseMode, parseActionNotes, type DocParseView } from './docparse';
import './docparse.css';

// Parse status of the current PDF inside the upload step. It only renders what
// the controller state says and offers explicit buttons: 重试 re-requests the
// parse under the current mode, 改用… switches mode on a click, and installing
// the offline component is its own click. Nothing here switches by itself, and
// raw server error text is never shown (docParseErrorReason maps fixed codes).
//   <ParseProgress docparse={state.docparse} busy={busy} onRetry={…} onChoose={…} onReopenChoice={…} />
const BACKEND_LABEL = { online_agent: '在线解析（mineru.net）', offline_mineru: '离线解析（本机）' } as const;
const OFFLINE_UNREADY = new Set(['not_installed', 'failed', 'broken']);

export default function ParseProgress({ docparse, busy, onRetry, onChoose, onReopenChoice, onRefresh }: {
  docparse: DocParseView;
  busy: boolean;
  onRetry: () => void;
  onChoose: (mode: DocParseMode) => void;
  onReopenChoice: () => void;
  onRefresh: () => void;
}) {
  const { pdf, needsChoice, status, text } = docparse;
  if (!pdf) return null;
  const label = docParseLabel(status, text);
  const offline = status?.mode === 'offline' ? status.offline : null;
  const installing = offline?.state === 'installing';
  const needsInstall = offline !== null && OFFLINE_UNREADY.has(offline.state);
  const stuck = offline !== null && (needsInstall || offline.state === 'unsupported');
  const failedText = text?.status === 'failed' ? text : null;
  const readyText = text?.status === 'ready' ? text : null;
  const parsing = text?.status === 'parsing';
  const tone: Tone = readyText ? 'ok' : failedText ? 'bad' : parsing || installing ? 'run' : 'idle';
  const total = text?.chunks.length ?? 0;
  const done = text?.chunks.filter(chunk => chunk.status === 'done').length ?? 0;
  const other = otherParseMode(status);
  const partial = hasMissingChunks(text);
  const actions = failedText !== null || partial || (stuck && readyText === null && !parsing);
  const notices = status?.mode === 'offline' && offline ? offlineNotices(offline) : [];
  const notes = actions ? parseActionNotes({ install: needsInstall, other }) : [];

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
      <progress className="dp-bar" value={offline.progress} max={1} aria-label="离线组件安装进度" />
      <span className="dp-meta">{Math.round(offline.progress * 100)}%</span>
    </div>}
    {failedText && <p className="ui-callout tone-bad" role="alert"><Icon name="alert" /><span>{docParseErrorReason(failedText.error_code)}</span></p>}
    {notices.map(notice => <p key={notice} className="dp-hint" role="status">{notice}</p>)}
    {actions && <div className="flow-actions">
      {failedText && <button type="button" className="btn btn-primary" disabled={busy} onClick={onRetry}>重试</button>}
      {partial && <button type="button" className="btn btn-secondary" disabled={busy} onClick={onRetry}>重试失败页</button>}
      {needsInstall && <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => onChoose('offline')}>安装离线组件</button>}
      {other && <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => onChoose(other)}>{other === 'offline' ? '改用离线解析' : '改用在线解析'}</button>}
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
    {needsChoice && <div className="dp-line">
      <span className="dp-meta">尚未选择解析方式，PDF 解析后才能抽取。</span>
      <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={onReopenChoice}>选择解析方式</button>
    </div>}
  </div>;
}
