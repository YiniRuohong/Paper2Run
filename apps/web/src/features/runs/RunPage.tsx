import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getClient, type CodeFile, type Run } from '../../api-client';
import { ErrorBanner, EmptyState, PageLoading, SkeletonBlock } from '../../components/Feedback';
import { ModeTag, RunStatusTag, Tag } from '../../components/StatusTag';
import { formatBytes, formatElapsed, formatMicros } from '../../formatters/format';

const ACTIVE: Run['status'][] = ['queued', 'generating', 'executing', 'repairing', 'verifying', 'cancel_requested'];
const MAX_LOG_LINES = 2000;

export function RunPage() {
  const { runId = '' } = useParams();
  const queryClient = useQueryClient();

  const runQ = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getClient().getRun(runId),
    refetchInterval: (q) => (q.state.data && ACTIVE.includes(q.state.data.status) ? 3000 : false),
  });
  const run = runQ.data;

  const attemptsQ = useQuery({
    queryKey: ['attempts', runId],
    queryFn: () => getClient().listAttempts(runId),
    refetchInterval: (q) => (q.state.data && q.state.data.some((a) => a.status === 'generated' || a.status === 'validating' || a.status === 'running') ? 3000 : false),
  });
  const attempts = useMemo(
    () => [...(attemptsQ.data ?? [])].sort((a, b) => b.attempt_index - a.attempt_index),
    [attemptsQ.data],
  );
  const currentAttempt = attempts.find((a) => a.attempt_index === (run?.attempt_index ?? 1)) ?? attempts[0];

  const filesQ = useQuery({
    queryKey: ['files', currentAttempt?.id],
    queryFn: () => getClient().listFiles(currentAttempt!.id),
    enabled: !!currentAttempt,
  });

  const [selectedFile, setSelectedFile] = useState<CodeFile | null>(null);
  useEffect(() => {
    if (filesQ.data && filesQ.data.length > 0) {
      setSelectedFile((prev) => (prev && filesQ.data.some((f) => f.artifact_id === prev.artifact_id) ? prev : filesQ.data[0]));
    } else {
      setSelectedFile(null);
    }
  }, [filesQ.data]);

  const [viewMode, setViewMode] = useState<'code' | 'patch'>('code');
  const fileTextQ = useQuery({
    queryKey: ['artifact', selectedFile?.artifact_id],
    queryFn: () => getClient().getArtifactText(selectedFile!.artifact_id, 0),
    enabled: !!selectedFile && viewMode === 'code',
    staleTime: 30_000,
  });
  const patchArtifactId = currentAttempt?.patch_artifact_id ?? null;
  const patchQ = useQuery({
    queryKey: ['artifact', patchArtifactId],
    queryFn: () => getClient().getArtifactText(patchArtifactId!, 0),
    enabled: !!patchArtifactId && viewMode === 'patch',
    staleTime: 30_000,
  });

  // 日志：按 offset 追加，仅展示有界视图
  const logArtifactId = currentAttempt?.log_artifact_ids[0] ?? null;
  const [logText, setLogText] = useState('');
  const [logOffset, setLogOffset] = useState(0);
  const [paused, setPaused] = useState(false);
  const termRef = useRef<HTMLDivElement>(null);
  const logQ = useQuery({
    queryKey: ['artifact', logArtifactId, logOffset],
    queryFn: () => getClient().getArtifactText(logArtifactId!, logOffset),
    enabled: !!logArtifactId,
    refetchInterval: (q) => {
      const chunk = q.state.data;
      const active = runQ.data ? ACTIVE.includes(runQ.data.status) : false;
      return chunk && !chunk.eof && active ? 3000 : false;
    },
  });
  useEffect(() => {
    if (logQ.data && logQ.data.offset >= logText.length) {
      setLogText((prev) => (prev + logQ.data.text).slice(-240_000));
      if (!logQ.data.eof) setLogOffset(logQ.data.next_offset);
    }
  }, [logQ.data, logText.length]);
  useEffect(() => {
    if (!paused && termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight;
  }, [logText, paused]);

  useEffect(() => {
    setLogText('');
    setLogOffset(0);
  }, [logArtifactId]);

  const cancelM = useMutation({
    mutationFn: () => getClient().cancelRun(runId),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['run', runId] }),
  });

  if (runQ.isPending) return <PageLoading label="读取运行" />;
  if (runQ.error) return <ErrorBanner error={runQ.error} onRetry={() => void runQ.refetch()} />;
  if (!run) return <EmptyState title="运行不存在" />;

  const canCancel = ACTIVE.includes(run.status) && run.status !== 'cancel_requested';

  return (
    <section className="view-enter">
      <div className="run-head">
        <div className="rh-main">
          <div className="run-title">{run.paper_title ?? '（未知论文）'}</div>
          <div className="run-meta">
            <span>run <b>{run.id}</b></span>
            <span>实验 <b>{run.experiment_id}</b></span>
            <span>版本 <b>rev {run.spec_revision_id.slice(-4)}</b></span>
            <span>attempt <b>{run.attempt_index ?? '-'}</b></span>
            <span>修复 <b>{run.repairs_used}/{run.repairs_limit}</b></span>
            <span>耗时 <b>{formatElapsed(run)}</b></span>
            <span>预算 <b>{formatMicros(run.budget.actual_micros)}</b>（预留 {formatMicros(run.budget.reserved_micros)}）</span>
            <span>GPU <b>{run.resources.gpu_slot ?? '未分配'}</b></span>
          </div>
        </div>
        <div className="run-actions">
          <RunStatusTag status={run.status} />
          <ModeTag mode={run.mode} />
          {canCancel && (
            <button type="button" className="btn danger sm" disabled={cancelM.isPending} onClick={() => cancelM.mutate()}>
              {cancelM.isPending && <span className="spin"></span>}
              停止运行
            </button>
          )}
          {run.status === 'cancel_requested' && <span className="cancel-note">停止确认中，等待服务端终态</span>}
        </div>
      </div>

      {run.terminal_reason && (
        <div className={`banner ${run.status === 'completed' ? 'ok' : 'err'}`}>
          <div>
            <div className="b-title">{run.terminal_reason.code} · {run.terminal_reason.message}</div>
            <div className="b-sub">终态以服务端返回为准；已产生的日志与补丁证据保留</div>
          </div>
          <div className="b-act">
            {run.report_status === 'ready' && <Link className="btn sm" to={`/runs/${run.id}/report`}>查看报告</Link>}
          </div>
        </div>
      )}
      {run.status === 'awaiting_revision' && (
        <div className="banner err">
          <div>
            <div className="b-title">协议变更，需重新确认实验</div>
            <div className="b-sub">原 Run 不复活；请回到协议页基于最新版本创建新 Run</div>
          </div>
          <div className="b-act">
            <Link className="btn sm" to={`/papers/${run.paper_id}/review?experiment_id=${run.experiment_id}`}>返回实验确认</Link>
          </div>
        </div>
      )}
      {run.status === 'completed' && (run.report_status === 'pending' || run.report_status === 'generating') && (
        <div className="banner">
          <div>
            <div className="b-title">执行结束，报告生成中</div>
            <div className="b-sub">报告为异步产物，完成后在报告页可见</div>
          </div>
          <div className="b-act"><Link className="btn sm" to={`/runs/${run.id}/report`}>打开报告页</Link></div>
        </div>
      )}

      <div className="run-grid">
        {/* 左：文件树 */}
        <div className="pane">
          <div className="pane-h">
            文件 · attempt {currentAttempt?.attempt_index ?? '-'}
            <span className="ph-right">{run.status === 'generating' ? '生成中' : ''}</span>
          </div>
          <div className="pane-body">
            {filesQ.isPending ? (
              <div style={{ padding: 8 }}><SkeletonBlock lines={5} /></div>
            ) : (filesQ.data ?? []).length === 0 ? (
              <div style={{ padding: '14px 12px' }}>
                <span className="tag dim">{run.status === 'queued' ? '排队中，尚未生成文件' : '尚无文件'}</span>
              </div>
            ) : (
              <ul className="file-tree">
                {filesQ.data!.map((f) => (
                  <li
                    key={f.artifact_id}
                    role="button"
                    tabIndex={0}
                    className={selectedFile?.artifact_id === f.artifact_id && viewMode === 'code' ? 'on' : ''}
                    onClick={() => { setSelectedFile(f); setViewMode('code'); }}
                    onKeyDown={(e) => { if (e.key === 'Enter') { setSelectedFile(f); setViewMode('code'); } }}
                  >
                    <span>{f.path}</span>
                    <span className="f-bytes">{formatBytes(f.bytes)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>

        {/* 中：代码 / 补丁 */}
        <div className="pane">
          <div className="pane-h">
            {viewMode === 'code' ? `代码 · ${selectedFile?.path ?? '未选择文件'}` : '补丁 diff（只读）'}
            {patchArtifactId && (
              <span className="ph-right">
                <button
                  type="button"
                  className="term-foot-btn"
                  style={{ background: 'none', border: 'none', color: viewMode === 'patch' ? 'var(--amber)' : 'var(--faint)', font: 'inherit', cursor: 'pointer' }}
                  onClick={() => setViewMode(viewMode === 'patch' ? 'code' : 'patch')}
                >
                  查看补丁
                </button>
              </span>
            )}
          </div>
          <div className="pane-body">
            {viewMode === 'code' ? (
              fileTextQ.isPending ? (
                <div style={{ padding: 12 }}><SkeletonBlock lines={6} /></div>
              ) : fileTextQ.data ? (
                <pre className="code-view">{fileTextQ.data.text}</pre>
              ) : (
                <div style={{ padding: 16 }}><span className="tag dim">未选择文件</span></div>
              )
            ) : patchQ.isPending ? (
              <div style={{ padding: 12 }}><SkeletonBlock lines={5} /></div>
            ) : patchQ.data ? (
              <pre className="code-view" style={{ paddingLeft: 0, paddingRight: 0 }}>
                {patchQ.data.text.split('\n').map((line, i) => (
                  <span key={i} className={`diff-line ${line.startsWith('+') && !line.startsWith('+++') ? 'add' : line.startsWith('-') && !line.startsWith('---') ? 'del' : 'ctx'}`}>
                    {line || ' '}
                  </span>
                ))}
              </pre>
            ) : (
              <div style={{ padding: 16 }}><span className="tag dim">当前候选无补丁</span></div>
            )}
          </div>
        </div>

        {/* 右：日志 + 修复时间线 */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
          <div className="pane">
            <div className="pane-h">
              日志 · stdout/stderr
              <span className="ph-right">
                <Tag kind="dim">最多显示 {MAX_LOG_LINES} 行</Tag>
              </span>
            </div>
            <div className="term" ref={termRef} aria-live="polite">
              {logText === '' ? (
                <span className="ln-sys">暂无日志输出</span>
              ) : (
                logText.split('\n').slice(-MAX_LOG_LINES).map((line, i) => (
                  <span key={i} className={lineClass(line)}>{line}{'\n'}</span>
                ))
              )}
            </div>
            <div className="term-foot">
              <button type="button" onClick={() => setPaused((p) => !p)}>{paused ? '恢复滚动' : '暂停滚动'}</button>
              <button type="button" onClick={() => { if (termRef.current) termRef.current.scrollTop = termRef.current.scrollHeight; }}>回到底部</button>
              <button type="button" disabled={!logArtifactId} onClick={() => logArtifactId && void getClient().downloadArtifact(logArtifactId)}>下载完整日志</button>
            </div>
          </div>

          <div className="pane">
            <div className="pane-h">候选与修复时间线</div>
            <div className="timeline" style={{ padding: '4px 14px' }}>
              {attempts.length === 0 ? (
                <div style={{ padding: 12 }}><span className="tag dim">尚未生成候选</span></div>
              ) : (
                attempts.map((a) => (
                  <div key={a.id} className={`tl-item ${a.status === 'succeeded' ? 'ok' : a.status === 'failed' ? 'err' : a.status === 'running' ? 'warn' : ''}`}>
                    <span className="tl-dot"></span>
                    <div className="tl-main">
                      <div className="tl-title">
                        候选 #{a.attempt_index}
                        {a.repair_ordinal > 0 ? `（修复第 ${a.repair_ordinal} 次）` : '（初始候选）'}
                      </div>
                      <div className="tl-sub">att {a.id} · code {a.code_hash.slice(0, 10)}… · {aStatusText(a.status)}</div>
                      {a.error_summary && (
                        <div className="tl-sub" style={{ color: 'var(--red)' }}>
                          {a.error_summary.code} · {a.error_summary.message}
                        </div>
                      )}
                      {a.diagnosis_summary && <div className="tl-sub">诊断：{a.diagnosis_summary}</div>}
                      {a.patch_artifact_id && (
                        <div className="tl-sub">
                          <button
                            type="button"
                            className="link-btn"
                            onClick={() => setViewMode('patch')}
                          >
                            查看补丁 {a.patch_artifact_id}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                ))
              )}
              {run.status === 'queued' && (
                <div className="cap-strip" style={{ padding: '8px 2px' }}>排队中：尚未占用 GPU，不假报训练进行</div>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function lineClass(line: string): string {
  if (line.startsWith('[stderr')) return 'ln-err';
  if (line.startsWith('[system')) return 'ln-sys';
  if (line.startsWith('[stdout') && (line.includes('val_err=') || line.includes('epoch='))) return 'ln-ok';
  return '';
}

function aStatusText(s: string): string {
  switch (s) {
    case 'generated': return '已生成';
    case 'validating': return '校验中';
    case 'running': return '运行中';
    case 'failed': return '失败';
    case 'succeeded': return '成功';
    default: return s;
  }
}
