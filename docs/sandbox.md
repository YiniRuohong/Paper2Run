# p2r_sandbox 沙箱组件：威胁模型、隔离层与后端矩阵

`packages/sandbox/p2r_sandbox` 在宿主机上隔离执行不受信任的 Python
代码。它是一个可独立使用的组件（`pip install packages/sandbox`，零第三方
依赖，Python >= 3.11），也是 Paper2Run 评测管线的执行底座。

## 1. 威胁模型

**防什么**（guest = 被执行的不受信任脚本及其派生的全部子进程）：

- guest 主动外联：网络连接（含 loopback）必须失败；
- guest 落盘：除沙箱工作目录外不可写（seatbelt/bwrap 后端）；
- guest 读取宿主敏感数据：`~/.ssh`、`~/.aws`、`~/.config`、`~/.gnupg`、
  `~/.minimax`、`~/Library/Keychains`、`~/Library/Application Support`、
  `/etc/ssh`、`/private/etc/ssh` 与 `~/.env*` 不可读（seatbelt 后端）；
- guest 耗尽宿主资源：CPU、墙钟、进程数、文件大小、内存（含整棵进程树）；
- guest 洪水攻击：stdout/stderr 各按 `max_output_bytes` 截断，收成的
  单文件上限 256 KB；
- 环境泄漏：guest 环境是白名单构造（PATH、TMPDIR），宿主环境变量、API
  key 不进入 guest。

**不防什么**（诚实声明）：

- guest 对非敏感路径的**读**是放开的（seatbelt 上除上述清单外全盘可读；
  Linux 后端完全不限读）。若运行环境把秘密放在非敏感路径，本组件拦不住；
- seatbelt 后端的敏感路径清单是**前缀规则**：清单外的位置（例如
  `~/Documents`）可读；
- `unshare` 后端**不限制工作目录之外的写**——选中它时 stderr 会带
  `[p2r_sandbox]` 说明；
- guest 子进程若自建**新会话**（`setsid`），将逃出进程组击杀范围；
- Windows 只有墙钟 + 输出封顶 + 一次性 cwd + 直接子进程击杀
  （`platform_walls=True`），无网络/内存/进程数防线；
- RSS 内存限制是**反应式**采样（见 §3），不是硬性内核配额；
- guest 输出/文件在 caps 之内是可信通道之外的唯一数据出口，本组件不
  对其内容做任何语义校验（shape 校验由 `validate_output_files` 负责）。

## 2. 隔离层（每层独立、失败如实上报）

1. 独立进程：`python -I`（隔离模式，无视用户 site-packages 与
   `PYTHON*` 环境影响）。
2. 独立会话/进程组（`start_new_session=True`）：一次 `os.killpg` 可击杀
   整棵 guest 进程树。
3. 白名单环境：仅构造 `PATH` 与 `TMPDIR`（指向沙箱工作目录）。macOS
   CoreFoundation 注入的 `LC_CTYPE`/`__CF_USER_TEXT_ENCODING` 属平台常量。
4. 一次性工作目录（`tempfile.mkdtemp`），`finally` 中删除。
5. rlimits（`preexec_fn`）：`RLIMIT_CPU`、`RLIMIT_NPROC`、`RLIMIT_FSIZE`
   (4 MB)、以及 OS 接受时的 `RLIMIT_AS`。每项是否可用由探针实测（在本
   macOS 主机上 `RLIMIT_AS` 对任何有限值都 `EINVAL`，已被实验证实）。
6. 墙钟超时：输出增量泵送 + deadline，超时整组 `SIGKILL`，
   `timed_out=True`。
7. 输出封顶：stdout/stderr 各截断到 `max_output_bytes`（`truncated=True`）。
8. OS 级沙箱后端（见 §4）：网络拒绝 + 越权写拒绝 +（seatbelt）敏感路径
   拒读。**每个能力都必须由探针子进程实测证明**才会在
   `sandbox_capabilities()` 里报 True；证不出来就如实降级并上报
   `backend="none"`、`network_isolated=False`。

所有探针结果按进程缓存一次；全程无 shell，`Popen` 只收字面量 argv。

## 3. 内存限制（macOS 的替代执行路径 + 进程树覆盖)

- `RLIMIT_AS` 在 macOS 上不可用（探针实测 EINVAL）→ 内存用 RSS 监控：
  - **leader 进程**：`proc_pidinfo`（Darwin）/ `/proc/<pid>/statm`
    （Linux），每 ~15 ms 采样——廉价 syscall，保证快速分配爆炸不会漏检；
  - **整棵进程树**：Darwin 用 `ps -o pid=,rss= -g <pgid>`（字面量 argv，
    无 shell），Linux 优先扫 `/proc/*/stat` 的 pgrp 字段 + `statm`（无
    外部进程），每 ~200 ms 汇总一次——限制 guest **子进程**囤积内存；
  - 超限 → 整组 `SIGKILL`，stderr 如实注记
    `memory limit exceeded (... MiB RSS, observed X MiB ...)`。
