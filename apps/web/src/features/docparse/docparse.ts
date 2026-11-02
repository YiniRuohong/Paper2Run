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

// Fixed public error codes map to one specific reason each. Anything else,
// including null, gets the generic line: raw server text is never shown.
const ERROR_REASONS = new Map<string, string>([
  ['DOCPARSE_OFFLINE_NOT_READY', '离线组件未就绪，请先完成安装'],
  ['DOCPARSE_OFFLINE_FAILED', '离线解析失败'],
  ['DOCPARSE_OFFLINE_UNSUPPORTED', '本机不支持离线解析'],
  ['DOCPARSE_RATE_LIMITED', '在线服务请求过于频繁，请稍后重试'],
  ['DOCPARSE_REMOTE_FAILED', '在线解析服务失败'],
  ['DOCPARSE_TIMEOUT', '解析超时'],
  ['DOCPARSE_PDF_INVALID', 'PDF 无法读取（可能已加密或损坏）'],
  ['DOCPARSE_PAGE_TOO_LARGE', '单页体积过大，在线解析无法处理，可改用离线解析'],
  ['DOCPARSE_UNSAFE_URL', '在线服务返回了不安全或过大的内容，已拒绝'],
  ['DOCPARSE_RESPONSE_TOO_LARGE', '在线服务返回了不安全或过大的内容，已拒绝'],
  ['DOCPARSE_CANCELLED', '解析已取消'],
  ['DOCPARSE_MODE_UNSET', '尚未选择解析方式'],
]);
const UNKNOWN_ERROR_REASON = '解析失败（未知原因）';

export function docParseErrorReason(code: string | null): string {
  return (code !== null && ERROR_REASONS.get(code)) || UNKNOWN_ERROR_REASON;
}

// Disk usage of the offline component, MB below one GB, GB above.
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 MB';
  const gb = bytes / 1024 ** 3;
  if (gb >= 1) return `${gb.toFixed(1)} GB`;
  return `${Math.max(1, Math.round(bytes / 1024 ** 2))} MB`;
}

// The mode a user could switch to from the current one, or null when there is
// no current mode or the other backend is unavailable on this platform. Used
// only to decide which explicit button to show; nothing switches by itself.
export function otherParseMode(status: DocParseStatus | null): DocParseMode | null {
  if (status === null || status.mode === null) return null;
  if (status.mode === 'online') return status.offline.state === 'unsupported' ? null : 'offline';
  return 'online';
}
