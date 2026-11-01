'use strict';
// Main-window preload. Sandboxed preloads cannot reach `app`, so the static
// fields come from one synchronous IPC round trip; main answers only for its
// own top frame on the backend origin.

const { contextBridge, ipcRenderer } = require('electron');

const info = ipcRenderer.sendSync('p2r:bootstrap');

if (info) {
  contextBridge.exposeInMainWorld('p2rDesktop', {
    platform: info.platform,
    appVersion: info.appVersion,
    dataDir: info.dataDir,
    logDir: info.logDir,
    backendUrl: info.backendUrl,
    openPath: (kind) => ipcRenderer.invoke('p2r:open-path', kind),
    setTheme: (theme) => ipcRenderer.send('p2r:set-theme', theme),
  });
}
