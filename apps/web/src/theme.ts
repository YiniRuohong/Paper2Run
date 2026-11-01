// Theme preference: 跟随系统 / 浅色 / 深色. The only persisted UI preference;
// it lives here (localStorage "p2r.theme") and nowhere else. public/theme-boot.js
// applies the same rules before first paint; this module keeps <html
// data-theme> in sync afterwards and tells the desktop shell (if any) about
// every change of the effective theme, including system changes.

export type ThemePref = 'system' | 'light' | 'dark';
export type Theme = 'light' | 'dark';
export const THEME_STORAGE_KEY = 'p2r.theme';
export const THEME_PREF_LABELS: Record<ThemePref, string> = { system: '跟随系统', light: '浅色', dark: '深色' };

export function parseThemePref(value: unknown): ThemePref | null {
  return value === 'system' || value === 'light' || value === 'dark' ? value : null;
}

export function resolveTheme(pref: ThemePref, systemDark: boolean): Theme {
  if (pref === 'light' || pref === 'dark') return pref;
  return systemDark ? 'dark' : 'light';
}

// "?theme=" overrides the stored preference for this page load (dev/screenshots).
export function initialThemePref(search: string, stored: string | null): ThemePref {
  let fromUrl: ThemePref | null = null;
  try { fromUrl = parseThemePref(new URLSearchParams(search).get('theme')); } catch { /* ignore */ }
  return fromUrl ?? parseThemePref(stored) ?? 'system';
}

export interface ThemeState { pref: ThemePref; theme: Theme }

let state: ThemeState = { pref: 'system', theme: 'light' };
let systemDark = false;
let started = false;
let sentToDesktop: Theme | null = null;
const listeners = new Set<() => void>();

function readStored(): string | null {
  try { return window.localStorage.getItem(THEME_STORAGE_KEY); } catch { return null; }
}

function apply(pref: ThemePref): void {
  const theme = resolveTheme(pref, systemDark);
  const changed = theme !== state.theme || pref !== state.pref;
  state = changed ? { pref, theme } : state;
  const root = document.documentElement;
  if (root.dataset.theme !== theme) root.dataset.theme = theme;
  const desktop = window.p2rDesktop;
  if (desktop && sentToDesktop !== theme) {
    sentToDesktop = theme;
    try { desktop.setTheme(theme); } catch { /* the shell is optional */ }
  }
  if (changed) for (const fn of listeners) fn();
}

export function startTheme(): void {
  if (started || typeof window === 'undefined') return;
  started = true;
  const media = typeof window.matchMedia === 'function' ? window.matchMedia('(prefers-color-scheme: dark)') : null;
  systemDark = media?.matches ?? false;
  state = { pref: 'system', theme: document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light' };
  apply(initialThemePref(window.location.search, readStored()));
  media?.addEventListener('change', event => { systemDark = event.matches; apply(state.pref); });
}

export function setThemePref(pref: ThemePref): void {
  try { window.localStorage.setItem(THEME_STORAGE_KEY, pref); } catch { /* not persisted; still applied */ }
  apply(pref);
}

export const getThemeState = (): ThemeState => state;
export function subscribeTheme(fn: () => void): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}
