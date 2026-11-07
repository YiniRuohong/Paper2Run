import { useCallback, useEffect, useRef, useState } from 'react';
import { createEnvApi, type EnvCheck, type EnvStatus } from '../../api-client/envcheck';
import { StatusWord, type Tone } from '../../ui/Card';
import '../docparse/docparse.css';
import { Group, Row } from './SettingsParts';

// 设置组「环境检测」：虚拟化、WSL2、Docker、实验镜像、磁盘、国内镜像源，逐项给出状态、原因和处理办法。
// 实验镜像缺失时可手动导入安装包内置的镜像；导入进行时每 3 秒刷新一次。
const POLL_MS = 3000;
const TONE: Record<EnvStatus, Tone> = { ok: 'ok', warn: 'warn', bad: 'bad' };
const WORD: Record<EnvStatus, string> = { ok: '正常', warn: '注意', bad: '不满足' };
const api = createEnvApi();

export default function EnvCheckSettings() {
  const [result, setResult] = useState<EnvCheck | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    try {
      const next = await api.check(signal);
      if (alive.current && !signal?.aborted) { setResult(next); setFailed(false); }
    } catch {
      if (alive.current && !signal?.aborted) setFailed(true);
    }
  }, []);

  useEffect(() => {
    const abort = new AbortController();
    void refresh(abort.signal);
    return () => abort.abort();
  }, [refresh]);

  const loading = result?.image_load.state === 'loading';
  useEffect(() => {
    if (!loading) return;
    const abort = new AbortController();
    const timer = window.setInterval(() => { void refresh(abort.signal); }, POLL_MS);
    return () => { window.clearInterval(timer); abort.abort(); };
  }, [loading, refresh]);

  const recheck = async () => {
    if (busy) return;
    setBusy(true);
    await refresh();
    if (alive.current) setBusy(false);
  };
  const loadImage = async () => {
    if (busy) return;
    setBusy(true);
    try { await api.loadImage(); } catch { if (alive.current) setFailed(true); }
    await refresh();
    if (alive.current) setBusy(false);
  };

  if (result === null) {
    return <Group title="环境检测">
      <Row label="检测结果"><span className="settings-muted">{failed ? '无法读取环境检测结果' : '检测中…（最多约 25 秒）'}</span>
        {failed && <button type="button" className="btn btn-secondary btn-sm" onClick={() => { void recheck(); }}>重试</button>}
      </Row>
    </Group>;
  }

  const imageError = result.image_load.state === 'failed' ? result.image_load.error : null;
  return <Group title="环境检测" help="检查本机是否满足运行实验的条件：虚拟化、WSL2、Docker、实验镜像、磁盘空间和国内镜像源。">
    <Row label="总体">
      <div className="dp-offline">
        <div className="dp-line">
          <StatusWord tone={result.ok ? 'ok' : 'bad'}>{result.ok ? '满足运行要求' : '存在不满足项'}</StatusWord>
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy} onClick={() => { void recheck(); }}>重新检测</button>
        </div>
        {failed && <p className="dp-hint" role="alert">操作未完成或检测失败，请稍后重试。</p>}
      </div>
    </Row>
    {result.checks.map(row => <Row key={row.key} label={row.label}>
      <div className="dp-offline">
        <div className="dp-line">
          <StatusWord tone={TONE[row.status]}>{WORD[row.status]}</StatusWord>
          <span className="dp-meta">{row.detail}</span>
        </div>
        {row.status !== 'ok' && row.fix && <p className="dp-hint" role="status">处理办法：{row.fix}</p>}
        {row.key === 'image' && row.status !== 'ok' && <div className="dp-line">
          <button type="button" className="btn btn-secondary btn-sm" disabled={busy || loading} onClick={() => { void loadImage(); }}>{loading ? '导入中…' : '导入内置镜像'}</button>
          {imageError && <span className="dp-meta">上次导入失败：{imageError}</span>}
        </div>}
      </div>
    </Row>)}
  </Group>;
}
