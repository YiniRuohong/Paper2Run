// Tablet remote: pairing codes and paired devices. The backend proxies these to
// the p2r-tunnel sidecar; requests that came through the tunnel are marked
// remote and may only read.
//   const api = createRemoteApi();
//   const view = remoteView(await api.status());

const BASE = '/api/v1/remote';

export type RemotePath = 'direct' | 'relay';

export interface RemoteDevice {
  id: string;
  name: string;
  paired_at: number;
  online: boolean;
  path: RemotePath | null;
  last_seen: number | null;
}

export interface RemoteStatus {
  available: boolean;
  remote: boolean;
  desktop_id?: string;
  relay?: string;
  devices?: RemoteDevice[];
}

export interface PairingStarted {
  code: string;
  expires_in_ms: number;
}

export type PairingState =
  | { state: 'none' | 'expired' | 'wrong_code' }
  | { state: 'waiting'; code: string; expires_in_ms: number }
  | { state: 'paired'; device: { id: string; name: string; paired_at: number } };

export class RemoteApiError extends Error {
  readonly code: string;
  readonly status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = 'RemoteApiError';
    this.code = code;
    this.status = status;
  }
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function bad(what: string): never {
  throw new RemoteApiError('BAD_RESPONSE', `unexpected ${what} from server`, 0);
}

function parseDevice(v: unknown): RemoteDevice {
  if (!isObject(v) || typeof v.id !== 'string' || typeof v.name !== 'string' || typeof v.paired_at !== 'number' || typeof v.online !== 'boolean') bad('device');
  const path = v.path === 'direct' || v.path === 'relay' ? v.path : null;
  const lastSeen = typeof v.last_seen === 'number' ? v.last_seen : null;
  return { id: v.id, name: v.name, paired_at: v.paired_at, online: v.online, path, last_seen: lastSeen };
}

function parseStatus(v: unknown): RemoteStatus {
  if (!isObject(v) || typeof v.available !== 'boolean' || typeof v.remote !== 'boolean') bad('status');
  if (!v.available) return { available: false, remote: v.remote };
  if (!Array.isArray(v.devices)) bad('device list');
  return {
    available: true,
    remote: v.remote,
    desktop_id: typeof v.desktop_id === 'string' ? v.desktop_id : undefined,
    relay: typeof v.relay === 'string' ? v.relay : undefined,
    devices: v.devices.map(parseDevice),
  };
}

function parseStarted(v: unknown): PairingStarted {
  if (!isObject(v) || typeof v.code !== 'string' || typeof v.expires_in_ms !== 'number') bad('pairing code');
  return { code: v.code, expires_in_ms: v.expires_in_ms };
}

function parseState(v: unknown): PairingState {
  if (!isObject(v)) bad('pairing state');
  switch (v.state) {
    case 'none': case 'expired': case 'wrong_code':
      return { state: v.state };
    case 'waiting':
      return { state: 'waiting', ...parseStarted(v) };
    case 'paired': {
      const d = v.device;
      if (!isObject(d) || typeof d.id !== 'string' || typeof d.name !== 'string' || typeof d.paired_at !== 'number') bad('paired device');
      return { state: 'paired', device: { id: d.id, name: d.name, paired_at: d.paired_at } };
    }
    default:
      return bad('pairing state');
  }
}

export function createRemoteApi(fetcher: typeof fetch = (...a) => fetch(...a)) {
  async function call(method: string, path: string): Promise<unknown> {
    const res = await fetcher(`${BASE}${path}`, { method, headers: { Accept: 'application/json' } });
    if (res.status === 204) return null;
    let body: unknown = null;
    try { body = await res.json(); } catch { /* handled below */ }
    if (!res.ok) {
      const e = isObject(body) && isObject(body.error) ? body.error : {};
      throw new RemoteApiError(typeof e.code === 'string' ? e.code : `HTTP_${res.status}`, typeof e.message === 'string' ? e.message : res.statusText, res.status);
    }
    if (!isObject(body) || !('data' in body)) bad('envelope');
    return body.data;
  }
  return {
    status: async () => parseStatus(await call('GET', '/status')),
    startPairing: async () => parseStarted(await call('POST', '/pairing')),
    pairingState: async () => parseState(await call('GET', '/pairing')),
    revoke: async (id: string) => { await call('DELETE', `/devices/${encodeURIComponent(id)}`); },
  };
}

export type RemoteApi = ReturnType<typeof createRemoteApi>;

export interface RemoteView {
  kind: 'off' | 'desktop' | 'tablet';
  canManage: boolean;
  devices: RemoteDevice[];
}

/** What the settings group shows: sidecar off, managing desktop, or read-only tablet. */
export function remoteView(s: RemoteStatus): RemoteView {
  if (s.remote) return { kind: 'tablet', canManage: false, devices: s.devices ?? [] };
  if (!s.available) return { kind: 'off', canManage: false, devices: [] };
  return { kind: 'desktop', canManage: true, devices: s.devices ?? [] };
}

export function formatCountdown(ms: number): string {
  const total = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}
