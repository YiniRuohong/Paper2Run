import { useState } from 'react';
import type { DocParseMode, DocParseStatus, PaperText } from '../../../api-client/papers';
import { Disclosure } from '../../../ui/Disclosure';
import ParseProgress from '../../docparse/ParseProgress';
import type { DraftPaper } from '../draft';
import StepFrame, { type StepFeedback, type StepNext } from '../StepFrame';

export interface ParseControls {
  onRetry(): void;
  onChoose(mode: DocParseMode): void;
  onReopenChoice(): void;
  onRefresh(): void;
}

// 01 上传论文: a PDF, or pasted text. Upload, project creation and parsing are
// the page's job (DraftPage); this step only collects the input and shows the
// parse state of the current paper.
export default function PaperStep({ paper, paperText, parseStatus, busy, feedback, next, onUploadPdf, onSubmitText, parse }: {
  paper: DraftPaper | null;
  paperText: PaperText | null;
  parseStatus: DocParseStatus | null;
  busy: boolean;
  feedback: StepFeedback | null;
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
  const unparsed = !!paper?.pdf && paperText?.status === 'none' && !!parseStatus?.mode;

  return <StepFrame guide="选择要复现的论文 PDF，或直接粘贴正文。" feedback={feedback} next={next}>
    {paper && <p className="flow-current">当前论文：<b>{paper.title}</b>{paper.pageCount ? ` · ${paper.pageCount} 页` : ''}{paper.pdf ? '' : ' · 粘贴的正文'}</p>}
    {paper?.pdf && <ParseProgress
      docparse={{ pdf: true, needsChoice: parseStatus?.mode === null, status: parseStatus, text: paperText }}
      busy={busy}
      onRetry={parse.onRetry}
      onChoose={parse.onChoose}
      onReopenChoice={parse.onReopenChoice}
      onRefresh={parse.onRefresh}
    />}
    {unparsed && <div className="flow-actions"><button type="button" className="btn btn-secondary" disabled={busy} onClick={parse.onRetry}>开始解析</button></div>}

    <div className="ui-form">
      <label className="ui-field"><span className="ui-field-label">{paper ? '换一篇 PDF' : '论文 PDF'}</span>
        <input key={fileInput} type="file" accept="application/pdf,.pdf" disabled={busy} onChange={e => setFile(e.target.files?.[0] ?? null)} />
      </label>
      <div className="ui-field">
        <span className="ui-field-label" />
        <div className="flow-actions">
          <button type="button" className="btn btn-secondary" disabled={busy || !file} onClick={() => {
            if (!file) return;
            void onUploadPdf(file).then(ok => { if (ok) { setFile(null); setFileInput(n => n + 1); } });
          }}>上传并解析</button>
        </div>
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
