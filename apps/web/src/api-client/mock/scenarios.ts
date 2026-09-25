// M01–M12 Mock 场景（对应 docs/10 Mock 场景清单）。
// 时间推进 tick 只在 mock 模式内驱动"后端"状态；live 组件不引用任何计时器。
import type { Attempt, Export, Report, Run, RunCurves, RunManifest } from '../types';
import {
  baseAttempt, baseFiles, baseRun, createBaseDb, fakeHash, patchText,
  registerArtifact, trainingLogLines, type MutableDb,
} from './fixtures';

export interface ScenarioDef {
  id: string;
  label: string;
  expect: string;
  entry: string;
  setup: (db: MutableDb) => void;
  tick?: (db: MutableDb, elapsed: number) => void;
}

const S = 1000;

function registerRunArtifacts(attemptId: string, maxEpoch: number, uptoEpoch: number, prefix: string[] = []) {
  const lines = [...prefix, ...trainingLogLines(maxEpoch, uptoEpoch)];
  registerArtifact(`art_log_${attemptId.slice(-4)}`, lines.join('\n') + '\n');
}

function scaledReport(runId: string): Report {
  return {
    id: `rpt_${runId}`,
    run_id: runId,
    version: 1,
    mode: 'scaled',
    data_mode: 'mock',
    verdict: 'outside_tolerance',
    comparison_reasons: [],
    score: { policy_version: 'heuristic-1', metric_match: 0.45, process: 0.8, environment: 0.9, total: 63 },
    metrics: [{
      id: 'mr_top1', name: 'Top-1 Error', unit: 'percent',
      reference_value: 8.75, observed_value: 9.86,
      delta_absolute: 1.11, delta_relative: 0.1269,
      tolerance: 1.0, comparable: true, within_tolerance: false,
      source_ref: 'src_5', evidence_artifact_ids: ['art_metrics_3001'],
    }],
    evidence: { spec_revision_id: 'rev_2001', manifest_artifact_id: 'art_manifest_3001', metric_result_id: 'mr_top1' },
    limitations: [
      'scaled 模式训练轮数低于原文协议（60/182 epochs）',
      '单次运行结果，论文报告为多次运行统计值',
      '此为界面测试数据，不是真实实验',
    ],
    created_at: new Date(Date.now() - 40 * 60_000).toISOString(),
  };
}

function smokeReport(runId: string): Report {
  return {
    id: `rpt_${runId}`,
    run_id: runId,
    version: 1,
    mode: 'smoke',
    data_mode: 'mock',
    verdict: 'not_comparable',
    comparison_reasons: ['SMOKE_MODE', 'TRAINING_BUDGET_REDUCED'],
    score: { policy_version: 'heuristic-1', metric_match: null, process: 0.6, environment: 0.6, total: null },
    metrics: [{
      id: 'mr_top1', name: 'Top-1 Error', unit: 'percent',
      reference_value: 8.75, observed_value: 28.4,
      delta_absolute: null, delta_relative: null,
      tolerance: 1.0, comparable: false, within_tolerance: null,
      source_ref: 'src_5', evidence_artifact_ids: ['art_metrics_3001'],
    }],
    evidence: { spec_revision_id: 'rev_2001', manifest_artifact_id: 'art_manifest_3001', metric_result_id: 'mr_top1' },
    limitations: [
      'smoke 模式仅验证流程连通性，不证明论文指标',
      '训练子集与轮数低于原文协议',
      '此为界面测试数据，不是真实实验',
    ],
    created_at: new Date(Date.now() - 35 * 60_000).toISOString(),
  };
}

