import { useCallback, useEffect, useRef, useState } from 'react';
import { createRemoteApi, formatCountdown, remoteView, RemoteApiError, type PairingState, type RemoteDevice, type RemoteStatus } from '../../api-client/remote';
import { StatusWord } from '../../ui/Card';
import { Group, Row } from './SettingsParts';

// "平板远程" settings group. On the desktop: generate a one-time pairing code,
// watch it get used, list and revoke paired tablets. Through the tablet the
// same group is read-only (the backend refuses writes from remote requests).
const api = createRemoteApi();
const STATUS_POLL_MS = 5000;
const PAIRING_POLL_MS = 2000;

function when(unix: number | null): string {
  return unix ? new Date(unix * 1000).toLocaleString() : '—';
}

function errorText(e: unknown): string {
  if (e instanceof RemoteApiError) {
    if (e.code === 'REMOTE_UNAVAILABLE') return '远程服务未运行';
    if (e.code === 'REMOTE_FORBIDDEN') return '只能在电脑上操作';
    return e.message || e.code;
  }
  return e instanceof Error ? e.message : String(e);
}

export default function RemoteSettings() {
  const [status, setStatus] = useState<RemoteStatus | null>(null);
  const [pairing, setPairing] = useState<PairingState | null>(null);
  const [deadline, setDeadline] = useState(0);
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const s = await api.status();
      if (alive.current) setStatus(s);
    } catch (e) {
      if (alive.current) setError(errorText(e));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void refresh();
    const id = window.setInterval(() => { if (document.visibilityState !== 'hidden') void refresh(); }, STATUS_POLL_MS);
    return () => { alive.current = false; window.clearInterval(id); };
  }, [refresh]);

  // While a code is live, poll its state and tick the countdown.
  const waiting = pairing?.state === 'waiting';
  useEffect(() => {
    if (!waiting) return;
    const tick = window.setInterval(() => setNow(Date.now()), 500);
    const poll = window.setInterval(async () => {
      try {
        const st = await api.pairingState();
        if (!alive.current) return;
        setPairing(st);
        if (st.state === 'paired') void refresh();
      } catch (e) {
        if (alive.current) setError(errorText(e));
      }
    }, PAIRING_POLL_MS);
    return () => { window.clearInterval(tick); window.clearInterval(poll); };
  }, [waiting, refresh]);

  async function start() {
    setBusy(true); setError(null);
    try {
      const started = await api.startPairing();
      setPairing({ state: 'waiting', ...started });
      setDeadline(Date.now() + started.expires_in_ms);
      setNow(Date.now());
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  async function revoke(d: RemoteDevice) {
    if (!window.confirm(`撤销「${d.name}」？撤销后这台平板会立即断开，需要重新配对。`)) return;
    setError(null);
    try {
      await api.revoke(d.id);
      await refresh();
    } catch (e) {
      setError(errorText(e));
    }
  }

  if (!status) {
    return <Group title="平板远程"><Row label="状态"><span className="settings-muted">{error ?? '正在读取'}</span></Row></Group>;
  }
  const view = remoteView(status);
  const help = view.kind === 'tablet'
    ? '你正在通过平板访问。配对和撤销只能在电脑上操作。'
    : '在平板上安装「Paper2Run 远程」，输入这里生成的配对码即可。配对码 5 分钟内有效、只能用一次，输错一次即作废。';

  return <Group title="平板远程" help={help}>
    {view.kind === 'off' && <Row label="状态" hint="用一键启动打开 Paper2Run 时会自动启动；需要本机装有 Rust（cargo）。">
      <StatusWord tone="bad">远程服务未运行</StatusWord>
    </Row>}

    {view.canManage && <Row label="配对新平板" hint={pairingHint(pairing)}>
      {pairing?.state === 'waiting' && <>
        <span className="ui-mono remote-code" aria-label="配对码">{pairing.code}</span>
        <span className="settings-muted">{formatCountdown(deadline - now)}</span>
      </>}
      <button type="button" className="btn btn-primary btn-sm" disabled={busy} onClick={() => void start()}>
        {pairing?.state === 'waiting' ? '重新生成' : '生成配对码'}
      </button>
    </Row>}

    {view.kind !== 'off' && view.devices.length === 0 && <Row label="已配对的平板"><span className="settings-muted">还没有</span></Row>}
    {view.devices.map(d => <Row key={d.id} label={d.name} hint={`配对于 ${when(d.paired_at)} · 最近在线 ${when(d.last_seen)}`}>
      <StatusWord tone={d.online ? 'ok' : 'idle'}>{d.online ? (d.path === 'direct' ? '在线 · 直连' : d.path === 'relay' ? '在线 · 经中继' : '在线') : '离线'}</StatusWord>
      {view.canManage && <button type="button" className="btn btn-secondary btn-sm" onClick={() => void revoke(d)}>撤销</button>}
    </Row>)}

    {error && <Row label="出错"><span className="settings-muted">{error}</span></Row>}
  </Group>;
}

function pairingHint(p: PairingState | null): string | undefined {
  switch (p?.state) {
    case 'waiting': return '在平板上的「Paper2Run 远程」里输入这串数字';
    case 'paired': return `已配对：${p.device.name}`;
    case 'expired': return '配对码已过期，请重新生成';
    case 'wrong_code': return '平板输错了配对码，这个码已作废，请重新生成';
    default: return undefined;
  }
}
