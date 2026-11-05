// Pure helpers for the task workbench: file tree building, search filtering,
// preview classification and size formatting. No React, no DOM — unit tested.

export interface WbFile {
  /** Posix path, relative; folders are derived from it. Unique per workbench. */
  path: string;
  bytes: number | null;
  mediaType: string | null;
  /** Short tag shown next to the size, e.g. 记录 / 证据. */
  badge?: string;
  sha256?: string | null;
}

export interface WbFolderNode { kind: 'folder'; name: string; path: string; children: WbNode[]; fileCount: number }
export interface WbFileNode { kind: 'file'; name: string; path: string; file: WbFile }
export type WbNode = WbFolderNode | WbFileNode;

export type PreviewKind = 'image' | 'pdf' | 'markdown' | 'csv' | 'json' | 'text' | 'binary';

const IMAGE_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'svg', 'avif']);
const TEXT_EXT = new Set([
  'txt', 'log', 'py', 'ts', 'tsx', 'js', 'mjs', 'cjs', 'sh', 'bash', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
  'r', 'jl', 'c', 'h', 'cpp', 'hpp', 'rs', 'go', 'java', 'tex', 'bib', 'rst', 'lock', 'env', 'gitignore', 'dockerfile',
  'out', 'err', 'diff', 'patch', 'xml', 'html', 'css', 'sql', 'ipynb',
]);

export function extOf(path: string): string {
  const name = baseName(path).toLowerCase();
  const dot = name.lastIndexOf('.');
  return dot > 0 ? name.slice(dot + 1) : name;
}

export function baseName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

export function previewKind(file: Pick<WbFile, 'path' | 'mediaType'>): PreviewKind {
  const ext = extOf(file.path);
  const type = (file.mediaType ?? '').split(';', 1)[0].trim().toLowerCase();
  if (IMAGE_EXT.has(ext) || (type.startsWith('image/') && type !== 'image/svg+xml')) return 'image';
  if (ext === 'pdf' || type === 'application/pdf') return 'pdf';
  if (ext === 'md' || ext === 'markdown' || type === 'text/markdown') return 'markdown';
  if (ext === 'csv' || ext === 'tsv' || type === 'text/csv') return 'csv';
  if (ext === 'json' || ext === 'jsonl' || type === 'application/json') return 'json';
  if (TEXT_EXT.has(ext) || type.startsWith('text/') || type === 'application/x-yaml' || type === 'application/toml') return 'text';
  return 'binary';
}

/** Icon family for a file row. */
export function fileTone(file: Pick<WbFile, 'path' | 'mediaType'>): 'pdf' | 'image' | 'code' | 'data' | 'doc' {
  const kind = previewKind(file);
  if (kind === 'pdf') return 'pdf';
  if (kind === 'image') return 'image';
  if (kind === 'json' || kind === 'csv') return 'data';
  if (kind === 'text' && !['txt', 'log', 'out', 'err'].includes(extOf(file.path))) return 'code';
  return 'doc';
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024; let i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return `${value >= 100 ? value.toFixed(0) : value.toFixed(1)} ${units[i]}`;
}

/** Build a folder tree. Folders sort before files, both alphabetically (natural order). */
export function buildTree(files: readonly WbFile[]): WbFolderNode {
  const root: WbFolderNode = { kind: 'folder', name: '', path: '', children: [], fileCount: 0 };
  const folders = new Map<string, WbFolderNode>([['', root]]);
  const folderFor = (path: string): WbFolderNode => {
    const found = folders.get(path);
    if (found) return found;
    const cut = path.lastIndexOf('/');
    const parent = folderFor(cut < 0 ? '' : path.slice(0, cut));
    const node: WbFolderNode = { kind: 'folder', name: cut < 0 ? path : path.slice(cut + 1), path, children: [], fileCount: 0 };
    parent.children.push(node);
    folders.set(path, node);
    return node;
  };
  for (const file of files) {
    const clean = file.path.split('/').filter(Boolean).join('/');
    if (!clean) continue;
    const cut = clean.lastIndexOf('/');
    const parent = folderFor(cut < 0 ? '' : clean.slice(0, cut));
    parent.children.push({ kind: 'file', name: cut < 0 ? clean : clean.slice(cut + 1), path: file.path, file });
  }
  const collator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' });
  const finish = (node: WbFolderNode): number => {
    node.children.sort((a, b) => a.kind !== b.kind ? (a.kind === 'folder' ? -1 : 1) : collator.compare(a.name, b.name));
    node.fileCount = node.children.reduce((n, child) => n + (child.kind === 'file' ? 1 : finish(child)), 0);
    return node.fileCount;
  };
  finish(root);
  return root;
}

export function countFolders(node: WbFolderNode): number {
  return node.children.reduce((n, child) => n + (child.kind === 'folder' ? 1 + countFolders(child) : 0), 0);
}

/** Case-insensitive match on the full path; every whitespace-separated term must match. */
export function filterFiles(files: readonly WbFile[], query: string): WbFile[] {
  const terms = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (terms.length === 0) return [...files];
  return files.filter(f => { const p = f.path.toLowerCase(); return terms.every(t => p.includes(t)); });
}

/** Ancestor folder paths of a file path, nearest last. */
export function ancestors(path: string): string[] {
  const parts = path.split('/').filter(Boolean);
  const out: string[] = [];
  for (let i = 1; i < parts.length; i++) out.push(parts.slice(0, i).join('/'));
  return out;
}

/** Minimal CSV parser (RFC 4180 quotes), bounded to `maxRows` rows. */
export function parseCsv(text: string, maxRows = 200, delimiter = ','): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let row: string[] = []; let cell = ''; let quoted = false; let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { cell += '"'; i += 2; continue; }
      if (ch === '"') { quoted = false; i++; continue; }
      cell += ch; i++; continue;
    }
    if (ch === '"' && cell === '') { quoted = true; i++; continue; }
    if (ch === delimiter) { row.push(cell); cell = ''; i++; continue; }
    if (ch === '\n' || ch === '\r') {
      if (ch === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = ''; i++;
      if (rows.length >= maxRows) return { rows, truncated: i < text.length };
      continue;
    }
    cell += ch; i++;
  }
  if (cell !== '' || row.length > 0) { row.push(cell); rows.push(row); }
  return { rows, truncated: false };
}
