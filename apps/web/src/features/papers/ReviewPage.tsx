import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiClientError, getClient, type ExperimentSpec, type SpecRevision } from '../../api-client';
import { useProject } from '../../app';
import { EmptyState, ErrorBanner, PageLoading, SkeletonBlock } from '../../components/Feedback';
import { Tag } from '../../components/StatusTag';
import { formatMicros } from '../../formatters/format';

const EV_TEXT: Record<string, string> = {
  supported: '有原文证据',
  inferred: '推断',
  missing: '缺失',
  conflicting: '存在冲突',
};
const EV_KIND: Record<string, 'ok' | 'warn' | 'err' | 'dim'> = {
  supported: 'ok',
  inferred: 'warn',
  missing: 'err',
  conflicting: 'err',
};

type FormValues = Record<string, string>;

function applyValues(spec: ExperimentSpec, values: FormValues, paths: string[]): ExperimentSpec {
  const next = structuredClone(spec);
  for (const p of paths) {
    const v = values[p];
    if (v === undefined || v === '') continue;
    if (p === 'mode') next.mode = v as ExperimentSpec['mode'];
    else if (p === 'training.seed') next.training.seed = Number.parseInt(v, 10);
    else if (p === 'training.batch_size') next.training.batch_size = Number.parseInt(v, 10);
    else if (p === 'training.max_epochs') next.training.max_epochs = Number.parseInt(v, 10);
    else if (p === 'training.max_steps') next.training.max_steps = Number.parseInt(v, 10);
    else if (p === 'training.learning_rate') next.training.learning_rate = Number.parseFloat(v);
    else if (p === 'training.weight_decay') next.training.weight_decay = Number.parseFloat(v);
    else if (p === 'dataset.train_subset_fraction') next.dataset.train_subset_fraction = Number.parseFloat(v);
  }
  return next;
}

