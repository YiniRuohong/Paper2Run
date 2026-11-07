/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_MODE?: 'mock' | 'live' | 'replay';
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Contract provided by the Electron preload (apps/desktop). Absent in a plain
// browser; every caller must treat it as optional.
interface P2RDesktop {
  platform: 'darwin' | 'win32' | 'linux';
  appVersion: string;
  dataDir: string;
  logDir: string;
  backendUrl: string;
  openPath(kind: 'data' | 'logs'): Promise<void>;
  setTheme(theme: 'light' | 'dark'): void;
  /** Windows: full screen while the report reader is open, so no caption buttons cover it. Optional: older shells lack it. */
  setReportMode?(on: boolean): void;
}

interface Window {
  p2rDesktop?: P2RDesktop;
}
