import type { PaperInfo, PaperText } from '../../api-client/papers.ts';

// 新建实验页的纯函数：论文类型与启动阻断原因。

/** 论文是否为 PDF：以后端存储的 source_kind 为准，缺失时看 media_type。
 * 不能从 /text 的状态推断：粘贴的正文没有解析记录，状态同样是 none。
 * 两者都缺失时不臆造解析要求，服务端仍会校验论文正文是否就绪。 */
export function paperIsPdf(info: Pick<PaperInfo, 'source_kind' | 'media_type'>): boolean {
  return info.source_kind ? info.source_kind === 'pdf' : info.media_type === 'application/pdf';
}

export interface LaunchInput {
  projectId: string;
  paperVersion: string;
  pdf: boolean;
  text: PaperText | null;
  authorized: boolean;
  mainProfile: string;
  objective: string;
}

/** 「授权并启动」的禁用原因；空字符串表示可以启动。 */
export function launchBlockReason(input: LaunchInput): string {
  if (!input.projectId) return '请先选择或创建项目';
  if (!input.paperVersion) return '请先上传或选择论文';
  if (input.pdf && input.text?.status !== 'ready') {
    const status = input.text?.status;
    const label = status === 'failed' ? '解析失败' : status === 'parsing' ? '解析中' : '尚未解析';
    return `论文解析未完成（${label}），不能启动`;
  }
  if (!input.authorized) return '请先勾选授权';
  if (!input.mainProfile) return '请先选择主模型';
  if (!input.objective.trim()) return '请填写实验目标';
  return '';
}
