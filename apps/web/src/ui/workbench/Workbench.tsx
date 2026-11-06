import { Fragment, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Icon, type IconName } from '../Icon';
import type { Tone } from '../Card';
import { CopyButton } from '../Controls';
import { ancestors, baseName, buildTree, countFolders, fileTone, filterFiles, formatBytes, parseCsv, previewKind, type WbFile, type WbFolderNode, type WbNode, type WbTag } from './tree';
import './workbench.css';

export type { WbFile, WbTag } from './tree';

// Three-column task workbench (shared by 新建任务 / 任务详情 / 自主实验):
//   left   stage timeline — each stage collapses to a header, expands to its sub-steps
//   centre viewer — a "实时" tab with the page's own live controls (children),
//          plus closable tabs for opened files; download
//   right  file tree of the task's real files and records, searchable
// The live content is passed as children on purpose: it stays a normal part of
// the caller's React tree (forms, focus, tests that walk children).

export type StageMark = 'done' | 'current' | 'running' | 'attention' | 'failed' | 'open' | 'locked';
export interface WbStageItem {
  id: string;
  label: string;
  detail?: string | null;
  time?: string | null;
  tone?: Tone;
  /** Opens this file (by WbFile.path) in the viewer when clicked. */
  openPath?: string;
  /** Sub-steps nested under this item (e.g. tool calls under a Pi progress note). */
  children?: WbStageItem[];
}
export interface WbStage {
  id: string;
  title: string;
  mark: StageMark;
  summary?: string | null;
  items?: WbStageItem[];
  /** Selecting a stage calls onSelectStage; locked stages are inert. */
  disabled?: boolean;
  /** Group heading shown above this stage; consecutive stages with the same group share it. */
  group?: string;
}

/** Content returned by a loader: text is rendered directly, a Blob is previewed by type. */
export type WbContent = { text: string } | { blob: Blob };

export interface WorkbenchProps {
  eyebrow?: ReactNode;
  title: ReactNode;
  status?: { text: string; tone: Tone; /** Tooltip, e.g. a caveat that must stay reachable. */ title?: string } | null;
  meta?: ReactNode;
  actions?: ReactNode;
  progress?: ReactNode;
  /** Replaces the "实时" tab label with a thin progress bar (and hides `progress`). Clicking it still returns to the live view. */
  progressBar?: { done: number; total: number; label: string; extra?: ReactNode };
  /** App layout: the rail runs full height and starts with this (brand, back link); the header moves above the centre and right columns. */
  railHead?: ReactNode;
  stages: WbStage[];
  activeStageId?: string | null;
  onSelectStage?: (id: string) => void;
  stagesBusy?: boolean;
  /** "实时" tab label. */
  liveLabel?: string;
  /** null while loading. */
  files: WbFile[] | null;
  filesError?: string | null;
  filesNote?: ReactNode;
  /** Empty-listing sentence; '' renders nothing. */
  filesEmpty?: string;
  onRefreshFiles?: () => void;
  loadFile: (file: WbFile, signal: AbortSignal) => Promise<WbContent>;
  /** Live view (the page's own controls). */
  children: ReactNode;
}

const TEXT_LIMIT = 2 * 1024 * 1024;
type Loaded = { sig: string } & ({ state: 'loading' } | { state: 'error'; message: string } | { state: 'ready'; text: string | null; url: string | null; blob: Blob });
const sigOf = (f: WbFile) => `${f.bytes ?? '?'}:${f.sha256 ?? ''}`;

const MARK_LABEL: Record<StageMark, string> = { done: '已完成', current: '当前', running: '进行中', attention: '需要注意', failed: '失败', open: '可进入', locked: '未解锁' };

