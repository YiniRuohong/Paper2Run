// 环境检测接口（GET /env/check、POST /env/image/load）。响应按不可信输入校验，形状不对就整体拒绝。

export type EnvStatus = 'ok' | 'warn' | 'bad';
export interface EnvRow { key: string; label: string; status: EnvStatus; detail: string; fix: string }
export interface EnvImageLoad { state: string; error: string | null }
export interface EnvCheck { platform: string; ok: boolean; checks: EnvRow[]; image_load: EnvImageLoad }

const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);

function parseImageLoad(value: unknown): EnvImageLoad | null {
  if (!isObject(value) || typeof value.state !== 'string') return null;
  return { state: value.state, error: typeof value.error === 'string' ? value.error : null };
}

export function parseEnvCheck(value: unknown): EnvCheck | null {
  if (!isObject(value) || typeof value.platform !== 'string' || typeof value.ok !== 'boolean' || !Array.isArray(value.checks)) return null;
  const imageLoad = parseImageLoad(value.image_load);
  if (!imageLoad) return null;
  const checks: EnvRow[] = [];
  for (const item of value.checks) {
    if (!isObject(item) || typeof item.key !== 'string' || typeof item.label !== 'string' || typeof item.detail !== 'string') return null;
    if (item.status !== 'ok' && item.status !== 'warn' && item.status !== 'bad') return null;
    checks.push({ key: item.key, label: item.label, status: item.status, detail: item.detail, fix: typeof item.fix === 'string' ? item.fix : '' });
  }
  return { platform: value.platform, ok: value.ok, checks, image_load: imageLoad };
}

export function createEnvApi() {
  async function call(path: string, method: 'GET' | 'POST', signal?: AbortSignal): Promise<unknown> {
    let response: Response;
    try {
      response = await fetch(`/api/v1${path}`, { method, headers: { Accept: 'application/json' }, credentials: 'same-origin', cache: 'no-store', ...(signal ? { signal } : {}) });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new Error('网络连接失败');
    }
    if (!response.ok) throw new Error(`请求失败 (${response.status})`);
    const payload: unknown = await response.json();
    if (!isObject(payload)) throw new Error('响应格式无法识别');
    return payload.data;
  }
  return {
    async check(signal?: AbortSignal): Promise<EnvCheck> {
      const parsed = parseEnvCheck(await call('/env/check', 'GET', signal));
      if (!parsed) throw new Error('环境检测结果格式无法识别');
      return parsed;
    },
    async loadImage(): Promise<EnvImageLoad> {
      const parsed = parseImageLoad(await call('/env/image/load', 'POST'));
      if (!parsed) throw new Error('导入结果格式无法识别');
      return parsed;
    },
  };
}
