import { useState } from 'react';
import type { DocParseStatus, PaperText } from '../../../api-client/papers';
import { canParseNow } from '../../docparse/docparse';
import { Disclosure } from '../../../ui/Disclosure';
import { Icon } from '../../../ui/Icon';
import ParseProgress from '../../docparse/ParseProgress';
import type { DraftPaper } from '../draft';
import StepFrame, { type StepNext } from '../StepFrame';
import { SETUP_TITLES } from '../timeline';

export interface ParseControls {
  /** Parse the current PDF again (downloads the local component first when it is missing). */
  onRetry(): void;
  /** Start (or restart) the local component download; the paper is parsed when it is ready. */
  onInstall(): void;
  onRefresh(): void;
}

// 01 上传论文: a PDF, or pasted text. Upload, project creation and parsing are
// the page's job (DraftPage); this step only collects the input and shows the
// parse state of the current paper.
export default function PaperStep({ paper, paperText, parseStatus, busy, next, onUploadPdf, onSubmitText, parse }: {
  paper: DraftPaper | null;
  paperText: PaperText | null;
  parseStatus: DocParseStatus | null;
  busy: boolean;
  next: StepNext;
  /** Resolve true when accepted, so the inputs can be cleared. */
  onUploadPdf(file: File): Promise<boolean>;
  onSubmitText(input: { text: string; titleHint?: string }): Promise<boolean>;
  parse: ParseControls;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [fileInput, setFileInput] = useState(0);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  // A PDF that was uploaded but never parsed (e.g. the parse call failed) needs a way to start it.
  const unparsed = !!paper?.pdf && paperText?.status === 'none' && !!parseStatus && canParseNow(parseStatus);

  return <StepFrame title={SETUP_TITLES.paper} guide="选择要复现的论文 PDF，或直接粘贴正文。" next={next}>
    {paper && <p className="flow-current">当前论文：<b>{paper.title}</b>{paper.pageCount ? ` · ${paper.pageCount} 页` : ''}{paper.pdf ? '' : ' · 粘贴的正文'}</p>}
    {paper?.pdf && <ParseProgress
      docparse={{ pdf: true, status: parseStatus, text: paperText }}
      busy={busy}
      onRetry={parse.onRetry}
      onInstall={parse.onInstall}
      onRefresh={parse.onRefresh}
    />}
    {unparsed && <div className="flow-actions"><button type="button" className="btn btn-secondary" disabled={busy} onClick={parse.onRetry}>开始解析</button></div>}

    <div className={`flow-drop${file ? ' has-file' : ''}${paper ? ' is-compact' : ''}`}>
      <span className="flow-drop-icon" aria-hidden="true"><Icon name={file ? 'doc' : 'upload'} size={26} /></span>
      <span className="flow-drop-title">{file ? '已选择' : paper ? '换一篇 PDF' : '选择要复现的论文 PDF'}</span>
      {file && <span className="flow-file-name" title={file.name}>{file.name}</span>}
      {!file && <span className="flow-drop-sub">仅支持 .pdf 文件</span>}
      <div className="flow-actions flow-drop-actions">
        <label className={`btn btn-secondary flow-file-pick${busy ? ' is-disabled' : ''}`}>
          <input key={fileInput} className="flow-file-input" type="file" accept="application/pdf,.pdf" disabled={busy} onChange={e => setFile(e.target.files?.[0] ?? null)} />
          选择 PDF
        </label>
        <button type="button" className="btn btn-primary" disabled={busy || !file} onClick={() => {
          if (!file) return;
          void onUploadPdf(file).then(ok => { if (ok) { setFile(null); setFileInput(n => n + 1); } });
        }}>上传并解析</button>
      </div>
    </div>

    <Disclosure variant="sub" title="没有 PDF？粘贴正文">
      <div className="ui-form">
        <label className="ui-field"><span className="ui-field-label">标题（可选）</span>
          <input value={title} disabled={busy} onChange={e => setTitle(e.target.value)} />
        </label>
        <label className="ui-field"><span className="ui-field-label">正文</span>
          <textarea rows={6} value={body} disabled={busy} onChange={e => setBody(e.target.value)} placeholder="粘贴论文正文（Markdown 或纯文本）" />
        </label>
        <div className="ui-field">
          <span className="ui-field-label" />
          <div className="flow-actions">
            <button type="button" className="btn btn-secondary" disabled={busy || !body.trim()} onClick={() => {
              void onSubmitText({ text: body.trim(), titleHint: title.trim() || undefined }).then(ok => { if (ok) { setBody(''); setTitle(''); } });
            }}>提交正文</button>
          </div>
        </div>
      </div>
    </Disclosure>
  </StepFrame>;
}
