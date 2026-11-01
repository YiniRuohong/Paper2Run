# p2r_sandbox 沙箱组件：威胁模型、隔离层与后端矩阵

`packages/sandbox/p2r_sandbox` 在宿主机上隔离执行不受信任的 Python
代码。它是一个可独立使用的组件（`pip install packages/sandbox`，零第三方
依赖，Python >= 3.11），也是 Paper2Run 评测管线的执行底座。

## 1. 威胁模型

**防什么**（guest = 被执行的不受信任脚本及其派生的全部子进程）：

- guest 主动外联：网络连接（含 loopback）必须失败；
- guest 落盘：除沙箱工作目录外不可写（seatbelt/landlock/bwrap/appcontainer 后端）；
- guest 读取宿主敏感数据：`~/.ssh`、`~/.aws`、`~/.config`、`~/.gnupg`、
  `~/.minimax`、`~/Library/Keychains`、`~/Library/Application Support`、
  `/etc/ssh`、`/private/etc/ssh` 与 `~/.env*` 不可读（seatbelt 后端）；
  Landlock 与 AppContainer 后端更严：只放行系统目录、解释器目录与 workdir，
  `$HOME`、`/tmp`、用户 profile 整体不可读；
- guest 读取 supervisor 的**启动环境变量**（`/proc/<pid>/environ`、Windows
  PEB 环境块）：Landlock/bwrap/unshare/AppContainer 后端实测拦截（见 §5 泄漏测试）；
- guest 耗尽宿主资源：CPU、墙钟、进程数、文件大小、内存（含整棵进程树）；
- guest 洪水攻击：stdout/stderr 各按 `max_output_bytes` 截断，收成的
  单文件上限 256 KB；
- 环境泄漏：guest 环境是白名单构造（PATH、TMPDIR），宿主环境变量、API
  key 不进入 guest。

**不防什么**（诚实声明）：

- guest 对非敏感路径的**读**：seatbelt 上除上述清单外全盘可读；bwrap
  （仅在内核无 Landlock 时作为兜底）全盘只读可读。Landlock 下 `/etc`、`/usr`
  等系统树可读；
- **macOS seatbelt 无法阻止 guest 经 `sysctl(KERN_PROCARGS2)` 读取同 uid
  进程（含 supervisor）的启动 argv/环境变量**（2026-11 实测，任何 seatbelt
  规则都拦不住，全局拒绝 sysctl-read 又会让 Python 起不来）。Paper2Run 的
  模型 API key 只保存在 provider session 内存中、不进启动环境，因此不受
  影响；部署时不要把秘密放进 API 进程的启动环境变量；
- seatbelt 后端的敏感路径清单是**前缀规则**：清单外的位置（例如
  `~/Documents`）可读；
- `unshare` 后端**不限制工作目录之外的写**——选中它时 stderr 会带
  `[p2r_sandbox]` 说明；
- guest 子进程若自建**新会话**（`setsid`），将逃出进程组击杀范围；
- Windows AppContainer 下 guest 可写自己 profile 的私有存储，该 profile
  每次运行新建、结束即删除，不能跨运行留存数据；
- RSS 内存限制是**反应式**采样（见 §3），不是硬性内核配额；
- guest 输出/文件在 caps 之内是可信通道之外的唯一数据出口，本组件不
  对其内容做任何语义校验（shape 校验由 `validate_output_files` 负责）。

## 2. 隔离层（每层独立、失败如实上报）

1. 独立进程：`python -I`（隔离模式，无视用户 site-packages 与
   `PYTHON*` 环境影响）。
2. 独立会话/进程组（`start_new_session=True`）：`os.killpg` 击杀整棵
   guest 进程树。击杀会**重复发送直到内核返回 ESRCH**（单次 killpg 会漏掉
   恰在 fork 中的子进程，2026-11 实测复现）；并且**每次运行结束后都清扫
   一次进程组**，guest 正常退出时留在后台的子进程同样会被回收。
3. 白名单环境：仅构造 `PATH` 与 `TMPDIR`（指向沙箱工作目录）。macOS
   CoreFoundation 注入的 `LC_CTYPE`/`__CF_USER_TEXT_ENCODING` 属平台常量。
