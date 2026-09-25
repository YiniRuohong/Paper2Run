import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  getClient, type CurveSeries, type ExportFormat, type Report, type Run,
} from '../../api-client';
import { ErrorBanner, EmptyState, PageLoading, SkeletonBlock } from '../../components/Feedback';
import {
  EXPORT_FORMAT_TEXT, ExportItemStatusTag, ExportStatusTag, ModeTag, VerdictTag,
} from '../../components/StatusTag';
import {
  formatBytes, formatDelta, formatLocalTime, formatMetricValue,
  formatMicros, formatRatioScore, formatTolerance,
} from '../../formatters/format';

const ALL_FORMATS: ExportFormat[] = ['bundle_zip', 'report_pdf', 'report_markdown', 'report_json'];

export function ReportPage() {
  const { runId = '' } = useParams();
  const queryClient = useQueryClient();
  const [formats, setFormats] = useState<ExportFormat[]>(['bundle_zip', 'report_json']);
  const [exportError, setExportError] = useState<unknown>(null);

  const runQ = useQuery({
    queryKey: ['run', runId],
    queryFn: () => getClient().getRun(runId),
    refetchInterval: (q) => (q.state.data && ['queued', 'generating', 'executing', 'repairing', 'verifying', 'cancel_requested'].includes(q.state.data.status) ? 3000 : false),
  });
  const reportQ = useQuery({
    queryKey: ['report', runId],
    queryFn: () => getClient().getReport(runId),
    retry: false,
    refetchInterval: (q) => {
      const d = q.state.data;
      if (d && d.kind === 'pending') return Math.max(2000, d.pending.retry_after_ms);
      return false;
    },
  });
  const manifestQ = useQuery({
    queryKey: ['manifest', runId],
    queryFn: () => getClient().getManifest(runId),
    enabled: reportQ.data?.kind === 'report',
    retry: false,
  });
  const curvesQ = useQuery({
    queryKey: ['curves', runId],
    queryFn: () => getClient().getCurves(runId),
    enabled: reportQ.data?.kind === 'report',
  });
  const exportsQ = useQuery({
    queryKey: ['exports', runId],
    queryFn: () => getClient().listExports(runId),
    retry: false,
    refetchInterval: (q) => {
      const list = q.state.data;
      return list && list.some((e) => e.status === 'queued' || e.status === 'running' || e.items.some((it) => it.status === 'pending' || it.status === 'running')) ? 3000 : false;
    },
  });

  const createExportM = useMutation({
    mutationFn: () => getClient().createExport(runId, { formats }),
    onSuccess: () => {
      setExportError(null);
      void queryClient.invalidateQueries({ queryKey: ['exports', runId] });
    },
    onError: (e) => setExportError(e),
  });

  if (runQ.isPending || reportQ.isPending) return <PageLoading label="读取报告" />;
  if (reportQ.error) return <ErrorBanner error={reportQ.error} onRetry={() => void reportQ.refetch()} />;

  const reportRes = reportQ.data;
  if (reportRes.kind === 'pending') {
    return (
      <section className="view-enter">
        <div className="pending-card">
          <div className="pc-title">
            {reportRes.pending.status === 'waiting_for_run' ? '执行尚未结束' : '执行结束，报告生成中'}
          </div>
          <div className="pc-sub">
            {reportRes.pending.operation_id ? `操作 ${reportRes.pending.operation_id} · ` : ''}
            约 {Math.round(reportRes.pending.retry_after_ms / 1000)} 秒后自动重试；报告就绪前不显示任何分数
          </div>
          <div className="pc-sub" style={{ marginTop: 6 }}>
            <button type="button" className="btn sm" onClick={() => void reportQ.refetch()}>立即刷新</button>
          </div>
        </div>
      </section>
    );
  }
  const report = reportRes.report;
  const manifest = manifestQ.data;
  const curves = curvesQ.data;

  return (
    <section className="view-enter">
      <VerdictCard report={report} run={runQ.data ?? null} />
      <div className="report-grid">
        <div>
          <div className="section-h">
            <h2>指标对照</h2>
            <span className="sub">差异、容差与比较资格均由独立评测器计算</span>
          </div>
          <div className="table-wrap"><table className="metrics">
            <thead>
              <tr><th>指标</th><th>原文值</th><th>实测值</th><th>差异</th><th>容差</th><th>比较资格</th></tr>
            </thead>
            <tbody>
              {report.metrics.map((m) => (
                <tr key={m.id}>
                  <td>{m.name}</td>
                  <td className={m.reference_value === null ? 'null-cell num' : 'num'}>{formatMetricValue(m.reference_value, m.unit)}</td>
                  <td className={m.observed_value === null ? 'null-cell num' : 'num'}>{formatMetricValue(m.observed_value, m.unit)}</td>
                  <td className={m.delta_absolute === null ? 'null-cell num' : `num ${m.within_tolerance === false ? 'neg' : m.within_tolerance === true ? 'pos' : ''}`}>
                    {formatDelta(m.delta_absolute, m.unit)}
                  </td>
                  <td className="num">{formatTolerance(m.tolerance, m.unit)}</td>
                  <td>
                    {m.comparable
                      ? (m.within_tolerance === null ? '未判定' : m.within_tolerance ? '在容差内' : '超出容差')
                      : '不可比较'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div>

          <div className="section-h">
            <h2>过程曲线</h2>
            <span className="sub">过程观测，最终指标以报告为准</span>
          </div>
          {curvesQ.isPending ? (
            <SkeletonBlock lines={4} />
          ) : (curves?.series.length ?? 0) === 0 ? (
            <EmptyState title="暂无可展示曲线" sub={curves?.warnings.join('；') || '该运行未返回曲线数据，不绘制填充曲线。'} />
          ) : (
            <ChartBox series={curves!.series} warnings={curves!.warnings} />
          )}

          {report.limitations.length > 0 && (
            <>
              <div className="section-h"><h2>已知限制</h2></div>
              <div className="ext-card">
                <ul style={{ listStyle: 'none' }}>
                  {report.limitations.map((l, i) => (
                    <li key={i} className="cap-strip" style={{ padding: '4px 0' }}>· {l}</li>
                  ))}
                </ul>
              </div>
            </>
          )}
        </div>

        <div>
          <div className="section-h"><h2>复现声明</h2></div>
          {manifestQ.isPending ? (
            <SkeletonBlock lines={5} />
          ) : manifest ? (
            <div className="ext-card">
              <dl className="manifest-list">
                <div className="m-row"><dt>声明状态</dt><dd>{manifest.status === 'finalized' ? '已定稿' : 'provisional'}</dd></div>
                <div className="m-row"><dt>spec hash</dt><dd>{manifest.spec_hash.slice(0, 16)}…</dd></div>
                <div className="m-row"><dt>论文 hash</dt><dd>{manifest.paper_sha256.slice(0, 16)}…</dd></div>
                <div className="m-row"><dt>代码 hash</dt><dd>{manifest.code_sha256?.slice(0, 16) ?? 'null'}…</dd></div>
                <div className="m-row"><dt>镜像</dt><dd>{manifest.image_digest ?? 'null（未提供）'}</dd></div>
                <div className="m-row"><dt>随机种子</dt><dd>{manifest.seed ?? 'null'}</dd></div>
                <div className="m-row"><dt>数据集</dt><dd>{manifest.dataset ? `${manifest.dataset.id} ${manifest.dataset.version} · split ${manifest.dataset.split_sha256.slice(0, 10)}…` : 'null'}</dd></div>
                <div className="m-row"><dt>环境</dt><dd>Python {manifest.environment.python ?? '?'} · Torch {manifest.environment.torch ?? '?'} · CUDA {manifest.environment.cuda ?? '?'} · {manifest.environment.gpu ?? '?'}</dd></div>
                <div className="m-row"><dt>评测器</dt><dd>{manifest.evaluator_version ?? 'null'}</dd></div>
                {manifest.models.map((m) => (
                  <div key={m.purpose} className="m-row"><dt>模型 · {m.purpose}</dt><dd>{m.model_id} · prompt {m.prompt_version}</dd></div>
                ))}
                <div className="m-row"><dt>产物</dt><dd>{manifest.artifacts.length} 个（代码清单 / 日志 / 指标）</dd></div>
              </dl>
              <div className="cap-strip" style={{ marginTop: 10 }}>报告生成时间 {formatLocalTime(report.created_at)} · 报告 {report.id} v{report.version}</div>
            </div>
          ) : (
            <EmptyState title="复现声明不可用" sub={manifestQ.error ? '请稍后重试' : '尚未生成'} />
          )}

          <div className="section-h"><h2>导出</h2><span className="sub">各格式独立状态</span></div>
          <div className="ext-card">
            {exportsQ.error ? (
              <ErrorBanner error={exportsQ.error} onRetry={() => void exportsQ.refetch()} />
            ) : (exportsQ.data ?? []).length === 0 ? (
              <div className="cap-strip" style={{ marginBottom: 10 }}>尚无导出记录</div>
            ) : (
              (exportsQ.data ?? []).map((e) => (
                <div key={e.id} style={{ marginBottom: 12 }}>
                  <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
                    <ExportStatusTag status={e.status} />
                    <span className="ei-name">{e.id}</span>
                    <span className="ei-meta">{formatLocalTime(e.created_at)}</span>
                  </div>
                  {e.items.map((it) => (
                    <div key={it.format} className="export-item">
                      <span className="ei-name">{EXPORT_FORMAT_TEXT[it.format]}</span>
                      <ExportItemStatusTag status={it.status} />
                      {it.status === 'ready' && it.artifact_id ? (
                        <button
                          type="button"
                          className="btn sm"
                          onClick={() => void getClient().downloadArtifact(it.artifact_id!)}
                        >
                          下载（{formatBytes(it.bytes)}）
                        </button>
                      ) : it.error ? (
                        <span className="field-error" style={{ margin: 0 }}>{it.error.code}</span>
                      ) : null}
                    </div>
                  ))}
                  {e.items.some((it) => it.error) && (
                    <div className="cap-strip">
                      失败项：{e.items.filter((it) => it.error).map((it) => `${EXPORT_FORMAT_TEXT[it.format]}（${it.error!.message}）`).join('；')}
                      ，不影响已就绪格式下载
                    </div>
                  )}
                </div>
              ))
            )}

            <div className="issue-list" style={{ marginTop: 8 }}>
              {ALL_FORMATS.map((f) => (
                <label key={f} className="issue-item">
                  <input
                    type="checkbox"
                    checked={formats.includes(f)}
                    onChange={(e) =>
                      setFormats((list) => (e.target.checked ? [...list, f] : list.filter((x) => x !== f)))
                    }
                  />
                  <div>{EXPORT_FORMAT_TEXT[f]}</div>
                </label>
              ))}
            </div>
            {exportError != null && <ErrorBanner error={exportError} />}
            <div className="action-bar" style={{ marginTop: 10 }}>
              <button
                type="button"
                className="btn primary"
                disabled={formats.length === 0 || createExportM.isPending || !manifest || manifest.status !== 'finalized'}
                title={!manifest || manifest.status !== 'finalized' ? '复现声明未定稿，暂不能导出' : undefined}
                onClick={() => createExportM.mutate()}
              >
                {createExportM.isPending && <span className="spin"></span>}
                创建导出
              </button>
              <span className="ab-note">导出由后端生成；viewer 仅能下载已就绪文件</span>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function VerdictCard({ report, run }: { report: Report; run: Run | null }) {
  const kind = report.verdict === 'matched' ? 'ok' : report.verdict === 'outside_tolerance' ? 'warn' : report.verdict === 'execution_failed' ? 'err' : '';
  return (
    <div className="verdict-card">
      <div className="v-mode">复现结论 · 实验模式</div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <ModeTag mode={report.mode} />
        <VerdictTag verdict={report.verdict} />
        {report.data_mode === 'mock' && <span className="mode-tag">MOCK</span>}
      </div>
      <div className={`v-main ${kind}`}>{verdictHeadline(report)}</div>
      {report.comparison_reasons.length > 0 && (
        <div className="v-reasons">不可比原因：{report.comparison_reasons.join('、')}</div>
      )}
      <div className="score-row">
        <div className="score-cell">
          <div className="s-label">总分 TOTAL</div>
          <div className={`s-val ${report.score.total === null ? 'null' : ''}`}>
            {report.score.total === null ? '不可比较' : report.score.total}
          </div>
        </div>
        <div className="score-cell">
          <div className="s-label">指标吻合</div>
          <div className={`s-val ${report.score.metric_match === null ? 'null' : ''}`}>
            {formatRatioScore(report.score.metric_match)}
          </div>
        </div>
        <div className="score-cell">
          <div className="s-label">过程</div>
          <div className="s-val">{formatRatioScore(report.score.process)}</div>
        </div>
        <div className="score-cell">
          <div className="s-label">环境</div>
          <div className="s-val">{formatRatioScore(report.score.environment)}</div>
        </div>
        <div className="score-cell">
          <div className="s-label">评分策略</div>
          <div className="s-val" style={{ fontSize: 13 }}>{report.score.policy_version}</div>
        </div>
      </div>
      <div className="cap-strip" style={{ marginTop: 12 }}>
        本次预算消耗 {formatMicros(run?.budget.actual_micros ?? null)} · 评分口径 process/environment 为 0-1，total 为 0-100
      </div>
    </div>
  );
}

function verdictHeadline(report: Report): string {
  switch (report.verdict) {
    case 'matched': return '选定指标在约定容差内';
    case 'outside_tolerance': return '选定指标未达约定容差';
    case 'not_comparable': return '条件不同，不可直接比较';
    case 'execution_failed': return '未完成执行';
    case 'insufficient_evidence': return '证据不足，无法给出结论';
  }
}

function ChartBox({ series, warnings }: { series: CurveSeries[]; warnings: string[] }) {
  // 同单位同横轴才叠加；异单位/异横轴分组并排
  const groups = new Map<string, CurveSeries[]>();
  for (const s of series) {
    const key = `${s.unit}|${s.x_axis}`;
    groups.set(key, [...(groups.get(key) ?? []), s]);
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {[...groups.entries()].map(([key, list]) => (
        <div key={key} className="chart-box">
          <SingleChart unit={key.split('|')[0]} xAxis={key.split('|')[1]} series={list} />
        </div>
      ))}
      {warnings.length > 0 && <div className="cap-strip">{warnings.join('；')}</div>}
    </div>
  );
}

function SingleChart({ unit, xAxis, series }: { unit: string; xAxis: string; series: CurveSeries[] }) {
  const W = 640, H = 280, PL = 48, PB = 30, PT = 16, PR = 16;
  const xs = series.flatMap((s) => s.points.map((p) => p.x));
  const ys = series.flatMap((s) => s.points.map((p) => p.y ?? NaN)).filter((v) => !Number.isNaN(v));
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.08 || 1;
  const yLo = y0 - pad, yHi = y1 + pad;
  const px = (x: number) => PL + ((x - x0) / (x1 - x0 || 1)) * (W - PL - PR);
  const py = (y: number) => H - PB - ((y - yLo) / (yHi - yLo || 1)) * (H - PB - PT);
  const colors = ['var(--green)', 'var(--amber)'];

  return (
    <>
      <svg className="chart-svg" viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${series[0].metric} 曲线（${unit}）`}>
        {[0, 0.25, 0.5, 0.75, 1].map((t) => {
          const v = yLo + (yHi - yLo) * t;
          return (
            <g key={t}>
              <line x1={PL} x2={W - PR} y1={py(v)} y2={py(v)} stroke="var(--line)" strokeWidth="1" />
              <text x={PL - 6} y={py(v) + 3} textAnchor="end" fontSize="9" fill="var(--faint)" fontFamily="var(--mono)">{v.toFixed(1)}</text>
            </g>
          );
        })}
        {[x0, (x0 + x1) / 2, x1].map((v, i) => (
          <text key={i} x={px(v)} y={H - 10} textAnchor="middle" fontSize="9" fill="var(--faint)" fontFamily="var(--mono)">{Math.round(v)}</text>
        ))}
        <text x={W / 2} y={H - 1} textAnchor="middle" fontSize="8.5" fill="var(--faint)" fontFamily="var(--mono)">{xAxis === 'epoch' ? 'epoch' : xAxis}</text>
        {series.map((s, si) => {
          const pts = s.points.filter((p) => p.y !== null) as { x: number; y: number }[];
          const d = pts.map((p, i) => `${i === 0 ? 'M' : 'L'}${px(p.x).toFixed(1)},${py(p.y).toFixed(1)}`).join(' ');
          return (
            <path key={s.id} d={d} fill="none" stroke={colors[si % colors.length]} strokeWidth="1.6"
              strokeDasharray={s.source === 'paper_digitized' ? '5,4' : undefined} />
          );
        })}
      </svg>
      <div className="chart-legend">
        {series.map((s, i) => (
          <span key={s.id}><i style={{ background: colors[i % colors.length] }}></i>{s.name}（{s.unit}）</span>
        ))}
        <span style={{ marginLeft: 'auto' }}>实线=实测 · 虚线=论文数字化</span>
      </div>
    </>
  );
}