function manifestFor(runId: string): RunManifest {
  return {
    run_id: runId,
    status: 'finalized',
    schema_version: '1.0',
    spec_hash: fakeHash(`rev-2001-spec`),
    paper_sha256: fakeHash('paper-1512.03385v1'),
    code_sha256: fakeHash('attempt-2-code'),
    image_digest: `registry.local/paper2run/runner:cuda12.1-torch2.4@sha256:${fakeHash('image').slice(0, 12)}`,
    seed: 20260919,
    dataset: {
      id: 'cifar10', version: '2011.06',
      sha256: fakeHash('cifar10'), split_sha256: fakeHash('cifar10-split'),
    },
    environment: { python: '3.11.9', torch: '2.4.1+cu121', cuda: '12.1', gpu: 'NVIDIA A10 24GB', driver: '550.54.15' },
    models: [
      { purpose: 'parse', model_id: 'qwen-vl-max', prompt_version: 'parse-v3' },
      { purpose: 'plan', model_id: 'qwen3-max', prompt_version: 'plan-v5' },
      { purpose: 'code', model_id: 'qwen3-coder', prompt_version: 'code-v7' },
    ],
    evaluator_version: 'p2r-eval/0.7.2',
    artifacts: [
      { artifact_id: 'art_cm_4002', kind: 'code_manifest', bytes: 4821, sha256: fakeHash('cm-4002') },
      { artifact_id: 'art_log_4002', kind: 'stdout_log', bytes: 128400, sha256: fakeHash('log-4002') },
      { artifact_id: 'art_metrics_3001', kind: 'metrics_json', bytes: 812, sha256: fakeHash('metrics-3001') },
    ],
    limitations: [],
  };
}

function measuredCurve(maxEpoch: number, startErr: number, endErr: number, step: number): { x: number; y: number }[] {
  const pts: { x: number; y: number }[] = [];
  for (let e = 1; e <= maxEpoch; e += step) {
    const t = Math.log(1 + 9 * (e / maxEpoch)) / Math.log(10);
    pts.push({ x: e, y: +(startErr + (endErr - startErr) * (1 - Math.exp(-2.2 * t))).toFixed(2) });
  }
  pts.push({ x: maxEpoch, y: endErr });
  return pts;
}

function scaledCurves(runId: string): RunCurves {
  return {
    run_id: runId, attempt_id: 'att_4002', downsampled: false, warnings: [],
    series: [
      {
        id: 'cs_meas', name: '实测 · ResNet-20（scaled）', source: 'measured',
        metric: 'Top-1 Error', unit: '%', x_axis: 'epoch',
        points: measuredCurve(60, 30.2, 9.86, 2), evidence_artifact_id: 'art_metrics_3001',
      },
      {
        id: 'cs_paper', name: '论文 · ResNet-20（数字化）', source: 'paper_digitized',
        metric: 'Top-1 Error', unit: '%', x_axis: 'epoch',
        points: measuredCurve(182, 31.5, 8.75, 8), evidence_artifact_id: 'art_metrics_3001',
      },
    ],
  };
}

function partialExports(runId: string): Export[] {
  return [{
    id: 'exp_9001', run_id: runId, status: 'partial', operation_id: 'op_7201',
    created_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    items: [
      { format: 'bundle_zip', status: 'ready', artifact_id: 'art_dl_9001', filename: `paper2run_${runId}_bundle.zip`, bytes: 48213312, error: null },
      { format: 'report_json', status: 'ready', artifact_id: 'art_dl_9002', filename: `report_${runId}.json`, bytes: 8124, error: null },
      { format: 'report_pdf', status: 'failed', artifact_id: null, filename: null, bytes: null, error: { code: 'REPORT_BUILD_FAILED', message: 'PDF 渲染超时（字体子集嵌入失败）', retryable: true, details: {} } },
    ],
  }];
}