- 采样是**反应式**的：短于采样间隔的内存尖峰可能不被记账；这是已知且
  已文档化的限制。

## 4. 后端矩阵

| 能力 | seatbelt (macOS) | bwrap (Linux) | unshare -rn (Linux) | none |
| --- | --- | --- | --- | --- |
| 网络拒绝 | ✅ `(deny network*)` | ✅ `--unshare-net` | ✅ netns | ❌ |
| workdir 外写拒绝 | ✅ | ✅ ro-bind + bind + tmpfs | ❌ | ❌ |
| 敏感路径拒读 | ✅ deny 清单 + `~/.env*` regex | ❌（全盘只读可读） | ❌ | ❌ |
| stdlib/解释器可读 | ✅（探针验证 import） | ✅ | ✅ | ✅ |
| 内存 | RSS 树采样（RLIMIT_AS 不可用） | RLIMIT_AS 可用 + RSS 树采样 | RLIMIT_AS 可用 + RSS 树采样 | Windows：无 |
| 进程组击杀 | ✅ | ✅（`--die-with-parent` 兜底） | ✅ | Windows 仅直接子进程 |
| 后端选择顺序 | 1 | 2（darwin 无 bwrap → 跳过） | 3 | 4（兜底） |

后端选择 `_select_backend()`：seatbelt → bwrap → unshare → none；
`SandboxResult.backend` 如实报告本次运行实际使用的后端
（`"seatbelt"`/`"bwrap"`/`"unshare"`/`"none"`）。

**argv 构造是纯函数**（`_bwrap_argv` / `_unshare_argv` /
`_wrapped_argv`），由单元测试逐字节断言；探针与选择逻辑用 monkeypatch
测试。bwrap 的挂载策略：`--unshare-net` + `--ro-bind / /` + workdir 可写
bind + `--tmpfs /tmp` + `--dev /dev` + `--proc /proc` + `--die-with-parent`。

⚠️ 诚实声明：Linux 后端仅在"argv 纯函数 + 探针/选择逻辑"层面被单元测试
覆盖；本开发机是 macOS，**没有在任何 Linux 主机上实跑过 bwrap/unshare
后端**。探针在 Linux 上首次运行时会给出真值，那才是行为生效的时刻。

## 5. 能力键（`sandbox_capabilities()`）

- `seatbelt`：探针子进程实测网络 + 越权写均被拒；
- `sensitive_read_denied`：同一探针子进程实测列读已存在的敏感目录失败
  （EPERM/EACCES）；不存在的路径会得到 ENOENT，**不能**作为证据，所以
  探针选取清单里第一个真实存在的目录（macOS 上恒为
  `~/Library/Keychains`）；
- `rlimits` / `platform_walls`：语义与历史版本一致（旧键不改语义）；
- `bubblewrap` / `unshare_net`：对应 Linux 后端探针结果（二进制不存在
  的平台恒 False）。

键是**只增不改**的：消费方应把未知键当作新增能力报告，而非固定 schema。

## 6. 收集与 CLI

- `collect_files` 支持精确名与顶层 glob（`fnmatch`，如 `*.json`）；所有
  候选（含 glob 命中项）都做 realpath 越界检查，symlink 逃逸会被拒收并
  在 stderr 注记；单文件 256 KB 上限不变。
- CLI：`python -m p2r_sandbox run script.py [--timeout S]
  [--max-memory-mb N] [--collect GLOB ...] [--json]` 与
  `python -m p2r_sandbox capabilities [--json]`。`--json` 输出稳定 JSON
  （sorted keys）：backend、network_isolated、timed_out、exit_code、
  stdout/stderr（UTF-8 解码，errors=replace）、files 名称列表、
  duration_ms、truncated。CLI 退出码 = guest 正退出码 / 击杀 1 / 用法或
  启动失败 2。

## 7. 已知限制清单（诚实汇总）

1. 非敏感路径可读（全平台），清单外敏感文件不防护；
2. `unshare` 后端无写隔离（stderr 注记）；
3. RSS 限制反应式，短尖峰可能漏记账；树扫描间隔 ~200 ms；
4. guest 自建新会话的子进程逃出组击杀；
5. Linux 后端未经实机验证（见 §4 声明）；
6. seatbelt 是 Apple 已标记 deprecated 的机制：未来 macOS 移除
   `sandbox-exec` 时会如实降级为 `backend="none"`；
7. 探针结果按进程缓存一次——运行期间系统环境变化不会重测；
8. Windows 只有墙钟/输出/临时目录防线。