export function Workbench(props: WorkbenchProps) {
  const { stages, files, loadFile } = props;
  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null); // null = 实时
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const [openItems, setOpenItems] = useState<Set<string>>(() => new Set());
  const [openStages, setOpenStages] = useState<Set<string>>(() => new Set(props.activeStageId ? [props.activeStageId] : []));
  const [loaded, setLoaded] = useState<Record<string, Loaded>>({});
  const loadedRef = useRef(loaded); loadedRef.current = loaded;
  const inflight = useRef(new Map<string, AbortController>());
  const viewer = useRef<HTMLDivElement>(null);

  const byPath = useMemo(() => new Map((files ?? []).map(f => [f.path, f])), [files]);
  const shown = useMemo(() => filterFiles(files ?? [], query), [files, query]);
  const tree = useMemo(() => buildTree(shown), [shown]);
  const allTree = useMemo(() => buildTree(files ?? []), [files]);
  const searching = query.trim() !== '';

  // The active stage always opens when it changes (others keep the user's choice).
  useEffect(() => {
    const id = props.activeStageId;
    if (id) setOpenStages(prev => prev.has(id) ? prev : new Set(prev).add(id));
  }, [props.activeStageId]);

  // A file that disappeared from the listing (refresh, task switch) closes its tab.
  useEffect(() => {
    if (!files) return;
    setTabs(prev => {
      const next = prev.filter(p => byPath.has(p));
      if (next.length !== prev.length) setActive(a => a !== null && !byPath.has(a) ? null : a);
      return next.length === prev.length ? prev : next;
    });
  }, [files, byPath]);

  // Drop cached content of removed files; re-read open files whose size/hash changed.
  useEffect(() => {
    for (const [path, entry] of Object.entries(loadedRef.current)) {
      const file = byPath.get(path);
      if (!file) {
        inflight.current.get(path)?.abort();
        setLoaded(prev => { if (prev[path] !== entry) return prev; release(entry); const copy = { ...prev }; delete copy[path]; return copy; });
      } else if (entry.sig !== sigOf(file) && entry.state !== 'loading') ensureLoaded(file, true);
    }
  }, [byPath]);
  // Release blob URLs on unmount.
  useEffect(() => () => {
    for (const c of inflight.current.values()) c.abort();
    for (const item of Object.values(loadedRef.current)) release(item);
  }, []);

  function ensureLoaded(file: WbFile, force = false) {
    const current = loadedRef.current[file.path];
    if (!force && current && current.state !== 'error') return;
    if (current) release(current);
    inflight.current.get(file.path)?.abort();
    const ctl = new AbortController();
    inflight.current.set(file.path, ctl);
    const sig = sigOf(file);
    setLoaded(prev => ({ ...prev, [file.path]: { sig, state: 'loading' } }));
    loadFile(file, ctl.signal).then(async content => {
      if (ctl.signal.aborted) return;
      let entry: Loaded;
      if ('text' in content) {
        entry = { sig, state: 'ready', text: content.text, url: null, blob: new Blob([content.text], { type: 'text/plain;charset=utf-8' }) };
      } else {
        const kind = previewKind(file);
        const typed = kind === 'image' && file.path.toLowerCase().endsWith('.svg') ? new Blob([content.blob], { type: 'image/svg+xml' })
          : kind === 'pdf' ? new Blob([content.blob], { type: 'application/pdf' }) : content.blob;
        const wantsText = kind !== 'image' && kind !== 'pdf' && kind !== 'binary';
        const text = wantsText && typed.size <= TEXT_LIMIT ? await typed.text() : null;
        if (ctl.signal.aborted) return;
        entry = { sig, state: 'ready', text, url: kind === 'image' || kind === 'pdf' ? URL.createObjectURL(typed) : null, blob: typed };
      }
      setLoaded(prev => ({ ...prev, [file.path]: entry }));
    }).catch(error => {
      if (ctl.signal.aborted) return;
      setLoaded(prev => ({ ...prev, [file.path]: { sig, state: 'error', message: error instanceof Error ? error.message : '读取失败' } }));
    }).finally(() => { if (inflight.current.get(file.path) === ctl) inflight.current.delete(file.path); });
  }

  function open(path: string) {
    const file = byPath.get(path);
    if (!file) return;
    setTabs(prev => prev.includes(path) ? prev : [...prev, path]);
    setActive(path);
    ensureLoaded(file);
    // Reveal it in the tree.
    setCollapsed(prev => { const anc = ancestors(path); if (!anc.some(a => prev.has(a))) return prev; const next = new Set(prev); anc.forEach(a => next.delete(a)); return next; });
  }
  function close(path: string) {
    const idx = tabs.indexOf(path);
    const next = tabs.filter(p => p !== path);
    setTabs(next);
    if (active === path) setActive(next[idx] ?? next[idx - 1] ?? null);
    inflight.current.get(path)?.abort();
    setLoaded(prev => { if (!(path in prev)) return prev; release(prev[path]); const copy = { ...prev }; delete copy[path]; return copy; });
  }
  function download() {
    if (active === null) return;
    const entry = loaded[active];
    if (!entry || entry.state !== 'ready') return;
    const url = URL.createObjectURL(entry.blob);
    const a = document.createElement('a'); a.href = url; a.download = baseName(active); a.rel = 'noopener';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function toggleStage(id: string) {
    setOpenStages(prev => { const next = new Set(prev); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  }

  function toggleItem(key: string) {
    setOpenItems(prev => { const next = new Set(prev); if (next.has(key)) next.delete(key); else next.add(key); return next; });
  }
  function renderItem(key: string, item: WbStageItem) {
    const target = item.openPath && byPath.has(item.openPath) ? item.openPath : null;
    const kids = item.children ?? [];
    const childOpen = openItems.has(key);
    const inner = <>
      <span className={`wb-sub-dot${item.tone ? ` tone-${item.tone}` : ''}`} aria-hidden="true" />
      <span className="wb-sub-text"><span className="wb-sub-label">{item.label}</span>{(item.detail || item.time) && <small>{item.detail}{item.detail && item.time ? ' · ' : ''}{item.time && <time>{item.time}</time>}</small>}</span>
      {target && <Icon name="chevron-right" size={12} className="wb-sub-go" />}
    </>;
    const row = target
      ? <button type="button" className={`wb-sub${active === target ? ' is-on' : ''}`} onClick={() => open(target)} title={`打开 ${baseName(target)}`}>{inner}</button>
      : kids.length > 0
        ? <button type="button" className="wb-sub" onClick={() => toggleItem(key)}>{inner}</button>
        : <div className="wb-sub">{inner}</div>;
    if (kids.length === 0) return <li key={item.id}>{row}</li>;
    return <li key={item.id} className="wb-item-parent">
      <div className="wb-item-row">
        {row}
        <button type="button" className={`wb-stage-toggle wb-item-toggle${childOpen ? ' is-open' : ''}`} aria-expanded={childOpen}
          aria-label={`${childOpen ? '收起' : '展开'}「${item.label}」的 ${kids.length} 个子步骤`} onClick={() => toggleItem(key)}><Icon name="chevron-down" size={13} /></button>
      </div>
      {childOpen && <ul className="wb-substeps wb-children">{kids.map(kid => renderItem(`${key}/${kid.id}`, kid))}</ul>}
    </li>;
  }

  const activeFile = active !== null ? byPath.get(active) ?? null : null;
  const activeEntry = active !== null ? loaded[active] : undefined;
  const fileCount = files?.length ?? 0;
  const folderCount = countFolders(allTree);
  const bar = props.progressBar;
  const barDone = bar ? Math.max(0, Math.min(bar.done, bar.total)) : 0;

  return <div className={`wb${props.railHead ? ' is-app' : ''}`}>
    <header className="wb-head">
      <div className="wb-head-main">
        {props.eyebrow && <p className="wb-eyebrow">{props.eyebrow}</p>}
        <h1 className="wb-title">{props.title}</h1>
        {(props.status || props.meta) && <div className="wb-meta">
          {props.status && <span className={`wb-status tone-${props.status.tone}`} title={props.status.title}><span className="wb-status-dot" aria-hidden="true" />{props.status.text}</span>}
          {props.meta}
        </div>}
      </div>
      {props.actions && <div className="wb-head-actions">{props.actions}</div>}
    </header>

    <div className="wb-grid">
      <nav className="wb-rail" aria-label="阶段时间线">
        {props.railHead && <div className="wb-rail-head drag">{props.railHead}</div>}
        {!props.progressBar && props.progress && <p className="wb-progress">{props.progress}</p>}
        <ol className="wb-stages">
          {stages.map((stage, i) => {
            const isActive = stage.id === props.activeStageId;
            const expanded = openStages.has(stage.id) && (stage.items?.length ?? 0) > 0;
            const hasItems = (stage.items?.length ?? 0) > 0;
            const newGroup = stage.group && stage.group !== stages[i - 1]?.group ? stage.group : null;
            return <Fragment key={stage.id}>
              {newGroup && <li className="wb-group" role="presentation">{newGroup}</li>}
              <li className={`wb-stage is-${stage.mark}${isActive ? ' is-active' : ''}`}>
              <div className="wb-stage-row">
                <button type="button" className="wb-stage-head" aria-current={isActive ? 'step' : undefined}
                  disabled={stage.disabled || props.stagesBusy || (!props.onSelectStage && !hasItems)}
                  onClick={() => { if (props.onSelectStage) props.onSelectStage(stage.id); if (hasItems && (!props.onSelectStage || !expanded)) toggleStage(stage.id); }}>
                  <StageMarker mark={stage.mark} index={i} />
                  <span className="wb-stage-text">
                    <span className="wb-stage-title"><span className="wb-stage-num" aria-hidden="true">{String(i + 1).padStart(2, '0')}</span><b>{stage.title}</b></span>
                    {stage.summary && <small title={stage.summary}>{stage.summary}</small>}
                  </span>
                </button>
                {hasItems && <button type="button" className={`wb-stage-toggle${expanded ? ' is-open' : ''}`} aria-expanded={expanded} aria-label={`${expanded ? '收起' : '展开'}「${stage.title}」的子步骤`} onClick={() => toggleStage(stage.id)}><Icon name="chevron-down" size={13} /></button>}
              </div>
              {expanded && <ul className="wb-substeps">
                {stage.items!.map(item => renderItem(`${stage.id}/${item.id}`, item))}
              </ul>}
              </li>
            </Fragment>;
          })}
        </ol>
      </nav>

      <section className="wb-viewer" ref={viewer} aria-label="查看器">
        <div className="wb-tabbar">
          <div className="wb-tabs" role="tablist" aria-label="已打开">
            {bar
              ? <button type="button" role="tab" aria-selected={active === null} aria-label={`实时 · ${bar.label}`} className={`wb-tab wb-tab-live wb-tab-bar${active === null ? ' is-on' : ''}`} onClick={() => setActive(null)}>
                <span className="wb-bar-label">{bar.label}</span>
                <span className="wb-steps" role="progressbar" aria-valuenow={barDone} aria-valuemin={0} aria-valuemax={bar.total} aria-label={`进度 ${barDone} / ${bar.total}`}>
                  {Array.from({ length: bar.total }, (_, i) => <span key={i} className={`wb-step ${i < barDone ? 'is-done' : i === barDone ? 'is-current' : 'is-todo'}`} />)}
                </span>
                <span className="wb-bar-count">{barDone} / {bar.total}</span>
                {bar.extra}
              </button>
              : <button type="button" role="tab" aria-selected={active === null} className={`wb-tab wb-tab-live${active === null ? ' is-on' : ''}`} onClick={() => setActive(null)}>
                <span className="wb-live-dot" aria-hidden="true" />{props.liveLabel ?? '实时'}
              </button>}
            {tabs.map(path => <span key={path} className={`wb-tab${active === path ? ' is-on' : ''}`}>
              <button type="button" role="tab" aria-selected={active === path} className="wb-tab-open" title={path} onClick={() => setActive(path)}>
                <FileGlyph file={byPath.get(path)} size={13} /><span>{baseName(path)}</span>
              </button>
              <button type="button" className="wb-tab-close" aria-label={`关闭 ${baseName(path)}`} onClick={() => close(path)}><Icon name="x" size={12} /></button>
            </span>)}
          </div>
          <div className="wb-tools">
            {activeFile && <button type="button" className="btn btn-icon" aria-label="重新读取" title="重新读取" onClick={() => ensureLoaded(activeFile, true)}><Icon name="refresh" size={15} /></button>}
            <button type="button" className="btn btn-icon" aria-label="下载当前文件" title={activeFile ? `下载 ${baseName(activeFile.path)}` : '打开文件后可下载'} disabled={!activeEntry || activeEntry.state !== 'ready'} onClick={download}><Icon name="download" size={15} /></button>
          </div>
        </div>
        <div className="wb-pane" hidden={active !== null}>{props.children}</div>
        {activeFile && <div className="wb-pane wb-pane-file">
          <div className="wb-file-meta">
            <span className="ui-mono" title={activeFile.path}>{activeFile.path}</span>
            <span>{formatBytes(activeFile.bytes)}</span>
            {activeFile.sha256 && <span className="wb-file-sha"><span className="ui-mono" title={activeFile.sha256}>sha256 {activeFile.sha256.slice(0, 12)}…</span><CopyButton value={activeFile.sha256} label="复制完整 SHA256" /></span>}
            {activeFile.badge && <span className="wb-badge">{activeFile.badge}</span>}
            {activeFile.tags?.map(tag => <FileTag key={tag.text} tag={tag} />)}
          </div>
          <Preview file={activeFile} entry={activeEntry} onRetry={() => ensureLoaded(activeFile, true)} />
        </div>}
      </section>

      <aside className="wb-files" aria-label="文件">
        <div className="wb-files-head">
          <b>文件</b>
          <span className="wb-files-count">{files === null ? '读取中…' : `${fileCount} 个文件 · ${folderCount} 个文件夹`}</span>
          {props.onRefreshFiles && <button type="button" className="btn btn-icon" aria-label="刷新文件列表" title="刷新文件列表" onClick={props.onRefreshFiles}><Icon name="refresh" size={14} /></button>}
        </div>
        <label className="wb-search">
          <Icon name="search" size={14} />
          <input type="search" value={query} placeholder="输入文件名进行搜索" aria-label="搜索文件" onChange={e => setQuery(e.target.value)} />
        </label>
        {props.filesError && <p className="wb-files-msg tone-bad" role="alert">{props.filesError}</p>}
        <div className="wb-tree" role="tree" aria-label="文件树">
          {files === null && !props.filesError && <div className="wb-tree-skel" aria-hidden="true"><span /><span /><span /><span /></div>}
          {files !== null && fileCount === 0 && props.filesEmpty !== '' && <p className="wb-files-msg">{props.filesEmpty ?? '还没有文件。'}</p>}
          {files !== null && fileCount > 0 && shown.length === 0 && <p className="wb-files-msg">没有匹配「{query.trim()}」的文件。</p>}
          <TreeLevel node={tree} depth={0} collapsed={searching ? EMPTY : collapsed} active={active}
            onToggle={path => setCollapsed(prev => { const next = new Set(prev); if (next.has(path)) next.delete(path); else next.add(path); return next; })}
            onOpen={open} />
        </div>
        {props.filesNote && <div className="wb-files-note">{props.filesNote}</div>}
      </aside>
    </div>
  </div>;
}

const EMPTY = new Set<string>();

function release(entry: Loaded | undefined) {
  if (entry && entry.state === 'ready' && entry.url) URL.revokeObjectURL(entry.url);
}

function StageMarker({ mark, index }: { mark: StageMark; index: number }) {
  const icon: IconName | null = mark === 'done' ? 'check' : mark === 'attention' ? 'alert' : mark === 'failed' ? 'x' : null;
  return <span className={`wb-mark is-${mark}`}>
    {icon ? <Icon name={icon} size={12} /> : mark === 'running' || mark === 'current' ? <span className="wb-mark-core" /> : <span className="wb-mark-num">{index + 1}</span>}
    <span className="sr-only">{MARK_LABEL[mark]}</span>
  </span>;
}

function FileTag({ tag }: { tag: WbTag }) {
  return <span className={`wb-badge${tag.tone ? ` tone-${tag.tone}` : ''}`} title={tag.title}>{tag.text}</span>;
}

function FileGlyph({ file, size = 15 }: { file: WbFile | undefined; size?: number }) {
  if (!file) return <Icon name="doc" size={size} />;
  const tone = fileTone(file);
  const name: IconName = tone === 'image' ? 'image' : tone === 'code' ? 'code' : tone === 'data' ? (previewKind(file) === 'csv' ? 'table' : 'chart') : 'doc';
  return <span className={`wb-glyph is-${tone}`}><Icon name={name} size={size} /></span>;
}

function TreeLevel({ node, depth, collapsed, active, onToggle, onOpen }: { node: WbFolderNode; depth: number; collapsed: Set<string>; active: string | null; onToggle: (path: string) => void; onOpen: (path: string) => void }) {
  return <>{node.children.map((child: WbNode) => child.kind === 'folder'
    ? <div key={`d:${child.path}`} role="treeitem" aria-expanded={!collapsed.has(child.path)} aria-selected={false}>
      <button type="button" className="wb-node wb-node-dir" style={{ paddingLeft: 8 + depth * 14 }} onClick={() => onToggle(child.path)}>
        <Icon name="chevron-down" size={12} className={`wb-node-chev${collapsed.has(child.path) ? ' is-closed' : ''}`} />
        <Icon name="folder" size={15} className="wb-node-folder" />
        <span className="wb-node-name">{child.name}</span>
        <span className="wb-node-size">{child.fileCount}</span>
      </button>
      {!collapsed.has(child.path) && <div role="group"><TreeLevel node={child} depth={depth + 1} collapsed={collapsed} active={active} onToggle={onToggle} onOpen={onOpen} /></div>}
    </div>
    : <div key={`f:${child.path}`} role="treeitem" aria-selected={active === child.path}>
      <button type="button" className={`wb-node wb-node-file${active === child.path ? ' is-on' : ''}`} style={{ paddingLeft: 8 + depth * 14 + 16 }} title={child.path} onClick={() => onOpen(child.path)}>
        <FileGlyph file={child.file} />
        <span className="wb-node-name">{child.name}</span>
        {child.file.badge && <span className="wb-badge">{child.file.badge}</span>}
        {child.file.tags?.filter(tag => tag.inTree).map(tag => <FileTag key={tag.text} tag={tag} />)}
        <span className="wb-node-size">{formatBytes(child.file.bytes)}</span>
      </button>
    </div>)}</>;
}

function Preview({ file, entry, onRetry }: { file: WbFile; entry: Loaded | undefined; onRetry: () => void }) {
  if (!entry || entry.state === 'loading') return <div className="wb-preview wb-preview-center" role="status"><Icon name="refresh" spin size={16} /><span>正在读取 {baseName(file.path)}…</span></div>;
  if (entry.state === 'error') return <div className="wb-preview wb-preview-center" role="alert">
    <Icon name="alert" size={18} /><span>读取失败：{entry.message}</span>
    <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>重试</button>
  </div>;
  const kind = previewKind(file);
  if (kind === 'image' && entry.url) return <div className="wb-preview wb-preview-image"><img src={entry.url} alt={baseName(file.path)} /></div>;
  if (kind === 'pdf' && entry.url) return <div className="wb-preview wb-preview-pdf"><iframe src={entry.url} title={baseName(file.path)} /></div>;
  if (entry.text === null) return <div className="wb-preview wb-preview-center">
    <Icon name="doc" size={18} />
    <span>{kind === 'binary' ? '此类型无法在页面中预览。' : `文件较大（${formatBytes(entry.blob.size)}），请下载后查看。`}</span>
    <span className="wb-preview-hint">点击右上角下载按钮保存到本地。</span>
  </div>;
  if (kind === 'csv') return <CsvView text={entry.text} delimiter={file.path.toLowerCase().endsWith('.tsv') ? '\t' : ','} />;
  if (kind === 'json') return <pre className="wb-preview wb-code">{prettyJson(entry.text)}</pre>;
  return <pre className={`wb-preview wb-code${kind === 'markdown' ? ' is-prose' : ''}`}>{entry.text}</pre>;
}

function CsvView({ text, delimiter }: { text: string; delimiter: string }) {
  const { rows, truncated } = useMemo(() => parseCsv(text, 200, delimiter), [text, delimiter]);
  if (rows.length === 0) return <pre className="wb-preview wb-code">（空文件）</pre>;
  const [head, ...body] = rows;
  return <div className="wb-preview wb-table-wrap">
    <table className="wb-table">
      <thead><tr>{head.map((h, i) => <th key={i}>{h}</th>)}</tr></thead>
      <tbody>{body.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
    </table>
    {truncated && <p className="wb-preview-hint">仅显示前 200 行，完整内容请下载。</p>}
  </div>;
}

function prettyJson(text: string): string {
  try { return JSON.stringify(JSON.parse(text), null, 2); } catch { return text; }
}