// 通用：排队→生成→执行的推进（M05 基线，M06 增加修复分支）
function progressingTick(opts: { repairAt?: number }) {
  return (db: MutableDb, elapsed: number) => {
    const run = db.runs[0];
    if (!run || isTerminal(run)) return;
    if (run.status === 'cancel_requested') return handleCancel(db, run);
    const att1 = db.attempts.find((a) => a.id === 'att_4001');
    const repairAt = opts.repairAt;
    const repairPhase = repairAt !== undefined && elapsed >= repairAt;

    if (elapsed < 6 * S) {
      Object.assign(run, { status: 'queued', stage: null, attempt_index: null, started_at: null });
      if (att1) att1.status = 'generated';
      db.filesByAttempt.clear();
      return;
    }
    if (elapsed < 26 * S) {
      Object.assign(run, { status: 'generating', stage: elapsed < 16 * S ? 'code_generate' : 'code_validate', attempt_index: 1, started_at: new Date(db.loadedAt + 6 * S).toISOString() });
      if (att1) att1.status = 'validating';
      if (elapsed >= 10 * S) db.filesByAttempt.set('att_4001', baseFiles('att_4001'));
      registerRunArtifacts('att_4001', 60, 0);
      return;
    }
    if (repairPhase && elapsed < repairAt! + 8 * S) {
      // attempt1 失败 → 修复提议
      Object.assign(run, { status: 'repairing', stage: 'repair_propose', attempt_index: 2, repairs_used: 1 });
      if (att1) {
        att1.status = 'failed';
        att1.error_summary = { code: 'SHAPE_MISMATCH', message: 'conv2 输出与 shortcut 维度不匹配（16→32，stride=2）' };
        att1.diagnosis_summary = 'shortcut 分支在维度变换时缺少 BatchNorm，与论文 3.3 节 option B 不一致；提议在 projection 后补 BatchNorm。';
      }
      const att2 = ensureAtt2(db);
      att2.status = 'validating';
      registerArtifact('art_patch_4002', patchText());
      registerRunArtifacts('att_4001', 16, 16, [
        '[stderr] RuntimeError: The size of tensor a (32) must match the size of tensor b (16) at non-singleton dimension 1',
        '[system] attempt 1 failed (SHAPE_MISMATCH), retry_class=repairable',
      ]);
      return;
    }
    // 执行阶段
    const execStart = repairPhase ? repairAt! + 8 * S : 26 * S;
    Object.assign(run, {
      status: 'executing', stage: 'train_execute',
      attempt_index: repairPhase ? 2 : 1,
      repairs_used: repairPhase ? 1 : 0,
    });
    if (att1 && !repairPhase) att1.status = 'running';
    if (repairPhase) {
      if (att1) {
        att1.status = 'failed';
        // 幂等补齐：轮询可能跳过 repairing 窗口，失败证据不能丢
        if (!att1.error_summary) {
          att1.error_summary = { code: 'SHAPE_MISMATCH', message: 'conv2 输出与 shortcut 维度不匹配（16→32，stride=2）' };
          att1.diagnosis_summary = 'shortcut 分支在维度变换时缺少 BatchNorm，与论文 3.3 节 option B 不一致；提议在 projection 后补 BatchNorm。';
        }
      }
      const att2 = ensureAtt2(db);
      att2.status = 'running';
      db.filesByAttempt.set('att_4002', baseFiles('att_4002'));
      registerRunArtifacts('att_4002', 60, Math.floor((elapsed - execStart) / (1.5 * S)));
      registerArtifact('art_patch_4002', patchText());
      registerRunArtifacts('att_4001', 16, 16, [
        '[stderr] RuntimeError: The size of tensor a (32) must match the size of tensor b (16) at non-singleton dimension 1',
        '[system] attempt 1 failed (SHAPE_MISMATCH), retry_class=repairable',
      ]);
    } else {
      db.filesByAttempt.set('att_4001', baseFiles('att_4001'));
      registerRunArtifacts('att_4001', 60, Math.floor((elapsed - 26 * S) / (1.5 * S)));
    }
  };
}

function ensureAtt2(db: MutableDb): Attempt {
  let att2 = db.attempts.find((a) => a.id === 'att_4002');
  if (!att2) {
    att2 = baseAttempt(2, { status: 'validating' });
    db.attempts.push(att2);
  }
  return att2;
}

function isTerminal(run: Run): boolean {
  return ['completed', 'failed', 'cancelled', 'awaiting_revision'].includes(run.status);
}

function handleCancel(db: MutableDb, run: Run) {
  const at = db.cancelRequestedAt.get(run.id);
  if (!at || Date.now() - at < 5 * S) return;
  const outcome = db.cancelOutcome?.(run.id) ?? 'cancelled';
  // 竞态以服务端终态为准：完成也可能胜出
  if (outcome === 'completed') {
    finishRun(db, run, 'scaled', 'generating');
    db.reports.set(run.id, { kind: 'generating', operationId: 'op_7101' });
  } else {
    Object.assign(run, { status: 'cancelled', stage: null, ended_at: new Date().toISOString() });
  }
}

