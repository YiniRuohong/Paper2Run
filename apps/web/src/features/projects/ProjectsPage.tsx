import { useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useMutation, useQuery } from '@tanstack/react-query';
import { getClient } from '../../api-client';
import { useProject } from '../../app';
import { EmptyState, ErrorBanner, PageLoading, SkeletonBlock } from '../../components/Feedback';
import { ModeTag, PaperStatusTag, RunStatusTag } from '../../components/StatusTag';
import { formatBytes, timeAgo } from '../../formatters/format';

const CONSENT_TEXT = '我已获得论文使用授权，并同意将论文内容提交本系统处理（解析与复现计划），内容不外传、不出现在公开页面。';

export function ProjectsPage() {
  const project = useProject();
  const navigate = useNavigate();
  const fileRef = useRef<HTMLInputElement>(null);
  const [consent, setConsent] = useState(false);
  const [arxiv, setArxiv] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [uploadError, setUploadError] = useState<unknown>(null);

  const capabilitiesQ = useQuery({
    queryKey: ['capabilities'],
    queryFn: () => getClient().getCapabilities(),
    staleTime: 60_000,
  });
  const runsQ = useQuery({
    queryKey: ['runs', project?.id],
    queryFn: () => getClient().listRuns(project!.id),
    enabled: !!project,
    refetchInterval: 5000,
  });
  const papersQ = useQuery({
    queryKey: ['papers', project?.id],
    queryFn: () => getClient().listPapers(project!.id),
    enabled: !!project,
    refetchInterval: (q) => (q.state.data?.items.some((p) => p.status === 'parsing') ? 3000 : false),
  });

  const upload = useMutation({
    mutationFn: async () => {
      if (!project) throw new Error('no project');
      const file = fileRef.current?.files?.[0] ?? undefined;
      const raw = arxiv.trim();
      const arxivId = raw ? extractArxivId(raw) : undefined;
      return getClient().uploadPaper(project.id, { file, arxivId: arxivId ?? undefined, consent });
    },
    onSuccess: (accepted) => {
      navigate(`/papers/${accepted.paper_id}/review`);
    },
    onError: (e) => setUploadError(e),
  });

  if (!project) return <PageLoading label="读取项目" />;
  const caps = capabilitiesQ.data;

  function extractArxivId(raw: string): string | null {
    const m = raw.match(/(\d{4}\.\d{4,5}(v\d+)?)/);
    return m ? m[1] : null;
  }

  function validateFile(file: File): string | null {
    if (!caps) return null;
    if (!/\.pdf$/i.test(file.name)) return '仅支持 PDF 文件';
    if (file.size > caps.upload.max_pdf_bytes) {
      return `文件超过上限 ${Math.floor(caps.upload.max_pdf_bytes / 1024 / 1024)} MiB`;
    }
    return null;
  }

  function onPickFile(file: File | undefined) {
    setUploadError(null);
    if (!file) return;
    const err = validateFile(file);
    if (err) {
      setUploadError(new Error(err));
      if (fileRef.current) fileRef.current.value = '';
      setFileName(null);
      return;
    }
    setFileName(file.name);
  }

  function submit(kind: 'file' | 'arxiv') {
    setUploadError(null);
    if (!consent) return;
    if (kind === 'file') {
      const file = fileRef.current?.files?.[0];
      if (!file) return;
    } else if (!extractArxivId(arxiv.trim())) {
      setUploadError(new Error('请输入规范 arXiv ID，例如 1512.03385v1，或粘贴 arxiv.org 链接'));
      return;
    }
    upload.mutate();
  }

  const runs = runsQ.data?.items ?? [];
  const papers = papersQ.data?.items ?? [];
  const consentMissing = !consent;

  return (
    <section className="view-enter">
      <div className="hero">
        <div className="hero-main">
          <h1>把论文，<span className="accent">变成能跑的实验。</span></h1>
          <p className="hero-sub">
            上传 PDF，系统解析方法、生成代码、在隔离环境运行并按需修复，最后交出与原文对照的复现报告。
          </p>
        </div>
        {caps && (
          <aside className="hero-caps" aria-label="当前能力范围">
            <div className="cap-row">
              <span>支持范围</span>
              <b>{caps.recipes.map((r) => r.label).join('；')}</b>
            </div>
            <div className="cap-row">
              <span>上传</span>
              <b>
                ≤ {formatBytes(caps.upload.max_pdf_bytes)} · ≤ {caps.upload.max_pages} 页
                {caps.upload.arxiv_enabled ? ' · 支持 arXiv 导入' : ''}
              </b>
            </div>
            <div className="cap-row">
              <span>限额</span>
              <b>自动修复 ≤ {caps.limits.max_repairs} 次 · 排队 ≤ {caps.limits.max_queued_runs_per_user} 个</b>
            </div>
          </aside>
        )}
      </div>

      <div
        className={`upload${dragOver ? ' drag' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragOver(false);
          const file = e.dataTransfer.files?.[0];
          if (file && fileRef.current) {
            const dt = new DataTransfer();
            dt.items.add(file);
            fileRef.current.files = dt.files;
            onPickFile(file);
          }
        }}
      >
        <div className="upload-icon" aria-hidden="true">P2R</div>
        <div className="upload-main">
          <div className="u-title">拖入论文 PDF，或粘贴 arXiv 链接</div>
          <div className="u-sub">
            {caps
              ? `单文件 ≤ ${formatBytes(caps.upload.max_pdf_bytes)} · ≤ ${caps.upload.max_pages} 页 · 自动修复上限 ${caps.limits.max_repairs} 次`
              : '读取上传能力中…'}
          </div>
          <div className="upload-row">
            <input
              ref={fileRef}
              id="pdf-file"
              type="file"
              accept="application/pdf"
              style={{ position: 'absolute', width: 1, height: 1, opacity: 0 }}
              onChange={(e) => onPickFile(e.target.files?.[0])}
            />
            <label
              htmlFor="pdf-file"
              className="btn"
              style={{ cursor: 'pointer' }}
              onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fileRef.current?.click(); } }}
              tabIndex={0}
              role="button"
            >
              选择 PDF 文件
            </label>
            <input
              className="upload-input"
              style={{ flex: 1 }}
              value={arxiv}
              onChange={(e) => setArxiv(e.target.value)}
              placeholder={caps?.upload.arxiv_enabled ? 'arxiv.org/abs/1512.03385 或直接输入 ID' : 'arXiv 导入未开放'}
              disabled={!caps?.upload.arxiv_enabled}
              aria-label="arXiv 链接或 ID"
            />
            <button
              type="button"
              className="btn primary"
              disabled={consentMissing || (!fileName && !arxiv.trim()) || upload.isPending}
              title={consentMissing ? '请先勾选内容处理同意项' : undefined}
              onClick={() => submit(fileName ? 'file' : 'arxiv')}
            >
              {upload.isPending && <span className="spin"></span>}
              {upload.isPending ? '上传中' : '开始复现'}
            </button>
          </div>
          {fileName && <div className="cap-strip">已选择：{fileName}</div>}
          <label className="consent-row">
            <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
            <span>{CONSENT_TEXT}</span>
          </label>
          {(uploadError || upload.error) && (
            <div className="field-error">
              {uploadError instanceof Error || upload.error instanceof Error
                ? (uploadError as Error)?.message ?? (upload.error as Error).message
                : '上传失败'}
            </div>
          )}
        </div>
      </div>

      <div className="section-h">
        <h2>最近论文</h2>
        <span className="sub">解析状态 · 点击进入确认页</span>
      </div>
      {papersQ.isPending ? (
        <SkeletonBlock lines={3} />
      ) : papers.length === 0 ? (
        <EmptyState title="还没有上传过论文" sub="从上方拖入第一篇论文 PDF 开始复现流程。" />
      ) : (
        <div className="table-wrap"><table className="recent">
          <thead>
            <tr><th>论文</th><th>状态</th><th>页数</th><th>上传时间</th><th></th></tr>
          </thead>
          <tbody>
            {papers.map((p) => (
              <tr key={p.paper_id}>
                <td className="paper-title-cell">
                  {p.title ?? '（标题解析中）'}
                  <span className="pid">{p.arxiv_version ? `arXiv:${p.arxiv_version} · ` : ''}{p.paper_id}</span>
                </td>
                <td><PaperStatusTag status={p.status} /></td>
                <td className="mono-cell">{p.page_count ?? '未知'}</td>
                <td className="mono-cell">{timeAgo(p.created_at)}</td>
                <td>
                  <Link className="btn sm" to={`/papers/${p.paper_id}/review`}>
                    {p.status === 'failed' ? '打开诊断' : '打开解析'}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}

      <div className="section-h">
        <h2>最近运行</h2>
        <span className="sub">状态 / 模式 / 修复数（无批量评分接口，不显示分数列）</span>
      </div>
      {runsQ.isPending ? (
        <SkeletonBlock lines={3} />
      ) : runsQ.error ? (
        <ErrorBanner error={runsQ.error} onRetry={() => void runsQ.refetch()} />
      ) : runs.length === 0 ? (
        <EmptyState title="还没有运行任务" sub="完成论文解析与实验确认后，这里会出现运行记录。" />
      ) : (
        <div className="table-wrap"><table className="recent">
          <thead>
            <tr><th>论文</th><th>状态</th><th>模式</th><th>开始</th><th>修复</th><th>预算</th><th></th></tr>
          </thead>
          <tbody>
            {runs.map((r) => {
              const terminal = ['completed', 'failed', 'cancelled', 'awaiting_revision'].includes(r.status);
              return (
                <tr key={r.id}>
                  <td className="paper-title-cell">
                    {r.paper_title ?? '（未知论文）'}
                    <span className="pid">{r.id}</span>
                  </td>
                  <td><RunStatusTag status={r.status} /></td>
                  <td><ModeTag mode={r.mode} /></td>
                  <td className="mono-cell">{timeAgo(r.started_at ?? r.created_at)}</td>
                  <td className="mono-cell">{r.repairs_used}/{r.repairs_limit}</td>
                  <td className="mono-cell">
                    {r.budget.actual_micros === null && r.budget.reserved_micros > 0
                      ? `预留 ¥${(r.budget.reserved_micros / 1e6).toFixed(2)}`
                      : r.budget.actual_micros !== null
                        ? `¥${(r.budget.actual_micros / 1e6).toFixed(2)}`
                        : '未知'}
                  </td>
                  <td>
                    {terminal && r.report_status === 'ready' ? (
                      <Link className="btn sm" to={`/runs/${r.id}/report`}>查看报告</Link>
                    ) : (
                      <Link className="btn sm" to={`/runs/${r.id}`}>进入工作台</Link>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table></div>
      )}
    </section>
  );
}
