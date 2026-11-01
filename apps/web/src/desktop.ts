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

// host:port of the local backend, for display only.
export function backendHost(desktopUrl: string | null | undefined, locationHost: string): string {
  if (desktopUrl) {
    try { return new URL(desktopUrl).host || locationHost; } catch { /* fall through */ }
  }
  return locationHost;
}
