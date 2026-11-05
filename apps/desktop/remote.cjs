'use strict';
// Tablet-remote sidecar (p2r-tunnel serve), mirroring scripts/launch.py.
// Dev builds only for now: the packaged app does not bundle the binary yet,
// and Electron never runs cargo. Missing binary = tablet remote off, app fine.
const fs = require('node:fs');
const path = require('node:path');
const { spawn } = require('node:child_process');

function remotePaths({ isPackaged, repo, home, isWin }) {
  if (isPackaged) return null;
  const tunnel = path.join(repo, 'apps', 'tunnel');
  return {
    binary: path.join(tunnel, 'target', 'release', isWin ? 'p2r-tunnel.exe' : 'p2r-tunnel'),
    config: path.join(tunnel, 'deploy', 'remote.json'),
    ca: path.join(tunnel, 'deploy', 'relay-ca.pem'),
    data: path.join(home, '.paper2run', 'remote'),
  };
}

function remoteArgs({ config, ca, data }, backendPort) {
  const cfg = JSON.parse(fs.readFileSync(config, 'utf8'));
  if (!cfg.rendezvous_id) throw new Error(`${config} has no rendezvous_id`);
  return ['serve',
    '--key', path.join(data, 'desktop.key'),
    '--relay', cfg.relay_url,
    '--relay-quic-port', String(cfg.relay_quic_port),
    '--relay-ca', ca,
    '--backend-port', String(backendPort),
    '--devices', path.join(data, 'devices.json'),
    '--rendezvous', cfg.rendezvous_id,
    '--control-file', path.join(data, 'control.json')];
}

let child = null;

/** Start (or restart) the sidecar for `backendPort`; logs and returns on any problem. */
function startRemote(paths, backendPort, log) {
  stopRemote();
  if (!paths) return log('[remote] tablet remote not bundled in this build\n');
  if (!fs.existsSync(paths.binary)) return log(`[remote] tablet remote off: ${paths.binary} missing (run scripts/launch.py once to build it)\n`);
  let args;
  try { args = remoteArgs(paths, backendPort); } catch (e) { return log(`[remote] tablet remote off: ${e.message}\n`); }
  fs.mkdirSync(paths.data, { recursive: true });
  child = spawn(paths.binary, args, { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
  child.stdout.on('data', (b) => log(`[remote] ${b}`));
  child.stderr.on('data', (b) => log(`[remote] ${b}`));
  child.on('exit', (code, signal) => { log(`[remote] exited code=${code} signal=${signal}\n`); child = null; });
  child.on('error', (e) => { log(`[remote] spawn error: ${e.message}\n`); child = null; });
}

function stopRemote() {
  if (child && child.exitCode === null && child.signalCode === null) {
    try { child.kill('SIGTERM'); } catch { /* already gone */ }
  }
  child = null;
}

module.exports = { remotePaths, remoteArgs, startRemote, stopRemote };
