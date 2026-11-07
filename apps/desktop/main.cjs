'use strict';
// Electron main process: owns the backend child process and the two windows
// (local splash while the backend starts, main window once /healthz is ok).

const { app, BrowserWindow, Menu, ipcMain, nativeTheme, screen, session, shell } = require('electron');
const { execFile, spawn } = require('node:child_process');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');

const IS_MAC = process.platform === 'darwin';
const IS_WIN = process.platform === 'win32';

// Never share data or the single-instance lock between dev and packaged runs.
if (!app.isPackaged) app.setPath('userData', `${app.getPath('userData')}-dev`);

const THEMES = {
  dark: { color: '#1C1C1F', symbolColor: '#ECECF0' },
  light: { color: '#F7F7F8', symbolColor: '#1D1D1F' },
};
const TITLE_BAR_HEIGHT = 52;
const STARTUP_TIMEOUT_MS = 120_000;
const HEALTH_POLL_MS = 200;
const STOP_TIMEOUT_MS = 7_000; // longer than serve.py's 5 s graceful shutdown
const LOG_MAX_BYTES = 5 * 1024 * 1024;
const LOG_TAIL_LINES = 40;

let dataDir;
let logDir;
let statePath;
let persisted = {};
let saveTimer = null;
let logFd = null;
let logBytes = 0;
const logTail = [];

let mainWindow = null;
let splashWindow = null;
let splashState = { state: 'starting' };
let explicitTheme = null;

let backend = null; // { child, exited: Promise }
let stopping = Promise.resolve(); // completes when the last stopped backend is gone
let backendUrl = null;
let backendGeneration = 0;
let quitState = 'running'; // running -> stopping -> done

// --- persisted window state -------------------------------------------------

