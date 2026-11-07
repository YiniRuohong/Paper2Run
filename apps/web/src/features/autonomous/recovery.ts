import type { AutonomousEvent, ModelProfile, SaveProfileRequest } from '../../api-client/autonomous.ts';
import { terminalState } from './config.ts';

// 任务页「需要恢复」的纯函数。模型配置只存在服务端内存里：应用重启后
// api.profiles() 为空，但任务快照里冻结了公开的主模型配置（无密钥）。

export interface FrozenProfile {
  id: string; base_url: string; model: string; api: ModelProfile['api'];
  auth_mode: 'api-key' | 'none'; credential_ref?: string; context_window?: number; max_tokens?: number;
}

const APIS = new Set(['openai-completions', 'anthropic-messages']);
const text = (value: unknown) => typeof value === 'string' && value.length > 0;
const positive = (value: unknown): value is number => typeof value === 'number' && Number.isSafeInteger(value) && value > 0;

/** 任务快照中冻结的主模型公开配置（service.create_task 写入 model_profile）；字段不全时为 null。 */
export function frozenMainProfile(snapshot: unknown): FrozenProfile | null {
  const value = snapshot && typeof snapshot === 'object' ? (snapshot as Record<string, unknown>).model_profile : null;
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (!text(item.id) || !text(item.base_url) || !text(item.model) || !APIS.has(item.api as string)) return null;
  return {
    id: item.id as string, base_url: item.base_url as string, model: item.model as string, api: item.api as ModelProfile['api'],
    auth_mode: item.auth_mode === 'none' ? 'none' : 'api-key',
    ...(text(item.credential_ref) ? { credential_ref: item.credential_ref as string } : {}),
    ...(positive(item.context_window) ? { context_window: item.context_window } : {}),
    ...(positive(item.max_tokens) ? { max_tokens: item.max_tokens } : {}),
  };
}

/** 重新注入密钥的保存请求。有在线配置时沿用它；没有时用冻结配置的
 * id、base_url、model、api 重新保存，后端 resolve 要求这四项与冻结快照一致。 */
export function recoveryProfileRequest(live: ModelProfile | null, snapshot: unknown, key: string): SaveProfileRequest | null {
  const apiKey = key.trim();
  if (!apiKey) return null;
  if (live) return { ...live, api_key: apiKey };
  const frozen = frozenMainProfile(snapshot);
  if (!frozen || frozen.auth_mode === 'none') return null;
  const { credential_ref: _ref, ...settings } = frozen;
  return { ...settings, api_key: apiKey };
}

export interface Recovery { needed: boolean; retainedJobs: string[] }
export interface RecoveryInput {
  profiles: ModelProfile[];
  /** api.profiles() 是否已返回；未返回前不根据「配置缺失」判断。 */
  profilesLoaded: boolean;
  /** 本页最近一次控制操作失败的错误码。 */
  lastControlError: string | null;
  keysInjected: boolean;
}

type RecoveryTask = { state: string; outcome?: string | null; config_snapshot?: unknown };

/** 是否显示「需要恢复」卡片：
 * - 最近一次恢复类事件是 recovery_required（应用退出时任务在跑）；
 * - 或最近一次控制操作返回 CREDENTIAL_REQUIRED；
 * - 或任务已暂停而主模型配置不在内存中（重启前就已暂停的任务不会写 recovery_required）。
 * 有结局或已结束的任务不需要恢复。 */
export function recoveryState(task: RecoveryTask, events: AutonomousEvent[], input: RecoveryInput): Recovery {
  let recovery: Recovery = { needed: false, retainedJobs: [] };
  for (const event of events) {
    if (event.type === 'recovery_required') recovery = { needed: true, retainedJobs: [] };
    if (event.type === 'task_resumed' || event.type === 'task_finished') recovery = { needed: false, retainedJobs: [] };
    if ((event.type === 'task_paused' || event.type === 'application_exit') && recovery.needed) {
      const retained = (event.payload as Record<string, unknown> | null)?.background_jobs_retained;
      if (Array.isArray(retained)) recovery = { ...recovery, retainedJobs: retained.map(String) };
    }
  }
  if (terminalState(task.state) || task.outcome != null) return { needed: false, retainedJobs: [] };
  if (input.lastControlError === 'CREDENTIAL_REQUIRED') recovery = { ...recovery, needed: true };
  if (task.state === 'paused' && input.profilesLoaded && profileMissing(task.config_snapshot, input.profiles)) recovery = { ...recovery, needed: true };
  return input.keysInjected ? { ...recovery, needed: false } : recovery;
}

function profileMissing(snapshot: unknown, profiles: ModelProfile[]): boolean {
  const frozen = frozenMainProfile(snapshot);
  const id = frozen?.id ?? ((snapshot as { model?: { provider_profile?: unknown } } | null)?.model?.provider_profile);
  if (typeof id !== 'string' || !id) return false;
  // 无认证的回环配置没有密钥可丢；环境变量凭据由服务端注入。
  if (frozen && (frozen.auth_mode === 'none' || frozen.credential_ref?.startsWith('env:'))) return false;
  const live = profiles.find(p => p.id === id);
  return !live || (live.auth_mode !== 'none' && live.secret_configured === false);
}