export function ReviewPage() {
  const { paperId = '' } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const project = useProject();
  const queryClient = useQueryClient();

  const paperQ = useQuery({
    queryKey: ['paper', paperId],
    queryFn: () => getClient().getPaper(paperId),
    refetchInterval: (q) => (q.state.data?.status === 'parsing' ? 3000 : false),
  });
  const paper = paperQ.data;
  const operationQ = useQuery({
    queryKey: ['operation', paper?.active_operation_id],
    queryFn: () => getClient().getOperation(paper!.active_operation_id!),
    enabled: !!paper?.active_operation_id,
    refetchInterval: (q) => (q.state.data?.status === 'running' ? 3000 : false),
  });
  const extractionQ = useQuery({
    queryKey: ['extraction', paperId],
    queryFn: () => getClient().getExtraction(paperId),
    enabled: paper?.status === 'ready' || paper?.status === 'failed' || paper?.status === 'unsupported',
    retry: false,
  });
  const experimentsQ = useQuery({
    queryKey: ['experiments', project?.id, paperId],
    queryFn: () => getClient().listExperiments(project!.id, paperId),
    enabled: !!project && (paper?.status === 'ready' || !!extractionQ.data),
  });

  const experiments = experimentsQ.data?.items ?? [];
  const experimentId = searchParams.get('experiment_id') ?? experiments[0]?.experiment_id ?? null;
  const experiment = experiments.find((e) => e.experiment_id === experimentId) ?? null;
  const draftQ = useQuery({
    queryKey: ['draft', experimentId],
    queryFn: () => getClient().getDraft(experimentId!),
    enabled: !!experimentId,
  });

  const [selectedCandidate, setSelectedCandidate] = useState<string | null>(null);
  const [values, setValues] = useState<FormValues>({});
  const [acceptedWarnings, setAcceptedWarnings] = useState<string[]>([]);
  const [budgetAccepted, setBudgetAccepted] = useState(false);
  const [savedRevision, setSavedRevision] = useState<SpecRevision | null>(null);
  const [conflict, setConflict] = useState<ApiClientError | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const draft = draftQ.data;
  // values 与"实验 + 基线版本"绑定：换实验或保存/加载新基线时按服务端 spec 重建
  const initializedKeyRef = useRef<string | null>(null);
  useEffect(() => {
    if (!draft) return;
    const key = `${draft.experiment_id}:${draft.base_revision_id ?? 'none'}`;
    if (initializedKeyRef.current === key) return;
    initializedKeyRef.current = key;
    const init: FormValues = {};
    for (const f of draft.editable_fields) {
      const raw = f.path.split('.').reduce<unknown>((acc, k) => (acc as Record<string, unknown>)?.[k], draft.spec);
      init[f.path] = raw === null || raw === undefined ? '' : String(raw);
    }
    setValues(init);
  }, [draft]);
  useEffect(() => {
    initializedKeyRef.current = null;
    setSavedRevision(null);
    setAcceptedWarnings([]);
    setBudgetAccepted(false);
    setConflict(null);
  }, [experimentId]);

  const editablePaths = useMemo(() => draft?.editable_fields.map((f) => f.path) ?? [], [draft]);
  const currentSpec = useMemo(
    () => (draft ? applyValues(draft.spec, values, editablePaths) : null),
    [draft, values, editablePaths],
  );
  const dirty = useMemo(
    () => !!draft && !!currentSpec && JSON.stringify(currentSpec) !== JSON.stringify(draft.spec),
    [draft, currentSpec],
  );

  const issues = savedRevision?.validation.issues ?? draft?.issues ?? [];
  const blocking = issues.filter((i) => i.severity === 'blocking');
  const warnings = issues.filter((i) => i.severity === 'warning');
  const precheck = savedRevision?.comparison_precheck ?? draft?.comparison_precheck;
  const budgetEstimate = savedRevision?.budget_estimate ?? null;
  const warningsAccepted = warnings.every((w) => acceptedWarnings.includes(w.code));

  const createExpM = useMutation({
    mutationFn: () => {
      const extraction = extractionQ.data;
      if (!extraction || extraction.kind !== 'extraction' || !selectedCandidate) throw new Error('缺少解析结果');
      const cand = extraction.extraction.candidate_experiments.find((c) => c.id === selectedCandidate);
      if (!cand?.recipe_id) throw new Error('该候选不支持执行');
      return getClient().createExperiment(project!.id, {
        paper_id: paperId,
        extraction_id: extraction.extraction.extraction_id,
        candidate_experiment_id: cand.id,
        recipe_id: cand.recipe_id,
        recipe_version: cand.recipe_version!,
      });
    },
    onSuccess: (exp) => {
      setActionError(null);
      setSearchParams({ experiment_id: exp.experiment_id });
      void queryClient.invalidateQueries({ queryKey: ['experiments'] });
    },
    onError: (e) => setActionError(e),
  });

  const saveM = useMutation({
    mutationFn: () => getClient().saveRevision(project!.id, {
      experiment_id: experimentId!,
      base_revision_id: draft?.base_revision_id ?? null,
      spec: currentSpec!,
    }),
    onSuccess: (rev) => {
      setSavedRevision(rev);
      setConflict(null);
      setActionError(null);
      // 同步草稿缓存：保存后 spec/base/issue 以新 revision 为准，dirty 归零
      if (draft) {
        queryClient.setQueryData(['draft', experimentId], {
          ...draft,
          base_revision_id: rev.id,
          spec: rev.spec,
          issues: rev.validation.issues,
          comparison_precheck: rev.comparison_precheck,
        });
      }
    },
    onError: (e) => {
      if (e instanceof ApiClientError && e.api.code === 'REVISION_CONFLICT') setConflict(e);
      else setActionError(e);
    },
  });

  const confirmRunM = useMutation({
    mutationFn: async () => {
      const rev = savedRevision;
      if (!rev) throw new Error('请先保存并检查');
      const confirmation = await getClient().confirmRevision(rev.id, {
        spec_hash: rev.spec_hash,
        policy_hash: rev.policy_hash,
        accepted_issue_codes: acceptedWarnings,
        accept_budget: true,
      });
      return getClient().createRun(project!.id, {
        experiment_id: rev.experiment_id,
        spec_revision_id: rev.id,
        confirmation_id: confirmation.confirmation_id,
        parent_run_id: null,
      });
    },
    onSuccess: (accepted) => {
      void queryClient.invalidateQueries({ queryKey: ['runs'] });
      navigate(`/runs/${accepted.run_id}`);
    },
    onError: (e) => setActionError(e),
  });
  const navigate = useNavigate();

  if (paperQ.isPending) return <PageLoading label="读取论文" />;
  if (paperQ.error) return <ErrorBanner error={paperQ.error} onRetry={() => void paperQ.refetch()} />;
  if (!paper) return <EmptyState title="论文不存在" />;

  // ---- 解析中（M02）----
  if (paper.status === 'parsing' || paper.status === 'uploaded') {
    const op = operationQ.data;
    return (
      <section className="view-enter">
        <ViewHead title={paper.title ?? '（标题解析中）'} arxiv={paper.arxiv_version} />
        <div className="pending-card">
          <div className="pc-title">论文解析中</div>
          <div className="pc-sub">
            阶段：{op?.stage ?? '排队等待'}
            {op ? ` · 操作 ${op.id}` : ''}
          </div>
          <div className="pc-sub">解析进度暂无可量化总量，等待完成，不显示估计百分比。</div>
        </div>
      </section>
    );
  }

  // ---- 解析失败 / 不支持（M03）----
  if (paper.status === 'failed' || paper.status === 'unsupported') {
    return (
      <section className="view-enter">
        <ViewHead title={paper.title ?? '（未知标题）'} arxiv={paper.arxiv_version} />
        <div className="banner err" role="alert">
          <div>
            <div className="b-title">{paper.error?.code ?? 'PAPER_NOT_READY'} · {paper.error?.message ?? '解析不可用'}</div>
            <div className="b-sub">request_id 见响应头 · 该论文不会进入执行页</div>
          </div>
        </div>
        <SupportScope />
      </section>
    );
  }

  // ---- ready ----
  if (extractionQ.isPending) return <PageLoading label="读取解析结果" />;
  if (extractionQ.error) {
    return (
      <section className="view-enter">
        <ViewHead title={paper.title ?? ''} arxiv={paper.arxiv_version} />
        <ErrorBanner error={extractionQ.error} onRetry={() => void extractionQ.refetch()} />
        <SupportScope />
      </section>
    );
  }
  const extractionRes = extractionQ.data;
  if (extractionRes.kind !== 'extraction') return <PageLoading label="等待解析完成" />;
  const ext = extractionRes.extraction;

  const fieldEntries = Object.entries(ext.fields) as [string, typeof ext.fields.task][];
  const supportedCount = fieldEntries.filter(([, f]) => f.evidence_status === 'supported').length;
  const refs = ext.source_refs;

  return (
    <section className="view-enter">
      <ViewHead title={paper.title ?? '（未知标题）'} arxiv={paper.arxiv_version} evidence={`${supportedCount}/${fieldEntries.length}`} />

      <div className="parse-grid">
        {/* 左：论文纸张（mock 无原文件 → 摘录模式） */}
        <div className="paper-stage">
          <div className="paper-sheet-2" aria-hidden="true"></div>
          <div className="paper-sheet">
            <div style={{ textAlign: 'center', fontWeight: 700, fontSize: 13, lineHeight: 1.4 }}>
              {paper.title ?? '（标题解析中）'}
            </div>
            <div style={{ textAlign: 'center', fontSize: 9.5, color: '#666666', marginTop: 4 }}>
              {paper.arxiv_version ? `arXiv:${paper.arxiv_version}` : '本地 PDF'}
            </div>
            {refs.slice(0, 5).map((r) => (
              <div key={r.id} className="excerpt">
                <span className="excerpt-loc">
                  p.{r.page}{r.section ? ` · ${r.section}` : ''}{r.bbox ? ' · 有定位框' : ''}
                </span>
                {r.quote}
              </div>
            ))}
            {!paper.content_available && (
              <div className="paper-unavailable">
                原文件不可用（content_available=false，可能处于保留期或当前模式未挂载 PDF），
                以上为解析摘录；有 bbox 的证据在接入真实 PDF 预览后支持跳页高亮。
              </div>
            )}
          </div>
        </div>

        {/* 右：提取结果 + 实验确认 */}
        <div>
          <div className="ext-card">
            <div className="ext-h">
              <span className="sec-name">任务画像</span>
              <span className="sec-tip">字段与证据状态来自解析</span>
            </div>
            <dl className="kv">
              {fieldEntries.map(([key, f]) => (
                <div key={key} style={{ display: 'contents' }}>
                  <dt>{key === 'task' ? '任务类型' : key === 'dataset' ? '数据集' : '优化器'}</dt>
                  <dd>
                    {f.value ?? <span className="null-note">null（未解析出）</span>}{' '}
                    <Tag kind={EV_KIND[f.evidence_status]}>{EV_TEXT[f.evidence_status]}</Tag>
                  </dd>
                </div>
              ))}
            </dl>
          </div>

          <div className="ext-card">
            <div className="ext-h"><span className="sec-name">方法实体</span></div>
            <div className="chips">
              {ext.methods.map((m) => <span key={m.id} className="chip">{m.label}</span>)}
            </div>
          </div>

          <div className="ext-card">
            <div className="ext-h"><span className="sec-name">关键公式</span><span className="sec-tip">LaTeX 源码只读展示</span></div>
            {ext.formulas.map((f) => (
              <div key={f.id} className="formula"><span>{f.latex}</span><span className="eq-id">{f.id}</span></div>
            ))}
          </div>

          <div className="ext-card">
            <div className="ext-h"><span className="sec-name">候选实验</span><span className="sec-tip">一次只选择一个候选</span></div>
            {ext.candidate_experiments.map((c) => {
              const chosen = experiment ? draft?.spec.candidate_experiment_id === c.id : selectedCandidate === c.id;
              return (
                <div key={c.id} className="goal-row">
                  <span className="goal-ico">{c.label.startsWith('Table') ? c.label.slice(6, 8).trim() : c.label.slice(0, 2)}</span>
                  <span className="goal-name">
                    {c.label}
                    {c.unsupported_reasons.length > 0 && (
                      <span className="gn-sub">{c.unsupported_reasons.join('；')}</span>
                    )}
                  </span>
                  {experiment ? (
                    chosen ? <Tag kind="ok">当前实验</Tag> : null
                  ) : (
                    <label className="radio-row" style={{ padding: 0 }}>
                      <input
                        type="radio"
                        name="candidate"
                        checked={chosen}
                        disabled={c.execution_support === 'unsupported' || !c.recipe_id}
                        onChange={() => setSelectedCandidate(c.id)}
                      />
                    </label>
                  )}
                </div>
              );
            })}
            {!experiment && (
              <div className="action-bar">
                <button
                  type="button"
                  className="btn primary"
                  disabled={!selectedCandidate || createExpM.isPending}
                  onClick={() => createExpM.mutate()}
                >
                  {createExpM.isPending && <span className="spin"></span>}
                  创建实验草稿
                </button>
                <span className="ab-note">仅支持可执行候选；不支持的候选只能查看解析</span>
              </div>
            )}
            {actionError != null && <ErrorBanner error={actionError} />}
          </div>

          {experimentId && draftQ.isPending && (
            <div className="ext-card"><SkeletonBlock lines={4} /></div>
          )}
          {draftQ.error && <ErrorBanner error={draftQ.error} onRetry={() => void draftQ.refetch()} />}

          {draft && currentSpec && (
            <div className="ext-card">
              <div className="ext-h">
                <span className="sec-name">实验草稿与确认</span>
                <span className="sec-tip">{experiment ? `实验 ${experiment.experiment_id} · 状态 ${experiment.status}` : ''}</span>
              </div>

              <div className="draft-form">
                {draft.editable_fields.map((f) => (
                  <div key={f.path} className="draft-field">
                    <label htmlFor={`f-${f.path}`}>
                      {f.label} <span className="opt">{f.nullable ? '可空' : '必填'}</span>
                    </label>
                    {f.kind === 'select' ? (
                      <select
                        id={`f-${f.path}`}
                        value={values[f.path] ?? ''}
                        onChange={(e) => setValues((v) => ({ ...v, [f.path]: e.target.value }))}
                      >
                        {f.options?.map((o) => (
                          <option key={String(o.value)} value={String(o.value)}>{o.label}</option>
                        ))}
                      </select>
                    ) : (
                      <input
                        id={`f-${f.path}`}
                        type={f.kind === 'integer' || f.kind === 'number' ? 'number' : 'text'}
                        value={values[f.path] ?? ''}
                        min={f.min ?? undefined}
                        max={f.max ?? undefined}
                        step={f.kind === 'number' ? 'any' : 1}
                        onChange={(e) => setValues((v) => ({ ...v, [f.path]: e.target.value }))}
                      />
                    )}
                  </div>
                ))}
              </div>

              {currentSpec.mode !== 'reference' && (
                <div className="banner" style={{ marginTop: 14 }}>
                  <div>
                    <div className="b-title">{currentSpec.mode === 'smoke' ? 'smoke 模式只验证流程连通性' : 'scaled 模式为缩减实验'}</div>
                    <div className="b-sub">不直接证明论文指标；比较资格以保存后的 precheck 为准</div>
                  </div>
                </div>
              )}

              <div style={{ marginTop: 14 }}>
                <div className="ext-h"><span className="sec-name">检查问题</span></div>
                {issues.length === 0 ? (
                  <div className="cap-strip">无待确认问题</div>
                ) : (
                  <div className="issue-list">
                    {blocking.map((i) => (
                      <div key={i.code} className="issue-item blocking">
                        <div>
                          <div>{i.message}</div>
                          <div className="ii-code">{i.code} · path={i.path ?? '/'} · blocking，禁止确认</div>
                        </div>
                      </div>
                    ))}
                    {warnings.map((i) => (
                      <label key={i.code} className="issue-item">
                        <input
                          type="checkbox"
                          checked={acceptedWarnings.includes(i.code)}
                          onChange={(e) =>
                            setAcceptedWarnings((list) =>
                              e.target.checked ? [...list, i.code] : list.filter((c) => c !== i.code))
                          }
                        />
                        <div>
                          <div>{i.message}</div>
                          <div className="ii-code">{i.code} · path={i.path ?? '/'} · warning，需勾选接受</div>
                        </div>
                      </label>
                    ))}
                  </div>
                )}
              </div>

              {precheck && (
                <div className="cap-strip" style={{ marginTop: 10 }}>
                  比较资格：{precheck.status === 'eligible' ? '可比较' : precheck.status === 'not_comparable' ? '不可直接比较' : '待定'}
                  {precheck.reasons.length > 0 ? ` · 原因 ${precheck.reasons.join('、')}` : ''}
                </div>
              )}

              <details style={{ marginTop: 10 }}>
                <summary className="cap-strip" style={{ cursor: 'pointer' }}>只读：recipe_config 与目标（原样回传，不做任意编辑）</summary>
                <pre className="ro-config" style={{ marginTop: 8 }}>{JSON.stringify(currentSpec.recipe_config, null, 2)}</pre>
                <div style={{ marginTop: 8 }}>
                  {currentSpec.targets.map((t) => (
                    <div key={t.id} className="cap-strip">
                      目标 {t.metric}（{t.unit}）· 原文值 {t.reference_value ?? 'null'} · 容差 {t.tolerance ?? 'null'} · 统计口径 {t.statistic}
                    </div>
                  ))}
                </div>
              </details>

              {budgetEstimate && (
                <label className="issue-item" style={{ marginTop: 12 }}>
                  <input
                    type="checkbox"
                    checked={budgetAccepted}
                    onChange={(e) => setBudgetAccepted(e.target.checked)}
                  />
                  <div>
                    <div>接受预算估计：{formatMicros(budgetEstimate.amount_micros)} · 预计 {Math.round(budgetEstimate.duration_seconds! / 60)} 分钟</div>
                    <div className="ii-code">{budgetEstimate.basis}</div>
                  </div>
                </label>
              )}

              {conflict && (
                <div className="banner err" style={{ marginTop: 12 }}>
                  <div>
                    <div className="b-title">保存冲突（409）：队友保存了新版本，本地修改已保留</div>
                    <div className="b-sub">latest_revision_id={String(conflict.api.details.latest_revision_id ?? 'unknown')}</div>
                  </div>
                  <div className="b-act">
                    <button
                      type="button"
                      className="btn sm"
                      onClick={() => { setConflict(null); void queryClient.invalidateQueries({ queryKey: ['draft'] }); }}
                    >
                      加载最新版本
                    </button>
                  </div>
                </div>
              )}
              {actionError != null && !conflict && <ErrorBanner error={actionError} />}

              <div className="action-bar">
                <button
                  type="button"
                  className="btn"
                  disabled={!dirty || saveM.isPending}
                  title={!dirty ? '没有未保存的修改' : undefined}
                  onClick={() => saveM.mutate()}
                >
                  {saveM.isPending && <span className="spin"></span>}
                  保存并检查
                </button>
                <button
                  type="button"
                  className="btn primary"
                  disabled={
                    dirty || blocking.length > 0 || !warningsAccepted || !budgetEstimate || !budgetAccepted || confirmRunM.isPending
                  }
                  title={
                    dirty ? '存在未保存的修改'
                      : blocking.length > 0 ? '存在 blocking 问题'
                        : !warningsAccepted ? '请先接受全部 warning'
                          : !budgetAccepted ? '请先接受预算估计' : undefined
                  }
                  onClick={() => confirmRunM.mutate()}
                >
                  {confirmRunM.isPending && <span className="spin"></span>}
                  确认并运行
                </button>
                <span className="ab-note">确认与启动是两次独立请求；确认成功但启动失败会保留 confirmation 重试</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function ViewHead({ title, arxiv, evidence }: { title: string; arxiv: string | null; evidence?: string }) {
  return (
    <div className="view-head">
      <div className="vh-main">
        <h1 className="paper-h1">{title}</h1>
        <div className="paper-meta">
          {arxiv && <span className="venue">arXiv:{arxiv}</span>}
        </div>
      </div>
      {evidence && (
        <div className="ev-badge">
          <div className="cb-label">字段证据状态</div>
          <div className="cb-val" style={{ color: 'var(--green)' }}>{evidence}</div>
          <div className="cb-sub">supported / 总字段数</div>
        </div>
      )}
    </div>
  );
}

function SupportScope() {
  const capsQ = useQuery({ queryKey: ['capabilities'], queryFn: () => getClient().getCapabilities(), staleTime: 60_000 });
  if (!capsQ.data) return null;
  return (
    <div className="ext-card" style={{ marginTop: 16 }}>
      <div className="ext-h"><span className="sec-name">当前支持范围</span></div>
      {capsQ.data.recipes.map((r) => (
        <div key={r.id} className="cap-strip">
          {r.label}（{r.id} v{r.version}）· 模式 {r.modes.join('/')} · 数据集 {r.dataset_ids.join(',')}
          {r.execution_support === 'unavailable' ? ` · 不可用：${r.reason}` : ''}
        </div>
      ))}
    </div>
  );
}