function loadState() {
  try {
    const parsed = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

function saveState() {
  clearTimeout(saveTimer);
  saveTimer = null;
  try {
    const tmp = `${statePath}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(persisted, null, 2));
    fs.renameSync(tmp, statePath);
  } catch (error) {
    writeLog(`[desktop] could not save window state: ${error.message}\n`);
  }
}

function restoredBounds() {
  const defaults = { width: 1280, height: 820 };
  const saved = persisted.bounds;
  const valid = saved && ['x', 'y', 'width', 'height'].every((key) => Number.isInteger(saved[key]));
  if (!valid) return defaults;
  // Ignore bounds left on a display that is no longer attached.
  const area = screen.getDisplayMatching(saved).workArea;
  const overlapX = Math.min(saved.x + saved.width, area.x + area.width) - Math.max(saved.x, area.x);
  const overlapY = Math.min(saved.y + saved.height, area.y + area.height) - Math.max(saved.y, area.y);
  if (overlapX < 100 || overlapY < 100) return { width: saved.width, height: saved.height };
  return saved;
}

function trackBounds(win) {
  const remember = () => {
    if (win.isDestroyed()) return;
    persisted.maximized = win.isMaximized();
    if (!win.isMaximized() && !win.isFullScreen() && !win.isMinimized()) persisted.bounds = win.getBounds();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveState, 500);
  };
  win.on('resize', remember);
  win.on('move', remember);
  win.on('close', () => {
    remember();
    saveState();
  });
}

// --- backend log ------------------------------------------------------------

function openLog() {
  fs.mkdirSync(logDir, { recursive: true });
  const file = path.join(logDir, 'backend.log');
  logFd = fs.openSync(file, 'a');
  logBytes = fs.fstatSync(logFd).size;
}

function writeLog(text) {
  for (const line of text.split(/\r?\n/)) {
    if (line) logTail.push(line);
  }
  logTail.splice(0, Math.max(0, logTail.length - LOG_TAIL_LINES));
  if (logFd === null) return;
  try {
    const bytes = Buffer.from(text);
    if (logBytes + bytes.length > LOG_MAX_BYTES) {
      fs.closeSync(logFd);
      logFd = null;
      const file = path.join(logDir, 'backend.log');
      try {
        fs.rmSync(`${file}.1`, { force: true });
        fs.renameSync(file, `${file}.1`);
      } finally {
        // A failed rotation (e.g. a locked file on Windows) must not end logging.
        openLog();
      }
    }
    fs.writeSync(logFd, bytes);
    logBytes += bytes.length;
  } catch {
    // Logging must never take the app down.
  }
}

// --- backend process --------------------------------------------------------

function runtimePaths() {
  if (app.isPackaged) {
    const res = process.resourcesPath;
    return {
      python: IS_WIN ? path.join(res, 'python', 'python.exe') : path.join(res, 'python', 'bin', 'python3.12'),
      serve: path.join(res, 'backend', 'serve.py'),
      web: path.join(res, 'web'),
      node: IS_WIN ? path.join(res, 'node', 'node.exe') : path.join(res, 'node', 'bin', 'node'),
      harness: path.join(res, 'harness'),
      skills: path.join(res, 'research-skills'),
    };
  }
  const repo = path.resolve(__dirname, '..', '..');
  return {
    python: IS_WIN ? path.join(repo, '.venv', 'Scripts', 'python.exe') : path.join(repo, '.venv', 'bin', 'python'),
    serve: path.join(__dirname, 'backend', 'serve.py'),
    web: path.join(repo, 'apps', 'web', 'dist'),
    node: process.env.P2R_NODE_BINARY || path.join(__dirname, '.runtime', `${process.platform}-${process.arch}`, 'node', ...(IS_WIN ? ['node.exe'] : ['bin', 'node'])),
    harness: process.env.P2R_HARNESS_DIR || path.join(repo, 'apps', 'harness'),
    skills: process.env.P2R_SKILLS_DIR || path.join(repo, 'resources', 'research-skills'),
  };
}

function backendEnv(runtime) {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    // PYTHONHOME/PYTHONPATH must not redirect the bundled interpreter, and
    // ELECTRON_* must not leak into anything the backend launches.
    if (!/^(PYTHON|ELECTRON_|NODE_OPTIONS$|NODE_PATH$)/i.test(key)) env[key] = value;
  }
  return {
    ...env,
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    P2R_DEMO_DIR: dataDir,
    P2R_WEB_DIST: runtime.web,
    P2R_NODE_BINARY: runtime.node,
    P2R_HARNESS_DIR: runtime.harness,
    P2R_SKILLS_DIR: runtime.skills,
  };
}

// Experiments run in Linux containers on every platform (Windows: Docker
// Desktop with the WSL2 backend). Returns the log line, or null when ready.
function dockerMessage(error, stdout) {
  if (error) {
    return '[desktop] 未检测到可用的 Docker：本地自主实验暂不可用。请安装并启动 Docker Desktop（Windows 需启用 WSL2 后端），然后在新建实验页重新探测执行位置。\n';
  }
  const osType = String(stdout).trim();
  if (osType !== 'linux') {
    return `[desktop] Docker 当前不是 Linux 容器模式（OSType=${osType || '未知'}）：自主实验需要 Linux 容器。Windows 上请在 Docker Desktop 托盘菜单选择 Switch to Linux containers，然后重新探测。\n`;
  }
  return null;
}

function checkAutonomousRuntime(runtime) {
  // Same checks on macOS and Windows. Windows is supported by construction
  // (unit-tested, not verified on a real machine). Nothing here blocks startup.
  // Docker absence only affects local autonomous experiments; the
  // new-experiment page shows the target probe result.
  execFile('docker', ['info', '--format', '{{.OSType}}'], { env: backendEnv(runtime), timeout: 10_000, windowsHide: true }, (error, stdout) => {
    const message = dockerMessage(error, stdout);
    if (message) writeLog(message);
  });
  const missing = [runtime.node, path.join(runtime.harness, 'bridge.mjs'),
    path.join(runtime.harness, 'node_modules', '@earendil-works', 'pi-coding-agent', 'package.json'), runtime.skills]
    .find((file) => !fs.existsSync(file));
  if (missing) {
    writeLog(`[desktop] autonomous mode unavailable: missing ${missing}; run scripts/desktop/build_harness.py or scripts/launch.py in development\n`);
    return;
  }
  execFile(runtime.node, ['-p', 'process.versions.node'], { env: backendEnv(runtime), timeout: 10_000, windowsHide: true }, (error, stdout) => {
    const version = String(stdout).trim();
    const [major, minor] = version.split('.').map(Number);
    if (error || !Number.isInteger(major) || !Number.isInteger(minor) || major < 22 || (major === 22 && minor < 19)) {
      writeLog('[desktop] autonomous mode unavailable: Node >=22.19 required or runtime failed to start\n');
    }
  });
}

function healthOk(port) {
  return new Promise((resolve) => {
    const request = http.get({ host: '127.0.0.1', port, path: '/healthz', timeout: 2000 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on('timeout', () => request.destroy());
    request.on('error', () => resolve(false));
  });
}

function startBackend() {
  const generation = ++backendGeneration;
  const current = () => generation === backendGeneration && quitState === 'running';
  backendUrl = null;
  setSplash({ state: 'starting' });

  const runtime = runtimePaths();
  checkAutonomousRuntime(runtime);
  const missing = [runtime.python, runtime.serve].find((file) => !fs.existsSync(file));
  if (missing) {
    failBackend(`找不到后端运行时：${missing}`);
    return;
  }

  // -I ignores PYTHON* and user site; -B keeps the read-only bundle unwritten.
  const args = ['-I', '-B', '-X', 'utf8', runtime.serve];
  if (validPort(persisted.port)) args.push('--port', String(persisted.port));
  writeLog(`[desktop] starting backend: ${runtime.python} ${args.join(' ')}\n`);

  let child;
  try {
    child = spawn(runtime.python, args, {
      cwd: dataDir,
      env: backendEnv(runtime),
      // stdin stays an open pipe: serve.py exits on EOF, so the backend dies
      // with this process even after SIGKILL or a crash.
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
      // POSIX: own process group, so stopping it also reaches the harness,
      // Docker CLI and parser processes spawned by the backend.
      detached: !IS_WIN,
    });
  } catch (error) {
    failBackend(`无法启动后端：${error.message}`);
    return;
  }

  // 'exit', not 'close': grandchildren inheriting stdout/stderr can keep the
  // pipes open long after the backend itself is gone.
  const exited = new Promise((resolve) => {
    child.once('exit', resolve);
    child.once('error', (error) => {
      writeLog(`[desktop] backend spawn error: ${error.message}\n`);
      resolve();
    });
  });
  backend = { child, exited };
  child.stdin.on('error', () => {});

  const deadline = setTimeout(() => {
    if (current() && !backendUrl) failBackend(`后端在 ${STARTUP_TIMEOUT_MS / 1000} 秒内未就绪`);
  }, STARTUP_TIMEOUT_MS);

  const waitForHealth = async (port) => {
    while (current() && !backendUrl) {
      if (await healthOk(port)) {
        if (!current()) return;
        clearTimeout(deadline);
        backendUrl = `http://127.0.0.1:${port}`;
        writeLog(`[desktop] backend ready at ${backendUrl}\n`);
        onBackendReady();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, HEALTH_POLL_MS));
    }
  };

  let stdoutBuffer = '';
  let announced = false;
  child.stdout.on('data', (chunk) => {
    const text = chunk.toString('utf8');
    writeLog(text);
    if (announced) return;
    stdoutBuffer += text;
    let newline;
    while (!announced && (newline = stdoutBuffer.indexOf('\n')) >= 0) {
      const line = stdoutBuffer.slice(0, newline).trim();
      stdoutBuffer = stdoutBuffer.slice(newline + 1);
      let event = null;
      try {
        event = JSON.parse(line);
      } catch {
        continue;
      }
      if (event && event.event === 'listening' && Number.isInteger(event.port)) {
        announced = true;
        stdoutBuffer = '';
        // Keep the first port: the UI origin, and with it localStorage,
        // stays the same across restarts unless that port is taken.
        if (!validPort(persisted.port)) {
          persisted.port = event.port;
          saveState();
        }
        waitForHealth(event.port);
      }
    }
  });
  child.stderr.on('data', (chunk) => writeLog(chunk.toString('utf8')));

  exited.then(() => {
    clearTimeout(deadline);
    if (!current()) return;
    const code = child.exitCode === null ? `signal ${child.signalCode || 'unknown'}` : `exit ${child.exitCode}`;
    writeLog(`[desktop] backend exited (${code})\n`);
    failBackend(`后端进程已退出（${code}）`);
  });
}

