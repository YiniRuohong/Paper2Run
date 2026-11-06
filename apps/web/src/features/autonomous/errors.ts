import { ApiError } from '../../api-client/client.ts';
import { errorExplanation } from './config.ts';

// 已知错误码在后端消息之后附中文说明（保留 missing: … 等细节）；code 与 request_id 始终保留。
export const errorDetail = (error: unknown) =>
  error instanceof ApiError
    ? `${error.message}${errorExplanation(error.code) ? `；${errorExplanation(error.code)}` : ''} (${error.code}${error.requestId ? ` · ${error.requestId}` : ''})`
    : error instanceof Error ? error.message : String(error);
