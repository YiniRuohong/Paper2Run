// 论文阅读器的本地持久化：书架偏好（别名 / 置顶 / 移出书架）、每篇论文的批注与笔画、
// 阅读位置与高亮配色。全部只存在本机浏览器的 localStorage 中，不经过后端；
// 书架本身来自后端的项目论文列表，这里只记录用户对它的本地调整。
// 写入失败（配额已满、隐私模式）时返回 false，调用方决定是否提示，不抛出。

import type { PaperInfo, ProjectInfo } from '../../api-client/papers.ts';

export interface KeyValueStore {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

const PREFIX = 'p2r.reader.';
const PREFS_KEY = `${PREFIX}shelf.v1`;
const POS_KEY = `${PREFIX}pos.v1`;
const COLOR_KEY = `${PREFIX}color.v1`;
const annotationsKey = (paperId: string) => `${PREFIX}notes.v1.${paperId}`;

function defaultStore(): KeyValueStore | null {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}

function readJson(store: KeyValueStore | null, key: string): unknown {
  if (!store) return null;
  try {
    const raw = store.getItem(key);
    return raw === null ? null : JSON.parse(raw);
  } catch { return null; }
}

function writeJson(store: KeyValueStore | null, key: string, value: unknown): boolean {
  if (!store) return false;
  try { store.setItem(key, JSON.stringify(value)); return true; } catch { return false; }
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const stringList = (v: unknown): string[] => Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];

// ---- 书架偏好 ----

export interface ShelfPrefs {
  /** paperId -> 用户起的显示名 */
  alias: Record<string, string>;
  pinned: string[];
  /** 移出书架的论文；论文仍在项目里，只是不在阅读器书架显示。 */
  hidden: string[];
}

export function loadShelfPrefs(store = defaultStore()): ShelfPrefs {
  const raw = readJson(store, PREFS_KEY);
  if (!isRecord(raw)) return { alias: {}, pinned: [], hidden: [] };
  const alias: Record<string, string> = {};
  if (isRecord(raw.alias)) {
    for (const [id, name] of Object.entries(raw.alias)) if (typeof name === 'string' && name.trim()) alias[id] = name;
  }
  return { alias, pinned: stringList(raw.pinned), hidden: stringList(raw.hidden) };
}

export function saveShelfPrefs(prefs: ShelfPrefs, store = defaultStore()): boolean {
  return writeJson(store, PREFS_KEY, prefs);
}

export interface ShelfDoc {
  id: string;
  name: string;
  projectId: string;
  projectName: string;
  createdAt: number;
  pinned?: boolean;
}

const paperIsPdf = (p: Pick<PaperInfo, 'source_kind' | 'media_type'>) =>
  p.source_kind ? p.source_kind === 'pdf' : p.media_type === 'application/pdf';

/** 后端论文列表 + 本地偏好 -> 书架条目。只收 PDF；移出书架的不显示。 */
export function buildShelf(papers: PaperInfo[], projects: ProjectInfo[], prefs: ShelfPrefs): ShelfDoc[] {
  const projectName = new Map(projects.map(p => [p.id, p.name]));
  const hidden = new Set(prefs.hidden);
  const pinned = new Set(prefs.pinned);
  return papers
    .filter(p => paperIsPdf(p) && !hidden.has(p.paper_id))
    .map(p => {
      const created = Date.parse(p.created_at);
      return {
        id: p.paper_id,
        name: prefs.alias[p.paper_id] ?? (p.title?.trim() || p.paper_id),
        projectId: p.project_id,
        projectName: projectName.get(p.project_id) ?? p.project_id,
        createdAt: Number.isFinite(created) ? created : 0,
        pinned: pinned.has(p.paper_id),
      };
    });
}

// ---- 批注与笔画 ----

export interface StoredAnnotations<H, S> { highlights: H[]; strokes: S[] }

export function loadAnnotations<H, S>(paperId: string, store = defaultStore()): StoredAnnotations<H, S> {
  const raw = readJson(store, annotationsKey(paperId));
  if (!isRecord(raw)) return { highlights: [], strokes: [] };
  return {
    highlights: Array.isArray(raw.highlights) ? raw.highlights.filter(isRecord) as H[] : [],
    strokes: Array.isArray(raw.strokes) ? raw.strokes.filter(isRecord) as S[] : [],
  };
}

export function saveAnnotations<H, S>(paperId: string, data: StoredAnnotations<H, S>, store = defaultStore()): boolean {
  return writeJson(store, annotationsKey(paperId), data);
}

// ---- 阅读位置 ----

export function loadPosition(paperId: string, store = defaultStore()): number {
  const raw = readJson(store, POS_KEY);
  const value = isRecord(raw) ? raw[paperId] : undefined;
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : 0;
}

export function savePosition(paperId: string, scrollTop: number, store = defaultStore()): boolean {
  const raw = readJson(store, POS_KEY);
  const map = isRecord(raw) ? { ...raw } : {};
  map[paperId] = Math.max(0, Math.round(scrollTop));
  return writeJson(store, POS_KEY, map);
}

// ---- 高亮配色 ----

export function loadColorKey<K extends string>(allowed: readonly K[], fallback: K, store = defaultStore()): K {
  const raw = readJson(store, COLOR_KEY);
  return typeof raw === 'string' && (allowed as readonly string[]).includes(raw) ? raw as K : fallback;
}

export function saveColorKey(key: string, store = defaultStore()): boolean {
  return writeJson(store, COLOR_KEY, key);
}