function failBackend(message) {
  backendGeneration += 1; // stale health polls and exit handlers become no-ops
  backendUrl = null;
  writeLog(`[desktop] ${message}\n`);
  stopping = stopBackend(backend);
  splashState = { state: 'failed', message, log: logTail.join('\n'), logDir };
  showSplash();
  if (mainWindow && !mainWindow.isDestroyed()) mainWindow.destroy();
}

function validPort(port) {
  return Number.isInteger(port) && port >= 1024 && port <= 65535;
}

function signalTree(child, signal) {
  try {
    process.kill(-child.pid, signal); // whole process group (detached spawn)
  } catch {
    try { child.kill(signal); } catch { /* already gone */ }
  }
}

function stopBackend(target = backend) {
  if (!target) return stopping;
  if (target === backend) backend = null;
  const { child, exited } = target;
  if (child.exitCode !== null || child.signalCode !== null || !child.pid) return exited;
  if (IS_WIN) {
    // /T also takes down the backend's child processes. Full path: never
    // resolve taskkill through PATH or the working directory.
    const taskkill = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe');
    execFile(taskkill, ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }, (error) => {
      if (error) {
        writeLog(`[desktop] taskkill failed: ${error.message}\n`);
        try { child.kill(); } catch { /* already gone */ }
      }
    });
    return exited;
  }
  signalTree(child, 'SIGTERM');
  const force = setTimeout(() => signalTree(child, 'SIGKILL'), STOP_TIMEOUT_MS);
  return exited.finally(() => {
    clearTimeout(force);
    signalTree(child, 'SIGKILL'); // stragglers left in the group
  });
}