4. 一次性工作目录（`tempfile.mkdtemp`），`finally` 中删除。
5. rlimits（`preexec_fn`）：`RLIMIT_CPU`、`RLIMIT_NPROC`、`RLIMIT_FSIZE`
   (4 MB)、以及 OS 接受时的 `RLIMIT_AS`。每项是否可用由探针实测（在本
   macOS 主机上 `RLIMIT_AS` 对任何有限值都 `EINVAL`，已被实验证实）。
6. 墙钟超时：输出增量泵送 + deadline，超时整组 `SIGKILL`，
   `timed_out=True`。
7. 输出封顶：stdout/stderr 各截断到 `max_output_bytes`（`truncated=True`）。
9. **fail-closed**：`run_python_sandbox(..., require_isolation=True)` 在选中
   的后端不属于 `FULL_ISOLATION_BACKENDS`（seatbelt/landlock/bwrap/
   appcontainer，即"网络 + workdir 外写"两道墙都经探针证实）时，**运行前**
   抛 `SandboxUnavailable`。Paper2Run 执行模型代码的唯一入口
   `p2r_runner.run_sandbox_python` 默认开启；运维可设
   `P2R_ALLOW_UNISOLATED_SANDBOX=1` 显式放行，结果里记
   `sandbox.isolation_required=false` 与实际 `backend`。
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

| 能力 | seatbelt (macOS) | landlock (Linux) | bwrap (Linux) | unshare -rn (Linux) | appcontainer (Windows) | none |
| --- | --- | --- | --- | --- | --- | --- |
| 网络拒绝 | ✅ `(deny network*)` | ✅ seccomp 拒 `socket()` + Landlock TCP 规则 | ✅ `--unshare-net` | ✅ netns | ✅ 无任何 capability（含 loopback） | ❌ |
| workdir 外写拒绝 | ✅ | ✅（`/tmp`、`/dev/shm` 同样拒） | ✅ ro-bind | ❌ | ✅ 仅 workdir 授 ACL | ❌ |
| 敏感/用户目录拒读 | ✅ 清单 + `~/.env*` | ✅ `$HOME`/`/tmp`/`/var` 整体不可读 | ❌ | ❌ | ✅ 用户 profile 整体不可读 | ❌ |
| 读 supervisor 启动环境 | ❌（KERN_PROCARGS2，见 §1） | ✅ 拒（Landlock ptrace 作用域） | ✅ 拒（userns） | ✅ 拒（userns） | ✅ 拒（OpenProcess 被拒） | ❌ |
| 内存 | RSS 树采样 | RLIMIT_AS + RSS 树采样 | RLIMIT_AS + RSS 树采样 | RLIMIT_AS + RSS 树采样 | Job Object 提交内存硬上限（含子孙） | Windows 同左 |
| 进程树击杀 | ✅ | ✅ | ✅（`--die-with-parent`） | ✅ | `TerminateJobObject` + `KILL_ON_JOB_CLOSE` | Windows 同左 |
| 需要的条件 | `sandbox-exec` | 内核 ≥5.13 且启用 Landlock LSM；**标准 Docker 默认 seccomp 下可用**，无需特权 | 允许 userns | 允许 userns | Win10+，一次性给解释器目录授 ACL | — |

后端选择 `_select_backend()`：macOS seatbelt；Linux landlock → bwrap →
unshare；Windows appcontainer；都不可用则 none。`SandboxResult.backend`
如实报告本次运行实际使用的后端。Landlock 排在 bwrap 前，因为它在相同的
网络/写墙之外还限制读、拒绝所有 socket（含 UDP/unix）、挡住读 supervisor
环境；bwrap 作为无 Landlock 内核的兜底。

**Landlock + seccomp 启动器（`_landlock_launcher.py`）**：不依赖用户命名
空间，也不依赖 preexec（supervisor 是多线程的）。启动 argv 为
`python -I -c <启动器源码> <workdir> <python> -I script.py`，启动器在 guest
进程内依次：`PR_SET_NO_NEW_PRIVS` → 按内核 ABI 计算 handled rights 建
Landlock ruleset（workdir 全权限；`/dev/null` 读写；`/usr`、`/lib*`、`/bin`、
`/sbin`、`/etc`、`/opt`、`/proc`、`/sys`、`/dev` 与解释器前缀只读+执行；ABI≥4
TCP bind/connect 无放行规则即拒；ABI≥6 信号与抽象 unix socket 限定在域内）→
seccomp-bpf（校验 arch，异架构直接 kill；x86_64 拒 x32；`socket`、
`io_uring_*` 返回 EPERM）→ `execv` 目标。任一步失败打印
`[p2r_sandbox] landlock launcher failed:` 并以 121 退出，supervisor 将其转成
`SandboxUnavailable`，**绝不无约束运行**。只放行解释器前缀而不放行
`sys.path` 任意条目，避免 editable 安装把项目根（含 `.env`）暴露给 guest。
BPF 程序与 rights 计算是纯函数，由单测里的小型 BPF 解释器逐 syscall 验证。

