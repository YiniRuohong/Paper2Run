'use strict';
// Splash-window preload: state updates in, retry / open logs / quit out.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('p2rSplash', {
  onState: (callback) => ipcRenderer.on('p2r:splash-state', (_event, state) => callback(state)),
  retry: () => ipcRenderer.send('p2r:splash-retry'),
  openLogs: () => ipcRenderer.send('p2r:splash-open-logs'),
  quit: () => ipcRenderer.send('p2r:splash-quit'),
});