function onBackendReady() {
  splashState = { state: 'starting' };
  createMainWindow();
}

// --- windows ----------------------------------------------------------------

function currentTheme() {
  return explicitTheme || (nativeTheme.shouldUseDarkColors ? 'dark' : 'light');
}

function applyTheme() {
  const theme = THEMES[currentTheme()];
  if (!mainWindow || mainWindow.isDestroyed()) return;
  mainWindow.setBackgroundColor(theme.color);
  if (IS_WIN) mainWindow.setTitleBarOverlay({ ...theme, height: TITLE_BAR_HEIGHT });
}

function createMainWindow() {
  const theme = THEMES[currentTheme()];
  const chrome = {};
  if (IS_MAC) {
    chrome.titleBarStyle = 'hiddenInset';
    chrome.trafficLightPosition = { x: 18, y: 18 };
  } else if (IS_WIN) {
    chrome.titleBarStyle = 'hidden';
    chrome.titleBarOverlay = { ...theme, height: TITLE_BAR_HEIGHT };
    chrome.autoHideMenuBar = true;
  }
  const win = new BrowserWindow({
    ...restoredBounds(),
    minWidth: 1040,
    minHeight: 680,
    show: false,
    title: 'Paper2Run',
    backgroundColor: theme.color,
    ...chrome,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  mainWindow = win;
  if (persisted.maximized) win.maximize();
  trackBounds(win);
  win.once('ready-to-show', () => {
    win.show();
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
  });
  win.on('closed', () => {
    if (mainWindow === win) mainWindow = null;
  });
  win.loadURL(backendUrl).catch((error) => {
    writeLog(`[desktop] main window load failed: ${error.message}\n`);
  });
}

function setSplash(state) {
  splashState = state;
  if (splashWindow && !splashWindow.isDestroyed()) splashWindow.webContents.send('p2r:splash-state', splashState);
}

function showSplash() {
  if (splashWindow && !splashWindow.isDestroyed()) {
    setSplash(splashState);
    splashWindow.show();
    return;
  }
  const win = new BrowserWindow({
    width: 520,
    height: 400,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    frame: false,
    show: false,
    title: 'Paper2Run',
    backgroundColor: THEMES[nativeTheme.shouldUseDarkColors ? 'dark' : 'light'].color,
    webPreferences: {
      preload: path.join(__dirname, 'splash-preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  splashWindow = win;
  win.once('ready-to-show', () => win.show());
  win.webContents.on('did-finish-load', () => win.webContents.send('p2r:splash-state', splashState));
  win.on('closed', () => {
    if (splashWindow === win) splashWindow = null;
  });
  win.loadFile(path.join(__dirname, 'splash.html'));
}

function buildMenu() {
  const view = [{ role: 'reload' }, { role: 'forceReload' }];
  if (!app.isPackaged) view.push({ role: 'toggleDevTools' });
  view.push(
    { type: 'separator' },
    { role: 'resetZoom' },
    { role: 'zoomIn' },
    { role: 'zoomOut' },
    { type: 'separator' },
    { role: 'togglefullscreen' },
  );
  const template = [
    // The Edit roles are what make copy/paste/undo work in inputs on macOS.
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    { label: 'View', submenu: view },
    { role: 'windowMenu' },
  ];
  if (IS_MAC) {
    template.unshift({
      label: app.name,
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'hide' },
        { role: 'hideOthers' },
        { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    });
  }
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// --- security ---------------------------------------------------------------

function isBackendUrl(url) {
  if (!backendUrl) return false;
  try {
    return new URL(url).origin === backendUrl;
  } catch {
    return false;
  }
}

function installSecurity() {
  const ses = session.defaultSession;
  ses.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  ses.setPermissionCheckHandler(() => false);
  // No setSavePath: Electron shows the native Save dialog for downloads.
  ses.on('will-download', (_event, item) => {
    writeLog(`[desktop] download requested: ${item.getFilename()} (${item.getTotalBytes()} bytes)\n`);
  });

  app.on('web-contents-created', (_event, contents) => {
    const guard = (event, url) => {
      if (!isBackendUrl(url)) event.preventDefault();
    };
    contents.on('will-navigate', guard);
    contents.on('will-redirect', guard);
    contents.on('will-attach-webview', (event) => event.preventDefault());
    contents.setWindowOpenHandler(({ url }) => {
      try {
        const { protocol } = new URL(url);
        if (protocol === 'http:' || protocol === 'https:') shell.openExternal(url);
      } catch {
        // Malformed URL: nothing to open.
      }
      return { action: 'deny' };
    });
  });
}

function fromMainWindow(event) {
  const frame = event.senderFrame;
  return Boolean(
    mainWindow && !mainWindow.isDestroyed() && event.sender === mainWindow.webContents
      && frame && frame.parent === null && isBackendUrl(frame.url),
  );
}

function fromSplashWindow(event) {
  const frame = event.senderFrame;
  return Boolean(
    splashWindow && !splashWindow.isDestroyed() && event.sender === splashWindow.webContents
      && frame && frame.parent === null && frame.url.startsWith('file:') && frame.url.endsWith('/splash.html'),
  );
}

function installIpc() {
  ipcMain.on('p2r:bootstrap', (event) => {
    event.returnValue = fromMainWindow(event)
      ? { platform: process.platform, appVersion: app.getVersion(), dataDir, logDir, backendUrl }
      : null;
  });
  ipcMain.handle('p2r:open-path', async (event, kind) => {
    if (!fromMainWindow(event)) throw new Error('forbidden');
    if (kind !== 'data' && kind !== 'logs') throw new TypeError('kind must be "data" or "logs"');
    const failure = await shell.openPath(kind === 'data' ? dataDir : logDir);
    if (failure) throw new Error(failure);
  });
  ipcMain.on('p2r:set-theme', (event, theme) => {
    if (!fromMainWindow(event) || (theme !== 'light' && theme !== 'dark')) return;
    explicitTheme = theme;
    applyTheme();
  });
  // The report reader covers the whole window. On Windows the caption buttons would sit on
  // top of its toolbar, so the reader runs full screen (no caption buttons): close the
  // report first, then the window. Leaving restores the previous maximized state.
  let reportRestoreMaximized = false;
  ipcMain.on('p2r:report-mode', (event, on) => {
    if (!fromMainWindow(event) || !IS_WIN || !mainWindow || mainWindow.isDestroyed()) return;
    if (on === true && !mainWindow.isFullScreen()) {
      reportRestoreMaximized = mainWindow.isMaximized();
      mainWindow.setFullScreen(true);
    } else if (on === false && mainWindow.isFullScreen()) {
      mainWindow.setFullScreen(false);
      if (reportRestoreMaximized) mainWindow.maximize();
    }
  });
  ipcMain.on('p2r:splash-retry', (event) => {
    if (!fromSplashWindow(event) || quitState !== 'running') return;
    setSplash({ state: 'starting' });
    // Wait for the failed backend (and its group) to be gone before reusing its port and database.
    Promise.all([stopping, stopBackend(backend)]).finally(() => {
      if (quitState === 'running') startBackend();
    });
  });
  ipcMain.on('p2r:splash-open-logs', (event) => {
    if (fromSplashWindow(event)) shell.openPath(logDir);
  });
  ipcMain.on('p2r:splash-quit', (event) => {
    if (fromSplashWindow(event)) app.quit();
  });
}

// --- app lifecycle ----------------------------------------------------------

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const win = mainWindow || splashWindow;
    if (!win || win.isDestroyed()) return;
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });

  app.on('before-quit', (event) => {
    if (quitState === 'done') return;
    event.preventDefault();
    if (quitState === 'stopping') return;
    quitState = 'stopping';
    saveState();
    const finish = () => {
      if (quitState === 'done') return;
      quitState = 'done';
      app.exit(0);
    };
    // Quitting must never hang on a backend that refuses to die.
    setTimeout(finish, STOP_TIMEOUT_MS + 3_000);
    Promise.all([stopping, stopBackend()]).finally(finish);
  });

  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => app.quit());

  app.on('window-all-closed', () => {
    // A macOS app stays alive with no windows, but only with a live backend
    // to come back to; a failed start has nothing to reopen.
    if (!IS_MAC || !backendUrl) app.quit();
  });

  app.on('activate', () => {
    if (!mainWindow && !splashWindow && backendUrl) createMainWindow();
  });

  app.whenReady().then(() => {
    const userData = app.getPath('userData');
    dataDir = path.join(userData, 'data');
    logDir = path.join(userData, 'logs');
    statePath = path.join(userData, 'window-state.json');
    fs.mkdirSync(dataDir, { recursive: true });
    openLog();
    persisted = loadState();

    app.setAboutPanelOptions({ applicationName: 'Paper2Run', applicationVersion: app.getVersion() });
    nativeTheme.on('updated', () => {
      if (!explicitTheme) applyTheme();
    });
    buildMenu();
    installSecurity();
    installIpc();
    showSplash();
    startBackend();
  });
}
