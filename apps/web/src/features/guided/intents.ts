import type { WriteIntent } from '../../api-client/guided-types.ts';

export interface IntentStorage { getItem(key: string): string | null; setItem(key: string, value: string): void; removeItem?(key: string): void }
export function storageKey(actor: string): string { return `p2r.guided.v1:${actor}`; }
export function prepareIntent(path: string, body: unknown): WriteIntent {
  return { key: crypto.randomUUID(), path, body: JSON.stringify(body), state: 'prepared' };
}
export function saveIntent(storage: IntentStorage, actor: string, intent: WriteIntent): void {
  const raw = storage.getItem(storageKey(actor));
  const current = raw ? JSON.parse(raw) as { actor: string; version: 1; intents: WriteIntent[] } : { actor, version: 1 as const, intents: [] };
  if (current.actor !== actor || current.version !== 1 || !Array.isArray(current.intents)) throw new Error('INVALID_STORAGE');
  storage.setItem(storageKey(actor), JSON.stringify({ actor, version: 1, intents: [...current.intents.filter(item => item.key !== intent.key), { key: intent.key, path: intent.path, body: intent.body, state: intent.state }] }));
}
export function restoreIntents(storage: IntentStorage, actor: string, allowedPaths: Set<string>): WriteIntent[] {
  const raw = storage.getItem(storageKey(actor));
  if (!raw) return [];
  try {
    const value = JSON.parse(raw) as { actor?: unknown; version?: unknown; intents?: unknown };
    if (value.actor !== actor || value.version !== 1 || !Array.isArray(value.intents)) return [];
    return value.intents.filter((item): item is WriteIntent => {
      if (!item || typeof item !== 'object') return false;
      const x = item as Record<string, unknown>;
      return typeof x.key === 'string' && typeof x.path === 'string' && allowedPaths.has(x.path) && typeof x.body === 'string' && x.body.length <= 100_000 && ['prepared', 'unknown', 'accepted'].includes(String(x.state));
    }).map(x => ({ key: x.key, path: x.path, body: x.body, state: x.state }));
  } catch { return []; }
}
