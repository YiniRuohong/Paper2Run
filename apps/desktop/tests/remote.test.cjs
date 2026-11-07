const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { remoteArgs, remotePaths } = require('../remote.cjs');

test('remoteArgs mirrors launch.py: serve with config, backend port and data paths', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'p2r-remote-'));
  const config = path.join(dir, 'remote.json');
  fs.writeFileSync(config, JSON.stringify({ relay_url: 'https://60.204.202.153:12234', relay_quic_port: 12234, rendezvous_id: 'e'.repeat(64) }));
  const args = remoteArgs({ config, ca: '/ca.pem', data: '/data' }, 8017);
  assert.equal(args[0], 'serve');
  const kv = Object.fromEntries(args.slice(1).reduce((acc, v, i, a) => (i % 2 ? acc : [...acc, [v, a[i + 1]]]), []));
  assert.deepEqual(kv, {
    '--key': path.join('/data', 'desktop.key'),
    '--relay': 'https://60.204.202.153:12234',
    '--relay-quic-port': '12234',
    '--relay-ca': '/ca.pem',
    '--backend-port': '8017',
    '--devices': path.join('/data', 'devices.json'),
    '--rendezvous': 'e'.repeat(64),
    '--control-file': path.join('/data', 'control.json'),
  });
});

test('remoteArgs refuses a config without a rendezvous id', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'p2r-remote-'));
  const config = path.join(dir, 'remote.json');
  fs.writeFileSync(config, JSON.stringify({ relay_url: 'https://x:1', relay_quic_port: 1, rendezvous_id: '' }));
  assert.throws(() => remoteArgs({ config, ca: '/ca', data: '/d' }, 1), /rendezvous/);
});

test('remotePaths: dev build points into the repo, packaged build into resources/tunnel', () => {
  const dev = remotePaths({ isPackaged: false, repo: '/repo', resources: '/res', home: '/home/u', isWin: false });
  assert.equal(dev.binary, path.join('/repo', 'apps', 'tunnel', 'target', 'release', 'p2r-tunnel'));
  assert.equal(dev.config, path.join('/repo', 'apps', 'tunnel', 'deploy', 'remote.json'));
  assert.equal(dev.data, path.join('/home/u', '.paper2run', 'remote'));
  const packaged = remotePaths({ isPackaged: true, repo: '/r', resources: '/res', home: '/h', isWin: true });
  assert.deepEqual(packaged, {
    binary: path.join('/res', 'tunnel', 'p2r-tunnel.exe'), config: path.join('/res', 'tunnel', 'remote.json'),
    ca: path.join('/res', 'tunnel', 'relay-ca.pem'), data: path.join('/h', '.paper2run', 'remote'),
  });
});
