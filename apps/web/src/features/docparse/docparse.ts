import type { DocParseMode, DocParseStatus, PaperText } from '../../api-client/guided-types.ts';

// Pure docparse helpers shared by the guided controller and (next) the UI.
// Nothing here talks to the network or decides a mode; it only reads DTOs.

// A parse is still owed for this paper: nothing was ever parsed, or the last
// attempt failed only because the offline component was not ready yet.
export function isParsePending(text: PaperText | null): boolean {
  if (text === null) return true;
  return text.status === 'none' || (text.status === 'failed' && text.error_code === 'DOCPARSE_OFFLINE_NOT_READY');
}

// The server can start a parse right now with the given (chosen) mode.
export function canParseNow(status: DocParseStatus, mode: DocParseMode | null = status.mode): boolean {
  if (mode === null || status.mode !== mode) return false;
  return mode === 'online' || status.offline.state === 'ready';
}

// Text papers can always be extracted; a PDF only once its parsed text exists.
export function canExtract(state: { docparse: { pdf: boolean; text: PaperText | null } }): boolean {
  return !state.docparse.pdf || state.docparse.text?.status === 'ready';
}

const OFFLINE_LABELS: Record<DocParseStatus['offline']['state'], string> = {
  not_installed: '离线组件未安装',
  installing: '离线组件安装中',
  ready: '等待解析',
  broken: '离线组件已损坏',
  failed: '离线组件安装失败',
  unsupported: '当前平台不支持离线解析',
};

export function docParseLabel(status: DocParseStatus | null, text: PaperText | null): string {
  if (text?.status === 'ready') return '解析完成';
  if (text?.status === 'parsing') {
    const total = text.chunks.length;
    const done = text.chunks.filter(chunk => chunk.status === 'done').length;
    return total > 0 ? `解析中 · 第 ${done}/${total} 段` : '解析中';
  }
  if (isParsePending(text) && status !== null) {
    if (status.mode === null) return '等待选择解析方式';
    if (status.mode === 'offline') {
      const label = OFFLINE_LABELS[status.offline.state];
      return status.offline.state === 'installing' && status.offline.step ? `${label} · ${status.offline.step}` : label;
    }
    if (text?.status !== 'failed') return '等待解析';
  }
  if (text?.status === 'failed') return '解析失败';
  return '读取解析状态…';
}
