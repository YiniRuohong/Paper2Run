import type { ReactNode } from 'react';
import type { Export, Experiment, ExperimentMode, Paper, Run, Verdict } from '../api-client/types';

type TagKind = 'ok' | 'warn' | 'err' | 'dim';

export function Tag({ kind, children }: { kind: TagKind; children: ReactNode }) {
  return <span className={`tag ${kind}`}>{children}</span>;
}

const RUN_STATUS: Record<Run['status'], TagKind> = {
  queued: 'dim',
  generating: 'warn',
  executing: 'warn',
  repairing: 'warn',
  verifying: 'warn',
  cancel_requested: 'warn',
  completed: 'ok',
  failed: 'err',
  cancelled: 'dim',
  awaiting_revision: 'err',
};
const RUN_TEXT: Record<Run['status'], string> = {
  queued: '排队中',
  generating: '生成代码中',
  executing: '正在执行',
  repairing: '自动修复中',
  verifying: '独立评测中',
  cancel_requested: '停止确认中',
  completed: '执行完成',
  failed: '执行失败',
  cancelled: '已停止',
  awaiting_revision: '需要重新确认',
};

export function RunStatusTag({ status }: { status: Run['status'] }) {
  return <Tag kind={RUN_STATUS[status]}>{RUN_TEXT[status]}</Tag>;
}

const PAPER_TEXT: Record<Paper['status'], { kind: TagKind; text: string }> = {
  uploaded: { kind: 'dim', text: '已上传' },
  parsing: { kind: 'warn', text: '解析中' },
  ready: { kind: 'ok', text: '解析完成' },
  failed: { kind: 'err', text: '解析失败' },
  unsupported: { kind: 'err', text: '不支持' },
};
export function PaperStatusTag({ status }: { status: Paper['status'] }) {
  const v = PAPER_TEXT[status];
  return <Tag kind={v.kind}>{v.text}</Tag>;
}

const VERDICT: Record<Verdict, { kind: TagKind; text: string }> = {
  matched: { kind: 'ok', text: '指标在容差内' },
  outside_tolerance: { kind: 'warn', text: '未达约定容差' },
  not_comparable: { kind: 'dim', text: '条件不同，不可直接比较' },
  execution_failed: { kind: 'err', text: '未完成执行' },
  insufficient_evidence: { kind: 'dim', text: '证据不足' },
};
export function VerdictTag({ verdict }: { verdict: Verdict }) {
  const v = VERDICT[verdict];
  return <Tag kind={v.kind}>{v.text}</Tag>;
}

const MODE_TEXT: Record<ExperimentMode, string> = {
  smoke: 'smoke · 流程验证',
  scaled: 'scaled · 缩减实验',
  reference: 'reference · 原文协议',
};
export function ModeTag({ mode }: { mode: ExperimentMode }) {
  return <Tag kind="dim">{MODE_TEXT[mode]}</Tag>;
}

const EXPERIMENT_TEXT: Record<Experiment['status'], { kind: TagKind; text: string }> = {
  draft: { kind: 'dim', text: '草稿' },
  ready: { kind: 'warn', text: '待运行' },
  active: { kind: 'warn', text: '运行中' },
  needs_revision: { kind: 'err', text: '需修订' },
  closed: { kind: 'dim', text: '已关闭' },
};
export function ExperimentStatusTag({ status }: { status: Experiment['status'] }) {
  const v = EXPERIMENT_TEXT[status];
  return <Tag kind={v.kind}>{v.text}</Tag>;
}

const EXPORT_TEXT: Record<Export['status'], { kind: TagKind; text: string }> = {
  queued: { kind: 'dim', text: '排队中' },
  running: { kind: 'warn', text: '生成中' },
  succeeded: { kind: 'ok', text: '完成' },
  partial: { kind: 'warn', text: '部分完成' },
  failed: { kind: 'err', text: '失败' },
};
export function ExportStatusTag({ status }: { status: Export['status'] }) {
  const v = EXPORT_TEXT[status];
  return <Tag kind={v.kind}>{v.text}</Tag>;
}

const EXPORT_ITEM_TEXT: Record<'pending' | 'running' | 'ready' | 'failed', { kind: TagKind; text: string }> = {
  pending: { kind: 'dim', text: '等待' },
  running: { kind: 'warn', text: '生成中' },
  ready: { kind: 'ok', text: '可下载' },
  failed: { kind: 'err', text: '失败' },
};
export function ExportItemStatusTag({ status }: { status: 'pending' | 'running' | 'ready' | 'failed' }) {
  const v = EXPORT_ITEM_TEXT[status];
  return <Tag kind={v.kind}>{v.text}</Tag>;
}

export const EXPORT_FORMAT_TEXT: Record<Export['items'][number]['format'], string> = {
  bundle_zip: '证据包 ZIP',
  report_pdf: '报告 PDF',
  report_markdown: '报告 Markdown',
  report_json: '报告 JSON',
};