**Windows AppContainer（`_winappc.py`）**：每次运行新建一个 AppContainer
profile（`p2r.sbx.<pid>.<随机>`），只带一个自定义 capability
`p2rSandboxInterpreterRead`，不带任何系统 capability。解释器目录（venv 根
与 base 安装目录）一次性、幂等地给该 capability SID 授可继承 RX ACL；
workdir 只授给本次运行的容器 SID，因此并发运行互相看不到对方 workdir。
进程用 `CreateProcessW` + `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` +
`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`（只继承三个 stdio 句柄）挂起创建，先
放入 Job Object 再 `ResumeThread`；结束后删除 profile 及其私有存储。
`LOCALAPPDATA` 必须出现在环境里（`CreateProcess` 用它定位容器目录，缺失
返回 ERROR_ENVVAR_NOT_FOUND），内核会把它重定向到容器目录。

**argv 构造是纯函数**（`_bwrap_argv` / `_unshare_argv` /
`_wrapped_argv`），由单元测试逐字节断言；探针与选择逻辑用 monkeypatch
测试。bwrap 的挂载策略：`--unshare-net` + `--ro-bind / /` + workdir 可写
bind + `--dev /dev --remount-ro /dev` + `--proc /proc` + `--die-with-parent`。
不挂私有 `/tmp` tmpfs：tmpfs 占用的是宿主内存，不受 RLIMIT_AS 约束；
guest 的临时文件经 `TMPDIR` 落在 workdir 内。bwrap 会在 child 里自行设置
`PWD`（值为 workdir），不含宿主数据。

**Linux 实跑矩阵（2026-11-01，OrbStack，Debian python:3.12-slim，aarch64，
bubblewrap 0.12.0，非 root 用户）**：

| 容器模式 | 选中后端 | 外网 | 写 `~` | 写 `/tmp` | 沙箱测试 |
| --- | --- | --- | --- | --- | --- |
| 默认 seccomp | none（userns 被拦，如实降级 netiso=False） | 通 | 通 | 通 | 40 passed / 5 skipped |
| seccomp+apparmor unconfined | bwrap | 拒 | 拒 | 拒（EROFS） | 42 passed / 3 skipped |
| --privileged | bwrap | 拒 | 拒 | 拒（EROFS） | 42 passed / 3 skipped |
| unconfined 且移除 bwrap | unshare（stderr 注明无写隔离） | 拒 | 拒* | 通 | 41 passed / 4 skipped |

\* unshare 下 `~` 写失败是因为 userns 里 uid 映射为 root 后对宿主 home
无写权限，属副作用，不是本后端承诺的能力。各模式结束后均无残留进程。

实跑中发现并修复：①探针把 bwrap 私有 loopback 的连接拒绝误判为"网络
未隔离"，导致 bwrap 被错误跳过，现改为"外网不通且只有 `lo` 网卡"；
②原 `--tmpfs /tmp` 让 guest 能写 `/tmp`，并可借 tmpfs 消耗宿主内存，现已
去掉，`/dev` 也重挂为只读；③Linux 上 `LC_CTYPE`（PEP 538）和 bwrap 的
`PWD` 是平台常量，测试白名单相应放宽。

**Windows Job Object（`_winjob.py`）**：guest 以 `CREATE_SUSPENDED` 启动，
先 `AssignProcessToJobObject` 再 `NtResumeProcess`，因此从第一条指令起就在
Job 内，不存在"先跑起来再挂进 Job"的竞态窗口；任何一步失败都抛
`SandboxUnavailable`，绝不无约束运行。Job 设置：`JobMemoryLimit` =
`max_memory_mb`、`ActiveProcessLimit` = `max_processes`、`PerJobUserTimeLimit`
= `max_cpu_seconds`、`KILL_ON_JOB_CLOSE`、`DIE_ON_UNHANDLED_EXCEPTION`，以及
全部 UI 限制（剪贴板、全局原子、桌面切换、显示/系统参数、ExitWindows、外部
USER 句柄）。超时和 leader 正常退出后都会 `TerminateJobObject`，后台孙进程
不能存活，也不能占住管道。

