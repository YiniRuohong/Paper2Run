import { useCallback, useEffect, useRef, useState } from 'react';
import type { GuidedApi } from '../../api-client/guided';
import type { DocParseMode, DocParseOfflineState, DocParseStatus } from '../../api-client/guided-types';
import { StatusWord, type Tone } from '../../ui/Card';
import { Segmented } from '../../ui/Controls';
import { Group, Row } from '../settings/SettingsParts';
import { DOWNLOAD_NOTE, canUninstallOffline, formatBytes, offlineNotices } from './docparse';
import './docparse.css';

// Settings group 「PDF 解析」: the current parse mode and the offline component.
// Changing the mode only calls the API and re-reads status; choosing 离线 never
// installs by itself (installing is its own 安装 button). Status is polled every
// 3 s only while the component installs and only while this group is mounted.
const POLL_MS = 3000;
const STATE_LABEL: Record<DocParseOfflineState, string> = {
  not_installed: '未安装', installing: '安装中', ready: '已就绪', broken: '已损坏', failed: '安装失败', unsupported: '当前平台不支持离线解析',
};
const STATE_TONE: Record<DocParseOfflineState, Tone> = {
  not_installed: 'idle', installing: 'run', ready: 'ok', broken: 'bad', failed: 'bad', unsupported: 'idle',
};
const ACTION_FAILED = '操作未完成，请稍后重试。';
type Choice = DocParseMode | '';

export default function DocParseSettings({ api }: { api: GuidedApi }) {
  const [status, setStatus] = useState<DocParseStatus | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await api.getDocParseStatus(signal);
      if (alive.current && !signal?.aborted) { setStatus(next); setLoadFailed(false); }
    } catch {
      if (alive.current && !signal?.aborted) setLoadFailed(true);
    }
  }, [api]);

  useEffect(() => {
    const abort = new AbortController();
    void refresh(abort.signal);
    return () => abort.abort();
  }, [refresh]);

  const installing = status?.offline.state === 'installing';
  useEffect(() => {
    if (!installing) return;
    const abort = new AbortController();
    const timer = window.setInterval(() => { void refresh(abort.signal); }, POLL_MS);
    return () => { window.clearInterval(timer); abort.abort(); };
  }, [installing, refresh]);

  const run = async (command: () => Promise<DocParseStatus>) => {
    if (busy) return;
    setBusy(true); setFailed(false);
    try {
      const next = await command();
      if (alive.current) setStatus(next);
      await refresh();
    } catch {
      if (alive.current) setFailed(true);
      await refresh();
    } finally {
      if (alive.current) setBusy(false);
    }
  };

  if (status === null) {
    return <Group title="PDF 解析">
      <Row label="当前方式"><span className="settings-muted">{loadFailed ? '无法读取解析设置' : '读取中…'}</span>
        {loadFailed && <button type="button" className="btn btn-secondary btn-sm" onClick={() => { void refresh(); }}>重试</button>}
      </Row>
    </Group>;
  }

  const { offline } = status;
  const unsupported = offline.state === 'unsupported';
  const options: Array<{ value: Choice; label: string }> = [{ value: 'online', label: '在线' }, ...(unsupported ? [] : [{ value: 'offline' as const, label: '离线' }])];
  const canInstall = offline.state === 'not_installed' || offline.state === 'failed' || offline.state === 'broken';
  const canUninstall = canUninstallOffline(offline);
  const notices = offlineNotices(offline);
  const needsInstallHint = status.mode === 'offline' && !unsupported && offline.state !== 'ready';
  const detail = [offline.tier ? `档位 ${offline.tier}` : null, offline.bytes_on_disk > 0 ? `占用 ${formatBytes(offline.bytes_on_disk)}` : null].filter(Boolean).join(' · ');

  return <Group title="PDF 解析" help="在线解析会把论文上传到 MinerU 官方服务器（mineru.net）；离线解析完全在本机进行。">
    <Row label="当前方式" hint={status.mode === null ? '尚未选择' : undefined}>
      <div className="dp-offline">
        <Segmented<Choice> label="当前方式" value={status.mode ?? ''} options={options} disabled={busy} onChange={value => { if (value !== '') void run(() => api.setDocParseMode(value)); }} />
        {needsInstallHint && <p className="dp-hint">需要先安装离线组件才能解析。</p>}
      </div>
    </Row>
    <Row label="离线组件">
      <div className="dp-offline">
        <div className="dp-line">
          <StatusWord tone={STATE_TONE[offline.state]}>{STATE_LABEL[offline.state]}</StatusWord>
          {detail && <span className="dp-meta">{detail}</span>}
        </div>
        {notices.map(notice => <p key={notice} className="dp-hint" role="status">{notice}</p>)}
        {installing && <div className="dp-line">
          <progress className="dp-bar" value={offline.progress} max={1} aria-label="离线组件安装进度" />
          <span className="dp-meta">{Math.round(offline.progress * 100)}%{offline.step ? ` · ${offline.step}` : ''}</span>
        </div>}
        <div className="dp-line">
          {canInstall && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { void run(() => api.startOfflineInstall()); }}>安装</button>}
          {canInstall && <span className="dp-meta">{DOWNLOAD_NOTE}</span>}
          {installing && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { void run(() => api.cancelOfflineInstall()); }}>取消</button>}
          {canUninstall && <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { void run(() => api.uninstallOffline()); }}>卸载</button>}
        </div>
        {failed && <p className="dp-hint" role="alert">{ACTION_FAILED}</p>}
      </div>
    </Row>
  </Group>;
}
