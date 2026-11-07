# p2r_sandbox 执行目标：Docker 实验容器

`packages/sandbox/p2r_sandbox/autonomous/` 是自主模式唯一的执行面：`ExecutionTarget` 在本机或 SSH 目标上为每个目标配置维护一个专用 Docker 容器，Pi 的文件、命令、后台任务、日志和采集工具都经由它执行。包只用标准库，不 import 任何其他 `p2r_*` 包，可用 `pip install packages/sandbox` 单独安装。

旧的宿主进程沙箱（seatbelt、Landlock、bwrap、unshare、Windows AppContainer/Job Object 后端，以及 `python -m p2r_sandbox` 命令行）已于 2026-11-05 随旧模式删除。

## 1. 组成

| 文件 | 作用 |
|---|---|
| `execution.py` | `ExecutionTarget` 与 `ExecutionError`：在宿主侧通过 `docker` CLI（本机）或 OpenSSH（远端）创建、校验和操作容器 |
| `_container.py` | 容器内可信控制器，经 stdin 写入 `/opt/p2r-controller.py`，不挂载源码、状态或凭据；负责网络规则、作业、文件和日志 |
| `_interfaces.py` | 在目标宿主上枚举 IPv4 网卡地址（libc `getifaddrs`），供网络策略屏蔽宿主地址 |

`_container.py` 与 `_interfaces.py` 以文本形式读入后发到目标执行，因此它们不被 Python import，但必须随包发布（`scripts/desktop/build_desktop.py` 会检查打包后的 `_container.py`）。

## 2. 目标配置

```json
{"kind": "local", "workspace": "/abs/dedicated/dir", "image": "paper2run-experiment:1",
 "network": {"public_internet": false, "host_and_metadata_access": false},
 "gpu_devices": [], "storage_bytes": null}
```

- `kind` 为 `local` 或 `ssh`；`ssh` 需要 `ssh_alias`（已存在的 OpenSSH 配置别名），可选 `ssh_config`。
- `workspace` 必须是专用的绝对路径，不能是 `/`、不能含 `..`、不能是符号链接，也不能包含控制面状态目录；它绑定挂载到容器的 `/work`。
- `network.host_and_metadata_access` 必须为 `false`；`public_internet` 必须显式给出布尔值。
- 容器名由配置的哈希决定（`p2r-<24 位>`），重启、重连或启动响应丢失后都能找到同一个容器。

## 3. 隔离措施

- **容器**：独立 Docker 网络；`--cap-drop ALL`，只加回控制器需要的 `NET_ADMIN`、`SETUID`、`SETGID`、`CHOWN`、`DAC_OVERRIDE`、`KILL`、`SETPCAP`；`no-new-privileges`；`--pids-limit 1024`。
- **用户**：每个后台作业分配一个不复用的 UID（20000 到 60000，组 10001），文件操作以 UID 10001 执行；两者都经 `setpriv` 清空能力集和附加组并设置 `no_new_privs`。控制器状态目录仅 root 可读。
- **网络**：IPv4 出站默认 DROP；本地回环只放行 Docker 内置 DNS；私网、链路本地（含 `169.254.169.254` 元数据地址）、组播、保留段和目标宿主自身地址一律 REJECT；`public_internet=true` 时才放行 UID 10001 和作业 UID 的公网访问。IPv6 全部拒绝。
- **验证**：建立后由 `network_probe` 在容器内实测（公网可达性、私网与元数据地址被拒、回环监听不可连、IPv6 不通），只有 `EACCES`/`EPERM`/`EHOSTUNREACH` 算作策略拒绝，超时或连接被拒不算。探测失败即停止容器并报 `network_isolation_failed`，不降级到宿主 shell。
- **复核**：每次可能启动作业或触碰工作区的操作前都会重新执行 `prepare()`：校验容器启动身份、宿主地址和策略指纹，Docker 重启导致规则丢失时会被发现（`isolation_changed`、`host_addresses_changed`）。
- **SSH**：`BatchMode=yes`、`StrictHostKeyChecking=yes`、`ForwardAgent=no`、`ClearAllForwardings=yes`；未知或变化的主机指纹直接拒绝（`ssh_host_key`），不会自动写入 `known_hosts`。
- **凭据**：模型凭据和 SSH 凭据都不进入实验容器。
- **镜像**：用 `bash scripts/autonomous/build-image.sh` 在目标上构建 `paper2run-experiment:1`，从不自动拉取任意镜像；缺镜像时 `probe()` 给出构建提示，`prepare()` 报 `image_missing`。

## 4. 操作

| 方法 | 说明 |
|---|---|
| `probe()` | 只读检查：Docker 可用性、镜像、容器状态、网络验证结果 |
| `prepare()` | 创建或复核容器；启动作业和读写、采集工作区前都会自动调用 |
| `start(command, launch_id, timeout_seconds)` | 启动后台作业；同一 `launch_id` 去重，传输超时后按它对账 |
| `execute(command, timeout_seconds)` | 短命令 |
| `status` / `logs` / `stop` | 作业状态、按偏移分页的日志（单次上限 1,000,000 字节）、终止该作业 UID 下的全部进程；这三项只做观察或停止，不先复核隔离 |
| `read` / `write` / `collect` | 工作区文件读写与产物取回 |

## 5. 已知限制

- 存储限额是观测值（`storage_enforcement: observed`），不是硬限制。
- GPU 通过 `gpu_devices` 以 `--gpus` 传给 Docker；用户尚未提供远程 GPU 目标，本机 Docker 和本机 sshd 测试不等于远端 GPU 验收。
- 真实 Docker 与 SSH 集成测试需显式开启：`P2R_DOCKER_TESTS=1`、`P2R_SSH_TESTS=1`（见 `tests/integration/autonomous/`）。
- Windows 宿主的网卡枚举和路径挂载尚未移植（见设计说明 §5.11）。
