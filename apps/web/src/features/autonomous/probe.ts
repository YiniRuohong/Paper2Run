import type { ProjectConfig, TargetProbe } from '../../api-client/autonomous.ts';
import type { Tone } from '../../ui/Card.tsx';

// 执行位置能力（契约 C6）的纯映射。只呈现后端实际观测到的能力；
// 未观测或无法确认的一律写「未验证」并给出原因。网络边界从不由该探测验证。

export const SSH_EXPERIMENTAL = '实验性：远端 GPU 尚未实机验收';

export interface ProbeRow { key: 'isolation' | 'docker' | 'image' | 'network' | 'gpu'; label: string; status: string; tone: Tone; reason: string | null }

const PLATFORMS: Record<string, string> = { darwin: 'macOS', linux: 'Linux', win32: 'Windows' };
const isObject = (value: unknown): value is Record<string, unknown> => !!value && typeof value === 'object' && !Array.isArray(value);
const isString = (value: unknown): value is string => typeof value === 'string';
const isBool = (value: unknown): value is boolean => typeof value === 'boolean';
const nullableString = (value: unknown) => value === null || isString(value);

/** 后端响应按不可信输入校验；形状不对就整体拒绝，不渲染半份结果。 */
export function parseProbe(value: unknown): TargetProbe | null {
  if (!isObject(value)) return null;
  const { isolation, docker, image, network_policy: network, gpu, missing } = value;
  if (!isString(value.platform) || (value.target_kind !== 'local' && value.target_kind !== 'ssh') || !isString(value.probed_at)) return null;
  if (!isObject(isolation) || !isBool(isolation.verified) || !isString(isolation.detail)) return null;
  if (!isObject(docker) || !isBool(docker.available) || !nullableString(docker.os_type) || !nullableString(docker.server_version)) return null;
  if (!isObject(image) || !isString(image.name) || !isBool(image.present)) return null;
  if (!isObject(network) || !isBool(network.public_internet) || !isString(network.detail)) return null;
  if (!Array.isArray(gpu) || !gpu.every(item => isObject(item) && isString(item.index) && isString(item.name))) return null;
  if (!Array.isArray(missing) || !missing.every(isString)) return null;
  const osType = docker.os_type === 'linux' || docker.os_type === 'windows' ? docker.os_type : null;
  return {
    platform: value.platform, target_kind: value.target_kind, probed_at: value.probed_at,
    isolation: { backend: 'docker', verified: isolation.verified, detail: isolation.detail },
    docker: { available: docker.available, os_type: osType, server_version: (docker.server_version as string | null) || null },
    image: { name: image.name, present: image.present },
    // 该探测不创建容器，网络边界无从验证：无论后端返回什么都按未验证处理。
    network_policy: { public_internet: network.public_internet, host_and_metadata_blocked: null, verified: false, detail: network.detail },
    gpu: gpu.map(item => ({ index: String((item as { index: string }).index), name: String((item as { name: string }).name) })),
    missing: [...missing],
  };
}

/** requestedGpus：被探测的那份已保存配置里的 limits.gpu_devices。 */
export function probeRows(probe: TargetProbe, requestedGpus: readonly string[] = []): ProbeRow[] {
  const { docker, image, isolation, network_policy: network, gpu } = probe;
  const linux = docker.available && docker.os_type === 'linux';
  const rows: ProbeRow[] = [];
  // 「可用」只说明后端存在且就绪；容器隔离本身在任务准备阶段才验证。
  rows.push(isolation.verified
    ? { key: 'isolation', label: '隔离后端 Docker', status: '可用', tone: 'ok', reason: isolation.detail || null }
    : { key: 'isolation', label: '隔离后端 Docker', status: '未验证', tone: docker.available ? 'warn' : 'bad', reason: isolation.detail || '探测未给出原因' });
  rows.push(!docker.available
    ? { key: 'docker', label: 'Docker', status: '未检测到', tone: 'bad', reason: 'Docker 未响应探测' }
    : docker.os_type === 'linux'
      ? { key: 'docker', label: 'Docker', status: `Linux 容器${docker.server_version ? ` · ${docker.server_version}` : ''}`, tone: 'ok', reason: null }
      : docker.os_type === 'windows'
        ? { key: 'docker', label: 'Docker', status: 'Windows 容器模式', tone: 'warn', reason: '实验镜像需要 Linux 容器' }
        : { key: 'docker', label: 'Docker', status: '未验证', tone: 'idle', reason: 'OSType 未知，无法确认使用 Linux 容器' });
  rows.push(image.present
    ? { key: 'image', label: '实验镜像', status: '已就绪', tone: 'ok', reason: image.name }
    : !docker.available
      ? { key: 'image', label: '实验镜像', status: '未验证', tone: 'idle', reason: 'Docker 不可用，无法检查镜像' }
      : { key: 'image', label: '实验镜像', status: '缺失', tone: 'warn', reason: `${image.name} 不在执行目标上` });
  rows.push({ key: 'network', label: '网络边界', status: '未验证', tone: 'idle',
    reason: `${network.public_internet ? '配置允许公网访问' : '配置禁止公网访问'}，禁止访问宿主与元数据；${network.detail}` });
  // 宿主可见不等于容器内可用：不配绿。配置请求了却没观测到，与 missing 一致用 warn。
  rows.push(gpu.length
    ? { key: 'gpu', label: 'GPU', status: `宿主可见 ${gpu.length} 块`, tone: 'idle', reason: `${[...new Set(gpu.map(item => item.name))].join('、')}；容器内可用性未验证` }
    : !linux
      ? { key: 'gpu', label: 'GPU', status: '未验证', tone: 'idle', reason: '执行目标未就绪，未进行 GPU 探测' }
      : requestedGpus.length
        ? { key: 'gpu', label: 'GPU', status: '未观测到', tone: 'warn', reason: `配置请求了 GPU 设备 ${requestedGpus.join(',')}，执行目标上没有可见的 NVIDIA GPU` }
        : { key: 'gpu', label: 'GPU', status: '未观测到', tone: 'idle', reason: '执行目标上没有可见的 NVIDIA GPU' });
  return rows;
}

/** 探测读取的是已保存配置；表单中任一探测相关字段与之不同，结果就过期了。 */
export function probeStale(probed: ProjectConfig, current: ProjectConfig): boolean {
  const key = (config: ProjectConfig) => JSON.stringify([
    config.target.kind, config.project.workspace, config.target.image,
    config.target.ssh_config_alias ?? '', config.target.remote_workspace ?? '',
    config.limits.gpu_devices ?? [], config.network.public_internet,
  ]);
  return key(probed) !== key(current);
}

/** 启动按钮旁的说明。探测只是提示，准备阶段才是真正的关卡，所以不禁用启动。 */
export function probeMissingNotice(probe: TargetProbe | null): string {
  return probe && probe.missing.length ? `启动后会在准备阶段失败：${probe.missing.join('；')}` : '';
}

export function probeMeta(probe: TargetProbe): string {
  const platform = Object.hasOwn(PLATFORMS, probe.platform) ? PLATFORMS[probe.platform] : probe.platform;
  const at = new Date(probe.probed_at);
  const time = Number.isNaN(at.getTime()) ? '探测时间未知' : `探测于 ${at.toLocaleTimeString('zh-CN', { hour12: false })}`;
  return `本机平台 ${platform} · ${time}`;
}