function finishRun(db: MutableDb, run: Run, mode: 'smoke' | 'scaled', reportStatus: Run['report_status']) {
  Object.assign(run, {
    status: 'completed', stage: null, ended_at: new Date().toISOString(),
    mode, report_status: reportStatus,
    budget: { ...run.budget, actual_micros: run.budget.actual_micros ?? 6_842_000, reserved_micros: 0 },
  });
  db.manifests.set(run.id, manifestFor(run.id));
}

export { progressingTick };

export const SCENARIOS: ScenarioDef[] = [
  {
    id: 'M01', label: 'M01 空项目', entry: '/projects',
    expect: '空态及上传入口，无假历史',
    setup(db) {
      db.papers = [];
      db.extractions = [];
      db.experiments = [];
    },
  },
  {
    id: 'M02', label: 'M02 上传/解析中', entry: '/papers/pap_1001/review',
    expect: '上传 202、真实轮询结构、缺进度时不显示假百分比',
    setup(db) {
      db.papers[0] = {
        ...db.papers[0],
        status: 'parsing',
        current_extraction_id: null,
        active_operation_id: 'op_7001',
      };
      db.operations.set('op_7001', {
        id: 'op_7001', kind: 'paper_ingest', status: 'running',
        resource: { type: 'paper', id: 'pap_1001' },
        stage: 'pdf_text', progress: null, error: null,
        updated_at: new Date().toISOString(),
      });
    },
    tick(db, elapsed) {
      if (elapsed < 45 * S) return;
      const paper = db.papers.find((p) => p.paper_id === 'pap_1001');
      if (paper && paper.status === 'parsing') {
        paper.status = 'ready';
        paper.current_extraction_id = 'ext_1001';
        paper.active_operation_id = null;
        const op = db.operations.get('op_7001');
        if (op) op.status = 'succeeded';
      }
    },
  },
  {
    id: 'M03', label: 'M03 解析失败/不支持', entry: '/papers/pap_1001/review',
    expect: '错误与支持范围，不进入执行页假装成功',
    setup(db) {
      db.papers[0] = {
        ...db.papers[0],
        status: 'failed',
        current_extraction_id: null,
        error: { code: 'PAPER_PARSE_FAILED', message: 'PDF 文本层缺失或损坏，无法完成结构化解析', retryable: false, details: {} },
      };
      db.papers.push({
        ...db.papers[0],
        paper_id: 'pap_1002',
        title: 'Attention Is All You Need',
        status: 'unsupported',
        arxiv_version: '1706.03762v7',
        page_count: 15,
        error: { code: 'UNSUPPORTED_RECIPE', message: '首版仅支持 CIFAR-10 图像分类论文，暂不支持 NLP 预训练范式', retryable: false, details: {} },
      });
    },
  },
  {
    id: 'M04', label: 'M04 缺参数/冲突', entry: '/papers/pap_1001/review?experiment_id=exp_1001',
    expect: 'blocking 禁确认，409 保留本地修改',
    setup(db) {
      const draft = db.drafts.get('exp_1001');
      if (draft) {
        draft.issues = [
          ...draft.issues,
          { code: 'TARGET_SAMPLE_INSUFFICIENT', message: '训练子集比例 0.1 不足以支撑容差判定，请提升子集比例或切换 scaled 模式', path: 'dataset.train_subset_fraction', severity: 'blocking' },
        ];
      }
      db.conflictOnce = true;
    },
  },
  {
    id: 'M05', label: 'M05 排队至执行', entry: '/runs/run_3001',
    expect: '按快照状态切换，不按定时动画自动成功',
    setup(db) {
      db.runs.push(baseRun({ status: 'queued', stage: null, attempt_index: null, started_at: null, created_at: new Date().toISOString(), budget: { currency: 'CNY', limit_micros: 20_000_000, actual_micros: null, reserved_micros: 8_000_000, unknown_charges: false } }));
      db.attempts.push(baseAttempt(1, { status: 'generated' }));
    },
    tick: progressingTick({}),
  },
  {
    id: 'M06', label: 'M06 一次修复', entry: '/runs/run_3001',
    expect: '错误、patch、候选版本和新日志对应',
    setup(db) {
      db.runs.push(baseRun({ status: 'queued', stage: null, attempt_index: null, started_at: null, created_at: new Date().toISOString(), budget: { currency: 'CNY', limit_micros: 20_000_000, actual_micros: null, reserved_micros: 8_000_000, unknown_charges: false } }));
      db.attempts.push(baseAttempt(1, { status: 'generated' }));
    },
    tick: progressingTick({ repairAt: 30 }),
  },
  {
    id: 'M07', label: 'M07 修复耗尽/预算停止', entry: '/runs/run_3001',
    expect: '终态及诊断，不无限重试',
    setup(db) {
      db.runs.push(baseRun({
        status: 'failed', stage: null, repairs_used: 5, attempt_index: 5,
        ended_at: new Date(Date.now() - 4 * 60_000).toISOString(),
        report_status: 'failed',
        terminal_reason: { code: 'REPAIRS_EXHAUSTED', message: '自动修复次数达到上限（5 次），停止重试；已保留全部日志与补丁证据' },
      }));
      for (let i = 1; i <= 5; i++) {
        db.attempts.push(baseAttempt(i, {
          status: 'failed',
          created_at: new Date(Date.now() - (50 - i * 8) * 60_000).toISOString(),
          error_summary: {
            code: i % 2 === 0 ? 'CUDA_OOM' : 'SHAPE_MISMATCH',
            message: i % 2 === 0 ? 'batch 128 在 gpu-0 触发显存溢出（需 26.1GB / 上限 24GB）' : 'conv2 输出与 shortcut 维度不匹配（16→32，stride=2）',
          },
          diagnosis_summary: i % 2 === 0 ? 'epoch 91 学习率上调后激活值放大，建议 batch 减半并线性缩放学习率。' : 'shortcut 投影分支缺少 BatchNorm，与论文 3.3 节 option B 不一致。',
          patch_artifact_id: i > 1 ? `art_patch_400${i}` : null,
        }));
        registerArtifact(`art_patch_400${i}`, patchText());
        registerRunArtifacts(`att_400${i}`, 60, 4 * i);
      }
      db.filesByAttempt.set('att_4005', baseFiles('att_4005'));
    },
  },
  {
    id: 'M08', label: 'M08 取消竞态', entry: '/runs/run_3001',
    expect: 'cancel_requested 到实际终态，完成也可胜出',
    setup(db) {
      db.runs.push(baseRun({ created_at: new Date(Date.now() - 8 * 60_000).toISOString() }));
      db.attempts.push(baseAttempt(1, { status: 'running' }));
      db.filesByAttempt.set('att_4001', baseFiles('att_4001'));
      registerRunArtifacts('att_4001', 60, 23);
      db.cancelOutcome = () => 'completed';
    },
    tick(db) {
      const run = db.runs[0];
      if (run.status === 'cancel_requested') handleCancel(db, run);
    },
  },
  {
    id: 'M10', label: 'M10 完成但报告延迟', entry: '/runs/run_3001/report',
    expect: '继续等待报告，不显示空白或虚假分',
    setup(db) {
      db.runs.push(baseRun({
        status: 'completed', stage: null, ended_at: new Date(Date.now() - 3 * 60_000).toISOString(),
        report_status: 'generating', attempt_index: 2, repairs_used: 1,
      }));
      db.attempts.push(baseAttempt(1, { status: 'failed', error_summary: { code: 'SHAPE_MISMATCH', message: 'conv2 输出与 shortcut 维度不匹配' } }));
      db.attempts.push(baseAttempt(2, { status: 'succeeded' }));
      db.filesByAttempt.set('att_4002', baseFiles('att_4002'));
      registerRunArtifacts('att_4002', 60, 60);
      db.reports.set('run_3001', { kind: 'generating', operationId: 'op_7101' });
      db.manifests.set('run_3001', manifestFor('run_3001'));
    },
    tick(db, elapsed) {
      if (elapsed < 40 * S) return;
      const run = db.runs[0];
      if (run.report_status === 'generating') {
        run.report_status = 'ready';
        db.reports.set('run_3001', scaledReport('run_3001'));
        db.curves.set('run_3001', scaledCurves('run_3001'));
      }
    },
  },
  {
    id: 'M11', label: 'M11 不可比结果', entry: '/runs/run_3001/report',
    expect: 'total=null、无差异值、清楚模式标签',
    setup(db) {
      db.runs.push(baseRun({
        status: 'completed', stage: null, mode: 'smoke', attempt_index: 1, repairs_used: 0,
        ended_at: new Date(Date.now() - 6 * 60_000).toISOString(),
        report_status: 'ready',
        budget: { currency: 'CNY', limit_micros: 20_000_000, actual_micros: 1_612_000, reserved_micros: 0, unknown_charges: false },
      }));
      db.attempts.push(baseAttempt(1, { status: 'succeeded' }));
      db.filesByAttempt.set('att_4001', baseFiles('att_4001'));
      registerRunArtifacts('att_4001', 3, 3);
      db.reports.set('run_3001', smokeReport('run_3001'));
      db.manifests.set('run_3001', { ...manifestFor('run_3001'), seed: 20260919 });
      db.curves.set('run_3001', {
        run_id: 'run_3001', attempt_id: 'att_4001', downsampled: false,
        warnings: ['论文仅提供曲线图，未可靠提取数值，未返回 paper_digitized 序列'],
        series: [{
          id: 'cs_smoke', name: '实测 · smoke 流程验证', source: 'measured',
          metric: 'Top-1 Error', unit: '%', x_axis: 'epoch',
          points: [{ x: 1, y: 65.2 }, { x: 2, y: 41.8 }, { x: 3, y: 28.4 }],
          evidence_artifact_id: 'art_metrics_3001',
        }],
      });
    },
  },
  {
    id: 'M12', label: 'M12 导出部分失败', entry: '/runs/run_3001/report',
    expect: 'viewer 可下载现成文件，PDF 失败不挡 ZIP',
    setup(db) {
      db.runs.push(baseRun({
        status: 'completed', stage: null, ended_at: new Date(Date.now() - 50 * 60_000).toISOString(),
        report_status: 'ready', attempt_index: 2, repairs_used: 1,
      }));
      db.attempts.push(baseAttempt(2, { status: 'succeeded' }));
      db.filesByAttempt.set('att_4002', baseFiles('att_4002'));
      registerRunArtifacts('att_4002', 60, 60);
      db.reports.set('run_3001', scaledReport('run_3001'));
      db.manifests.set('run_3001', manifestFor('run_3001'));
      db.curves.set('run_3001', scaledCurves('run_3001'));
      db.exports.set('run_3001', partialExports('run_3001'));
    },
    tick(db, elapsed) {
      const list = db.exports.get('run_3001') ?? [];
      const dyn = list.find((e) => e.id === 'exp_9002');
      if (!dyn) return;
      const created = db.exportCreatedAt.get('exp_9002');
      if (created === undefined) return;
      const age = elapsed - (created - db.loadedAt);
      if (age > 14 * S && dyn.status !== 'partial') {
        dyn.status = 'partial';
        dyn.items.forEach((it) => {
          if (it.format === 'bundle_zip') Object.assign(it, { status: 'ready', artifact_id: 'art_dl_9003', filename: `paper2run_run_3001_bundle.zip`, bytes: 47_882_112 });
          else if (it.format === 'report_json') Object.assign(it, { status: 'ready', artifact_id: 'art_dl_9004', filename: 'report_run_3001.json', bytes: 8201 });
          else Object.assign(it, { status: 'failed', error: { code: 'REPORT_BUILD_FAILED', message: 'PDF 渲染超时（字体子集嵌入失败）', retryable: true, details: {} } });
        });
      } else if (age > 6 * S && dyn.status === 'queued') {
        dyn.status = 'running';
        dyn.items.forEach((it) => { it.status = 'running'; });
      }
    },
  },
];

export function buildScenarioDb(scenarioId: string): MutableDb {
  const def = SCENARIOS.find((s) => s.id === scenarioId) ?? SCENARIOS[0];
  const db = createBaseDb(def.id);
  def.setup(db);
  return db;
}

export function tickDb(db: MutableDb): void {
  const def = SCENARIOS.find((s) => s.id === db.scenarioId);
  def?.tick?.(db, Date.now() - db.loadedAt);
}
