import { useState } from 'react';
import type { PapersApi } from '../../api-client/papers';
import { getDesktop } from '../../desktop';
import type { Health } from '../../shell/hooks';
import { StatusWord } from '../../ui/Card';
import { CopyButton, Segmented } from '../../ui/Controls';
import { Icon } from '../../ui/Icon';
import { useTheme } from '../../ui/useTheme';
import { THEME_PREF_LABELS, type ThemePref } from '../../theme';
import DocParseSettings from '../docparse/DocParseSettings';
import { Group, Row } from './SettingsParts';
import './settings.css';

// Settings: grouped rows (macOS / magpie style). The theme preference is the
// only thing persisted, by src/theme.ts. Desktop rows render only inside the
// Electron shell (window.p2rDesktop).
//   <Settings health={health} host="127.0.0.1:8010" />
const THEME_OPTIONS = (['system', 'light', 'dark'] as const).map(value => ({ value, label: THEME_PREF_LABELS[value] }));
const HEALTH = { up: { text: '已连接', tone: 'ok' }, down: { text: '未连接', tone: 'bad' }, checking: { text: '正在检查', tone: 'idle' } } as const;
const PLATFORM: Record<string, string> = { darwin: 'macOS', win32: 'Windows', linux: 'Linux' };

export default function Settings({ health, host, api }: { health: Health; host: string; api: PapersApi }) {
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

    <DocParseSettings api={api} />

    <Group title="自主实验" help="模型配置仅保存凭据引用；API Key 在新建实验页的当前内存中输入，不写入浏览器存储。SSH 使用本机预配置别名，实际连接与 GPU 能力以任务事件为准。">
      <Row label="模型、SSH 与科研 Skills" hint="配置预算、查看能力版本、导入或启用 Skills。"><a className="btn btn-secondary btn-sm" href="#/new">新建实验</a></Row>
    </Group>

    {desktop && <Group title="桌面应用">
      <Row label="版本"><span className="ui-mono">{desktop.appVersion}</span><span className="settings-muted">{PLATFORM[desktop.platform] ?? desktop.platform}</span></Row>
      <PathRow label="数据目录" path={desktop.dataDir} onOpen={() => desktop.openPath('data')} />
      <PathRow label="日志目录" path={desktop.logDir} onOpen={() => desktop.openPath('logs')} />
    </Group>}

    <Group title="关于">
      <div className="settings-about">
        <p><b>Paper2Run</b> 把一篇论文变成可执行、独立评估的实验：上传论文 → AI 规划并生成代码 → 隔离环境运行 → 冻结证据 → 独立评估 → 报告与导出。</p>
        <p className="settings-muted">结论由程序判定，不由模型给出；不可比较时报告会明说不可比较。全部在本机运行，SSH 远端能力以任务事件为准。</p>
      </div>
    </Group>
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