**Windows 实跑（2026-11-02，Windows 11 build 26200，AMD64，Python 3.12.7，
经 WSL interop 启动，supervisor 本身已在一个外层 Job 里）**：`job_object=True`；
64 MiB 限制下 guest 和其子进程申请 300 MiB 均得到 `MemoryError`；
`max_processes=4` 时第 4 个子进程创建失败；`DETACHED_PROCESS` 后台孙进程在
leader 退出后被回收；超时整树击杀；带 `CREATE_BREAKAWAY_FROM_JOB` 启动的子进程
仍受 Job 约束（嵌套 Job 下 Windows 不拒绝启动，但不脱离限制）。

## 5. 能力键（`sandbox_capabilities()`）

- `seatbelt`：探针子进程实测网络 + 越权写均被拒；
- `sensitive_read_denied`：同一探针子进程实测列读已存在的敏感目录失败
  （EPERM/EACCES）；不存在的路径会得到 ENOENT，**不能**作为证据，所以
  探针选取清单里第一个真实存在的目录（macOS 上恒为
  `~/Library/Keychains`）；
- `rlimits` / `platform_walls`：语义与历史版本一致（旧键不改语义）；
- `bubblewrap` / `unshare_net`：对应 Linux 后端探针结果（二进制不存在
  的平台恒 False）；
- `job_object`：仅 Windows，探针 guest 在 64 MiB Job 内申请 256 MiB 必须
  得到 `MemoryError` 才为 True；
- `landlock`：探针子进程经启动器约束后，外网不通、`/var/tmp` 写被拒、
  `/proc/<父进程>/environ` 读被拒，三项都成立才为 True；
- `appcontainer`：探针子进程在 AppContainer 内，supervisor 的 loopback 监听
  始终未收到连接、外网被拒、`%TEMP%` 写被拒、`OpenProcess(supervisor)` 被拒，
  才为 True；
- `sensitive_read_denied`：随选中后端取对应探针的拒读结果（seatbelt：敏感
  目录；landlock：`$HOME`；appcontainer：用户 profile 根目录）。

**跨进程环境泄漏测试（`tests/unit/sandbox/test_env_leak.py`）**：起一个
启动环境带随机金丝雀的 supervisor 子进程，让 guest 用平台最强手段读取：
Linux 扫全部 `/proc/*/environ`，macOS `KERN_PROCARGS2`，Windows
`OpenProcess` + PEB 环境块 `ReadProcessMemory`。none 后端必须读到（证明
检测器有效）；landlock、bwrap、unshare、appcontainer 都必须读不到；
seatbelt 读得到，标记为 xfail（已知限制）。

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
5. 标准 Docker 默认 seccomp 禁止 userns，bwrap/unshare 不可用，但
   Landlock 后端照常生效（已实测）；仅当内核不支持 Landlock（<5.13 或 LSM
   未启用）且不允许 userns 时才会落到 `none`，此时 Paper2Run 拒绝执行模型
   代码；workdir 内文件数与磁盘占用无上限；
6. seatbelt 是 Apple 已标记 deprecated 的机制：未来 macOS 移除
   `sandbox-exec` 时会如实降级为 `backend="none"`；
7. 探针结果按进程缓存一次——运行期间系统环境变化不会重测；
8. Windows AppContainer 首次运行会给 guest 解释器的 venv 与 base 安装目录
   加一条可继承的只读 ACE（授给自定义 capability SID，不影响其他程序）；
   移除可用 `icacls <目录> /remove *<SID> /T`。supervisor 崩溃时本次运行的
   AppContainer profile 可能残留（无数据，下次不复用）。Windows 上无
   `RLIMIT_FSIZE`，单文件大小不设上限；AppContainer 内不能创建符号链接；
9. macOS seatbelt 下 guest 可读同 uid 进程的启动 argv/环境（`KERN_PROCARGS2`），
   见 §1；秘密不要放进 supervisor 的启动环境变量。
