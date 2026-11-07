// Optional desktop-shell integration. The Electron preload exposes
// window.p2rDesktop (see vite-env.d.ts); in a plain browser it is absent and
// everything here degrades to browser behaviour.

export function getDesktop(): P2RDesktop | null {
  return typeof window !== 'undefined' && window.p2rDesktop ? window.p2rDesktop : null;
}

// Marks <html data-shell="darwin|win32|linux"> so CSS can leave room for the
// native window controls. theme-boot.js does the same before first paint.
export function applyShell(): void {
  const desktop = getDesktop();
  if (desktop) document.documentElement.dataset.shell = desktop.platform;
}

// While a full-window reader is open: on Windows the shell goes full screen so the caption
// buttons cannot cover the reader's own toolbar. <html data-report-mode> drops the space the
// toolbar otherwise keeps free for them. Returns the undo.
export function enterReportMode(): () => void {
  const desktop = getDesktop();
  if (!desktop?.setReportMode || desktop.platform !== 'win32') return () => {};
  const root = document.documentElement;
  root.dataset.reportMode = 'full';
  desktop.setReportMode(true);
  return () => { delete root.dataset.reportMode; desktop.setReportMode?.(false); };
}

// host:port of the local backend, for display only.
export function backendHost(desktopUrl: string | null | undefined, locationHost: string): string {
  if (desktopUrl) {
    try { return new URL(desktopUrl).host || locationHost; } catch { /* fall through */ }
  }
  return locationHost;
}
