import { useState } from 'react';
import { Dot, type Tone } from '../ui/Card';
import { AppMark, Icon } from '../ui/Icon';
import { taskDisplay } from '../features/autonomous/config';
import type { Health, RecentTask } from './hooks';

export type View = 'overview' | 'new' | 'task' | 'settings';

const HEALTH_TEXT: Record<Health, string> = { up: '后端已连接', down: '后端未连接', checking: '正在检查后端' };
const HEALTH_TONE = { up: 'ok', down: 'bad', checking: 'idle' } as const;

export const taskHref = (taskId: string): string => `#/task/${encodeURIComponent(taskId)}`;

// magpie-style sidebar: brand row (window-controls area on desktop), grouped
// nav, nested "最近" disclosure of recent tasks, footer with 设置 + backend dot.
export function Sidebar({ open: visible = true, view, taskId, recent, recentFailed, health, host, onHide }: {
  open?: boolean; view: View; taskId: string | null; recent: RecentTask[] | null; recentFailed: boolean; health: Health; host: string; onHide: () => void;
}) {
  const [open, setOpen] = useState(true);
  const item = (target: View) => ({ className: `side-item${view === target ? ' on' : ''}`, 'aria-current': view === target ? 'page' as const : undefined });
  // Stays mounted while collapsed so it can animate out; inert keeps it out of the tab order and the a11y tree.
  const hiddenProps = visible ? {} : ({ inert: 'true', 'aria-hidden': true } as Record<string, string | boolean>);
  return <aside className="side" id="app-sidebar" aria-label="侧栏" {...hiddenProps}>
    <div className="side-head drag">
      <button type="button" className="btn btn-icon" aria-label="隐藏侧栏" title="隐藏侧栏" aria-controls="app-sidebar" aria-expanded="true" onClick={onHide}><Icon name="sidebar" /></button>
      <span className="side-brand"><AppMark size={19} /><b>Paper2Run</b></span>
    </div>
    <nav className="side-nav" aria-label="主导航">
      <div className="side-group">
        <span className="side-label">工作区</span>
        <a href="#/" {...item('overview')}><Icon name="home" />总览</a>
        <a href="#/new" {...item('new')}><Icon name="plus" />新建实验</a>
      </div>
      <div className="side-group">
        <span className="side-label">任务</span>
        <button type="button" className="side-item side-disc" aria-expanded={open} aria-controls="side-recent" onClick={() => setOpen(o => !o)}>
          <Icon name="chevron-right" size={14} className={`side-chev${open ? ' is-open' : ''}`} />
          <span>最近</span>
          {recent && recent.length > 0 && <span className="side-count">{recent.length}</span>}
        </button>
        <div id="side-recent" hidden={!open}>
          {recent === null && !recentFailed && <div className="side-sub-empty"><span className="ui-skel w60" /></div>}
          {recent === null && recentFailed && <p className="side-sub-empty">暂时无法读取任务</p>}
          {recent && recent.length === 0 && <p className="side-sub-empty">还没有任务</p>}
          {recent && recent.length > 0 && <ul className="side-sub">
            {recent.map(t => {
              const shown = taskDisplay(t);
              const tone: Tone = shown.tone;
              const label = shown.lifecycle ? `${shown.text}，${shown.lifecycle}` : shown.text;
              const current = view === 'task' && taskId === t.id;
              return <li key={t.id}>
                <a href={taskHref(t.id)} className={`side-item side-sub-item${current ? ' on' : ''}`} aria-current={current ? 'page' : undefined} title={`${t.objective} · ${label}${shown.caption ? `；${shown.caption}` : ''}`}>
                  <Dot tone={tone} /><span className="side-sub-title">{t.objective}</span>
                  <span className="sr-only">（{label}）</span>
                </a>
              </li>;
            })}
          </ul>}
        </div>
      </div>
    </nav>
    <div className="side-foot">
      <a href="#/settings" {...item('settings')}><Icon name="settings" />设置</a>
      <div className="side-status" role="status" title={HEALTH_TEXT[health]}>
        <Dot tone={HEALTH_TONE[health]} />
        <span className="ui-mono">{host}</span>
        <span className="sr-only">{HEALTH_TEXT[health]}</span>
      </div>
    </div>
  </aside>;
}
