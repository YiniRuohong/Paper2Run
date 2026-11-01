import { useState, type ReactNode } from 'react';
import { getDesktop } from '../../desktop';
import type { Health } from '../../shell/hooks';
import { StatusWord } from '../../ui/Card';
import { CopyButton, Segmented } from '../../ui/Controls';
import { Icon } from '../../ui/Icon';
import { useTheme } from '../../ui/useTheme';
import { THEME_PREF_LABELS, type ThemePref } from '../../theme';
import './settings.css';

// Settings: grouped rows (macOS / magpie style). The theme preference is the
// only thing persisted, by src/theme.ts. Desktop rows render only inside the
// Electron shell (window.p2rDesktop).
//   <Settings health={health} host="127.0.0.1:8010" />
const THEME_OPTIONS = (['system', 'light', 'dark'] as const).map(value => ({ value, label: THEME_PREF_LABELS[value] }));
const HEALTH = { up: { text: '已连接', tone: 'ok' }, down: { text: '未连接', tone: 'bad' }, checking: { text: '正在检查', tone: 'idle' } } as const;
const PLATFORM: Record<string, string> = { darwin: 'macOS', win32: 'Windows', linux: 'Linux' };

export default function Settings({ health, host }: { health: Health; host: string }) {
  const { pref, theme, setPref } = useTheme();
  const desktop = getDesktop();
  const address = desktop?.backendUrl?.replace(/\/+$/, '') || `${window.location.protocol}//${host}`;
  return <div className="settings">
    <Group title="外观">
      <Row label="主题" hint={pref === 'system' ? `跟随系统，当前为${theme === 'dark' ? '深色' : '浅色'}` : undefined}>
        <Segmented label="主题" value={pref} options={THEME_OPTIONS} onChange={(v: ThemePref) => setPref(v)} />
      </Row>
    </Group>

    <Group title="本地服务" help="后端只监听本机回环地址；页面与 API 由同一进程提供。">
      <Row label="地址"><span className="ui-mono">{address}</span><CopyButton value={address} label="复制地址" /></Row>
      <Row label="状态"><StatusWord tone={HEALTH[health].tone}>{HEALTH[health].text}</StatusWord></Row>
    </Group>

    {desktop && <Group title="桌面应用">
      <Row label="版本"><span className="ui-mono">{desktop.appVersion}</span><span className="settings-muted">{PLATFORM[desktop.platform] ?? desktop.platform}</span></Row>
      <PathRow label="数据目录" path={desktop.dataDir} onOpen={() => desktop.openPath('data')} />
      <PathRow label="日志目录" path={desktop.logDir} onOpen={() => desktop.openPath('logs')} />
    </Group>}

    <Group title="关于">
      <div className="settings-about">
        <p><b>Paper2Run</b> 把一篇论文变成可执行、独立评估的实验：上传论文 → 模型抽取实验方案 → 生成代码 → 本地沙箱或内置 CPU 基线运行 → 独立评估 → 报告与导出。</p>
        <p className="settings-muted">全部在本机运行；不是“复现任意论文”的通用产品，也不做 GPU 级复现承诺。证据等级由程序判定，不由模型给出。</p>
      </div>
    </Group>
  </div>;
}

function Group({ title, help, children }: { title: string; help?: string; children: ReactNode }) {
  return <section className="settings-group" aria-label={title}>
    <h2 className="settings-title">{title}</h2>
    <div className="settings-card">{children}</div>
    {help && <p className="ui-help settings-help">{help}</p>}
  </section>;
}

function Row({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return <div className="settings-row">
    <div className="settings-label"><span>{label}</span>{hint && <small>{hint}</small>}</div>
    <div className="settings-value">{children}</div>
  </div>;
}

function PathRow({ label, path, onOpen }: { label: string; path: string; onOpen: () => Promise<void> }) {
  const [failed, setFailed] = useState(false);
  return <Row label={label} hint={failed ? '无法打开该目录' : undefined}>
    <span className="ui-mono settings-path" title={path}>{path}</span>
    <CopyButton value={path} label={`复制${label}`} />
    <button type="button" className="btn btn-secondary btn-sm" onClick={() => { setFailed(false); onOpen().catch(() => setFailed(true)); }}><Icon name="folder" size={14} />打开</button>
  </Row>;
}
