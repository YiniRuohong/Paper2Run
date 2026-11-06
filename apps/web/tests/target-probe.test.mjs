import test from 'node:test';
import assert from 'node:assert/strict';
import { parseProbe, probeRows, probeMissingNotice, probeMeta, SSH_EXPERIMENTAL } from '../src/features/autonomous/probe.ts';

// C6 probe display: each capability shows only what the backend observed.
// Unknown or unobserved values say 未验证 with a reason, never a green state.

const ready = () => ({
  platform: 'win32', target_kind: 'local',
  isolation: { backend: 'docker', verified: true, detail: 'Docker 使用 Linux 容器，实验镜像已就绪；容器隔离在任务启动时逐项验证' },
  docker: { available: true, os_type: 'linux', server_version: '27.3.1' },
  image: { name: 'paper2run-experiment:1', present: true },
  network_policy: { public_internet: true, host_and_metadata_blocked: null, verified: false, detail: '隔离规则在任务启动时由容器内探针验证' },
  gpu: [], missing: [], probed_at: '2026-11-06T08:00:00+00:00',
});
const byKey = rows => Object.fromEntries(rows.map(row => [row.key, row]));

test('a ready target: isolation, Docker and image are ok; network is never verified here', () => {
  const rows = byKey(probeRows(parseProbe(ready())));
  assert.deepEqual([rows.isolation.status, rows.isolation.tone], ['已验证', 'ok']);
  assert.deepEqual([rows.docker.status, rows.docker.tone], ['Linux 容器 · 27.3.1', 'ok']);
  assert.deepEqual([rows.image.status, rows.image.tone], ['已就绪', 'ok']);
  assert.deepEqual([rows.network.status, rows.network.tone], ['未验证', 'idle']);
  assert.match(rows.network.reason, /配置允许公网访问/);
  assert.match(rows.network.reason, /隔离规则在任务启动时由容器内探针验证/);
  assert.deepEqual([rows.gpu.status, rows.gpu.tone], ['未观测到', 'idle']);
  assert.equal(probeMissingNotice(parseProbe(ready())), '');
  assert.match(probeMeta(parseProbe(ready())), /^本机平台 Windows · 探测于 /);
});

test('Docker missing: bad, nothing downstream is claimed, missing items sit beside the start button', () => {
  const probe = { ...ready(), isolation: { backend: 'docker', verified: false, detail: '未检测到可用的 Docker（transport_unavailable）' },
    docker: { available: false, os_type: null, server_version: null }, image: { name: 'paper2run-experiment:1', present: false },
    missing: ['未检测到可用的 Docker：请安装并启动 Docker Desktop（Windows 需启用 WSL2 后端）'] };
  const rows = byKey(probeRows(parseProbe(probe)));
  assert.deepEqual([rows.isolation.status, rows.isolation.tone], ['未验证', 'bad']);
  assert.equal(rows.isolation.reason, probe.isolation.detail);
  assert.deepEqual([rows.docker.status, rows.docker.tone], ['未检测到', 'bad']);
  assert.deepEqual([rows.image.status, rows.image.tone], ['未验证', 'idle']);
  assert.match(rows.image.reason, /Docker 不可用/);
  assert.deepEqual([rows.gpu.status, rows.gpu.tone], ['未验证', 'idle']);
  assert.equal(probeMissingNotice(parseProbe(probe)), `启动后会在准备阶段失败：${probe.missing[0]}`);
});

test('Windows-container mode and missing image are warnings; unknown OSType stays unknown', () => {
  const windows = byKey(probeRows(parseProbe({ ...ready(), isolation: { backend: 'docker', verified: false, detail: 'x' },
    docker: { available: true, os_type: 'windows', server_version: '27.0' }, image: { name: 'paper2run-experiment:1', present: false },
    missing: ['a', 'b'] })));
  assert.deepEqual([windows.isolation.tone, windows.docker.status, windows.docker.tone], ['warn', 'Windows 容器模式', 'warn']);
  assert.deepEqual([windows.image.status, windows.image.tone], ['缺失', 'warn']);
  const unknown = byKey(probeRows(parseProbe({ ...ready(), isolation: { backend: 'docker', verified: false, detail: 'x' },
    docker: { available: true, os_type: null, server_version: null } })));
  assert.deepEqual([unknown.docker.status, unknown.docker.tone], ['未验证', 'idle']);
  assert.match(unknown.docker.reason, /OSType 未知/);
  assert.equal(probeMissingNotice(parseProbe({ ...ready(), missing: ['a', 'b'] })), '启动后会在准备阶段失败：a；b');
});

test('observed GPUs are host visibility only', () => {
  const rows = byKey(probeRows(parseProbe({ ...ready(), gpu: [{ index: '0', name: 'NVIDIA L4' }, { index: '1', name: 'NVIDIA L4' }] })));
  assert.equal(rows.gpu.status, '宿主可见 2 块');
  assert.match(rows.gpu.reason, /NVIDIA L4/);
  assert.match(rows.gpu.reason, /容器内可用性未验证/);
});

test('no green for anything the probe could not observe, and copy has no em-dash or emoji', () => {
  for (const probe of [ready(), { ...ready(), docker: { available: false, os_type: null, server_version: null }, image: { name: 'i', present: false }, isolation: { backend: 'docker', verified: false, detail: '' } }]) {
    const rows = probeRows(parseProbe(probe));
    assert.equal(rows.find(r => r.key === 'network').tone, 'idle');
    for (const row of rows) {
      assert.ok(['ok', 'run', 'accent', 'warn', 'bad', 'idle'].includes(row.tone), row.key);
      assert.ok(!/[—–]/.test(`${row.label}${row.status}${row.reason ?? ''}`), row.key);
      assert.ok((`${row.status}${row.reason ?? ''}`.match(/·/g) ?? []).length <= 1, row.key);
    }
    const isolation = rows.find(r => r.key === 'isolation');
    if (!probe.isolation.verified) assert.notEqual(isolation.tone, 'ok');
  }
  assert.ok(!/[—–]/.test(SSH_EXPERIMENTAL));
  assert.equal(SSH_EXPERIMENTAL, '实验性：远端 GPU 尚未实机验收');
});

test('malformed or partial responses are rejected rather than half-rendered', () => {
  assert.equal(parseProbe(null), null);
  assert.equal(parseProbe({}), null);
  assert.equal(parseProbe({ ...ready(), isolation: { verified: 'yes' } }), null);
  assert.equal(parseProbe({ ...ready(), missing: [1] }), null);
  assert.equal(parseProbe({ ...ready(), gpu: [{ index: 0 }] }), null);
  // A backend claiming network verification is not trusted: this probe never verifies it.
  const claimed = parseProbe({ ...ready(), network_policy: { ...ready().network_policy, verified: true } });
  assert.equal(byKey(probeRows(claimed)).network.status, '未验证');
  // Unknown platform names are shown as given.
  assert.match(probeMeta(parseProbe({ ...ready(), platform: 'freebsd14' })), /本机平台 freebsd14/);
  assert.match(probeMeta(parseProbe({ ...ready(), probed_at: 'not a date' })), /探测时间未知/);
});
