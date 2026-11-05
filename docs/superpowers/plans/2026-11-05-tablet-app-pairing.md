# 平板 App 与配对码（Plan 2+3）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 完成两件事：一个能装到平板上的 Android App，以及电脑端设置页上的"平板远程"。用户在电脑上点"生成配对码"，在平板 App 里输入这串码就完成配对；之后 App 打开即自动连上，界面和电脑端完全一致，电脑端可以随时撤销设备。

**Architecture:**
- `p2r-tunnel`（Plan 1）新增三个子命令或功能：会合（`rendezvous`，部署在中继服务器上）、配对（`pair`，平板端）、控制接口（电脑端 `serve` 内置，仅本机可访问）。
- 配对流程：配对码分成"桌号"和"口令"两段。会合服务只登记"桌号 → 电脑 id"，不经手口令。两端在 iroh 连接上用 SPAKE2 和口令互相确认身份，再把对方的公钥存下来。
- 会合服务本身也是一个 iroh 节点，只经同一台中继通信，不需要新开端口。
- FastAPI 新增 `/api/v1/remote/*` 路由，转发到电脑端隧道进程的控制接口。设置页新增"平板远程"分组。
- 隧道进程由 `launch.py` 和 Electron 负责启动。
- Android App 用 Kotlin 写，把 Android 版 `p2r-tunnel` 作为可执行文件打包进 APK，以子进程方式运行。配对页之后，前台服务保持隧道常驻，WebView 加载本机代理端口。

**Tech Stack:**
- Rust：在 Plan 1 依赖的基础上新增 `spake2 0.4`、`serde_json`、`hmac`/`sha2`。
- 后端与前端：Python FastAPI（现有）、React + TS（现有）。
- Android：Kotlin + Gradle（wrapper 8.11.1，JDK 17，compileSdk 36，minSdk 26，仅 arm64-v8a）。

**Spec:** `docs/superpowers/specs/2026-11-05-tablet-remote-design.md`（含 2026-11-05 修订的 §5.2）
**基础：** Plan 1 的结果和测试报告 `docs/verification/2026-11-tablet-tunnel-spike.md`

## 本计划对设计文档的具体化（实施时以这里为准）

- **配对码格式**：`NNN-NNNNN`，共 8 位数字。前 3 位是桌号，由会合服务分配，用于查找电脑；后 5 位是口令，由电脑本地生成，只参与 SPAKE2，不会发给会合服务。
  - 码 5 分钟有效、只能用一次，任何一次口令错误都会让这个码立刻作废。所以攻击者只有一次猜测机会，猜中概率 1/10⁵。
  - 这样设计文档 §6 的承诺依然成立：会合服务或中继即使作恶，也无法冒充任何一端。
- **会合服务的形态**：用 iroh 节点代替 HTTP 服务，因为服务器对外只开放了 12234 端口，而它已被中继占用。会合节点的 id 由它在服务器上的固定密钥决定，与中继地址、CA 一起写进公开配置 `apps/tunnel/deploy/remote.json`。
- **允许列表的格式**：从纯文本 `allowed` 改为 `~/.paper2run/remote/devices.json`，内容为 `[{id, name, paired_at}]`。`serve` 照旧在每个请求流上重新读取。
- **Android 端嵌入方式**：设计 §5.3 写的是 uniffi/JNI，这里改为把 Android 版二进制以 `jniLibs/arm64-v8a/libp2rtunnel.so` 的名义打包，用 `ProcessBuilder` 执行。理由：Plan 1 已在这台平板上验证过这个二进制能跑，这样省掉整套 FFI 层；Android 允许执行 `nativeLibraryDir` 下的文件（需 `extractNativeLibs=true`）。
- **"是否为远程请求"的标记**：`serve` 转发前先删掉请求里可能伪造的 `X-P2R-Remote`，再统一设为 `1`。后端据此决定两件事：拒绝远程请求调用 `/api/v1/remote/*` 的写操作；让前端把"平板远程"分组显示为只读。

## Global Constraints

- FastAPI 依然只监听 `127.0.0.1`；电脑端控制接口只监听 `127.0.0.1`，访问需要随机 token。
- 完全不用 n0 公共服务。中继为 `https://60.204.202.153:12234`，QUIC 端口 12234，CA 为 `apps/tunnel/deploy/relay-ca.pem`。
- ALPN：隧道 `p2r/http/1`（不变）；配对 `p2r/pair/1`；会合 `p2r/rendezvous/1`。
- 同一把密钥同一时刻只能有一个隧道进程（Plan 1 的结论），App 必须保证这一点。
- 中继速度不作为目标，能演示即可（用户 2026-11-05 决定）。
- 两项不变式测试必须一直通过：`tests/unit/test_architecture.py`、`tests/unit/phase01/test_packaging.py`。本计划不新增 Python 包，路由放在 `services/api/p2r_api/routes/remote.py`。
- Rust 用 rustup 工具链：命令前加 `PATH="$HOME/.cargo/bin:$PATH"`。
- 测试平板：adb `HA2KPCA4`，Android 16（SDK 36），arm64-v8a。
- 提交信息使用 conventional commits，结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. 同一个配对码被第二台平板再用，或口令输错之后再试，都必须失败，而且第一台的配对结果不受影响——由 Task 2 的 `code_is_single_use` 和 `wrong_secret_burns_code` 钉住。
2. 已配对的平板调用 `/api/v1/remote/*` 的写操作（生成配对码、撤销设备），必须被拒绝；电脑本机调用要正常成功——由 Task 4 的 `remote_header_blocks_writes` 和 Task 3 的 `serve_overrides_spoofed_remote_header` 钉住。
3. 电脑端撤销平板后，平板 App 必须显示"已被撤销，请重新配对"，而不是一直显示"正在重连"——由 Task 3 的 `connect_reports_revoked` 和 Task 7 的验收步骤钉住。
4. App 被系统杀掉或平板重启后重新打开，不需要重新配对就能连上——由 Task 7 的验收步骤钉住。
5. 隧道进程没启动、或电脑上没装 Rust 时，电脑端设置页要明确显示"远程服务未运行"，后端和页面其它部分照常工作——由 Task 4 的 `status_when_sidecar_absent` 和 Task 5 的启动日志钉住。

---

### Task 1：会合服务

**Files:**
- Create: `apps/tunnel/p2r-tunnel/src/rendezvous.rs`
- Create: `apps/tunnel/p2r-tunnel/tests/rendezvous.rs`
- Modify: `src/lib.rs`、`src/main.rs`（新增子命令 `rendezvous --key --relay ...`）

**Interfaces:**
- Produces：
  ```rust
  pub const ALPN_RENDEZVOUS: &[u8] = b"p2r/rendezvous/1";
  pub async fn serve(ep: Endpoint) -> anyhow::Result<()>;                         // 服务器端
  pub async fn claim(ep: &Endpoint, rv: EndpointAddr) -> anyhow::Result<Claim>;   // 电脑端：申请桌号，连接保持到配对结束或过期
  pub struct Claim { pub nameplate: u16 /* 0..=999 */, pub expires_at: SystemTime, /* 内部持有连接 */ }
  pub async fn lookup(ep: &Endpoint, rv: EndpointAddr, nameplate: u16) -> anyhow::Result<EndpointId>; // 平板端
  ```
- 协议：每个请求一条双向流，内容为一行 JSON。桌号在登记它的那条连接断开或 5 分钟到期时释放。查询成功后立即释放（只能用一次）。

- [ ] **Step 1：写失败测试**（复用 `tests/common` 的测试中继）
  - `claim_then_lookup_returns_desktop_id`
  - `lookup_unknown_nameplate_fails`
  - `nameplate_released_after_lookup`：同一桌号第二次查询失败。
  - `nameplate_released_when_claimer_disconnects`
  - `nameplates_are_unique_while_active`：同时申请 50 个，互不重复。
- [ ] **Step 2：** `cargo test --test rendezvous`，预期 FAIL。
- [ ] **Step 3：** 实现。桌号从未占用的号码中随机选。过期时间是一个常量，测试可以注入更短的值。
- [ ] **Step 4：** 预期 PASS；`cargo clippy --all-targets -- -D warnings` 无警告。
- [ ] **Step 5：** 提交 `feat(tunnel): rendezvous service for pairing nameplates`。

### Task 2：配对协议（SPAKE2）与设备存储

**Files:**
- Create: `src/pair.rs`、`src/devices.rs`、`tests/pairing.rs`
- Modify: `src/allow.rs`（改为读 `devices.json`，`allow::load` 签名不变）、`src/main.rs`（新增子命令 `pair`）

**Interfaces:**
- Produces：
  ```rust
  pub const ALPN_PAIR: &[u8] = b"p2r/pair/1";
  pub struct Code { pub nameplate: u16, pub secret: u32 /* 0..100000 */ }   // Display/FromStr: "NNN-NNNNN"，也接受不带横线的 8 位数字
  // 电脑端：在已有 endpoint 上开放一次配对；返回码，以及一个等待结果的 future
  pub async fn host(ep: &Endpoint, rv: EndpointAddr, devices: &Path) -> anyhow::Result<(Code, PairingWait)>;
  pub enum PairOutcome { Paired { id: EndpointId, name: String }, Expired, WrongCode }
  // 平板端：输入码与设备名，返回电脑 id（调用方负责保存）
  pub async fn join(ep: &Endpoint, rv: EndpointAddr, code: &Code, device_name: &str) -> anyhow::Result<EndpointId>;
  // devices.rs
  pub struct Device { pub id: EndpointId, pub name: String, pub paired_at: String }
  pub fn list(path: &Path) -> anyhow::Result<Vec<Device>>;
  pub fn add(path: &Path, d: Device) -> anyhow::Result<()>;       // 原子写入，权限 0600
  pub fn remove(path: &Path, id: EndpointId) -> anyhow::Result<bool>;
  ```
- 消息流程（在 `p2r/pair/1` 双向流上）：
  1. 平板 → 电脑：SPAKE2 消息 A。
  2. 电脑 → 平板：SPAKE2 消息 B。
  3. 双方派生共享密钥 K，各自发送 `HMAC(K, "p2r-pair-v1" ‖ role ‖ 平板id ‖ 电脑id ‖ 设备名)` 作为确认值，并核对对方的。
  4. SPAKE2 的 identity 用两端的 id（iroh TLS 已认证）。
  5. 任一步失败，码立即作废。
- CLI：`pair --key <path> --relay ... --relay-ca ... --rendezvous <id> --code NNN-NNNNN --name <设备名> --out <desktop.id>`。成功时把电脑 id 写入 `--out`，退出码 0；口令错误退出码 3；码不存在或已过期退出码 4。

- [ ] **Step 1：写失败测试**（进程内：测试中继 + 会合 + 电脑端 + 平板端）
  - `pairs_with_correct_code`：平板拿到电脑 id；`devices.json` 里出现平板 id 和设备名；之后平板用 `connect` 能经 `serve` 访问假后端，返回 200。
  - `wrong_secret_burns_code`：口令错误，电脑端返回 `WrongCode`；再用正确口令重试同一个码，也失败。
  - `code_is_single_use`：第一台平板配对成功后，第二台用同一个码失败，`devices.json` 里只有第一台。
  - `code_expires`：注入 1 秒过期，2 秒后再用失败，返回 `Expired`。
  - `devices_file_roundtrip_and_0600`：`add`、`list`、`remove` 都正常，文件权限为 0600。
  - `allow_load_reads_devices_json`：Plan 1 的允许列表测试改为基于 `devices.json`，继续通过。
- [ ] **Step 2：** 预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** `cargo test` 全部通过，包括 Plan 1 的 24 个；clippy 无警告。
- [ ] **Step 5：** 提交 `feat(tunnel): pairing codes over SPAKE2 with a rendezvous nameplate`。

### Task 3：控制接口、远程标记、连接状态输出

**Files:**
- Create: `src/control.rs`、`tests/control.rs`
- Modify: `src/serve.rs`（注入 `X-P2R-Remote`；记录每台设备的最近在线时间和当前路径）、`src/connect.rs`（`--status-json` 输出；识别"已被撤销"）、`src/main.rs`

**Interfaces:**
- `serve` 新增参数：`--devices <devices.json>`（替代 `--allow`）、`--rendezvous <id>`、`--control-file <path>`。
- 启动时，`serve` 在 `127.0.0.1:0` 上监听控制接口，把 `{"port":N,"token":"<32 字节 hex>","pid":P}` 写入 `--control-file`（权限 0600），退出时删除该文件。
- 控制接口（每个请求都要带 `Authorization: Bearer <token>`，否则返回 401）：
  - `GET /status` → `{"desktop_id","relay","devices":[{"id","name","paired_at","online":bool,"path":"direct|relay|null","last_seen"}]}`
  - `POST /pairing` → `{"code":"NNN-NNNNN","expires_at"}`。同一时刻只保留一个配对会话，新建时作废旧的。
  - `GET /pairing` → `{"state":"waiting|paired|expired|wrong_code|none","device":{...}?}`
  - `DELETE /devices/{id}` → 204。从 `devices.json` 删除，并立即关闭该设备的现有连接。
- 撤销设备时，`serve` 以错误码 `2` 和原因 `b"revoked"` 关闭连接；平板首次连接被允许列表拒绝时，同样使用错误码 `2`。
- `connect --status-json`：每次状态变化向 stdout 输出一行 JSON：`{"state":"connecting|connected|revoked","path":"direct|relay"?}`。收到错误码 2 时输出 `revoked` 并以退出码 5 结束，不再重连。

- [ ] **Step 1：写失败测试**
  - `control_requires_token`：不带 token 返回 401，带上返回 200。
  - `control_pairing_flow`：`POST /pairing` 拿到码，平板 `join` 成功后，`GET /pairing` 返回 `paired`；`/status` 能看到这台设备且 `online:true`。
  - `revoke_via_control_cuts_live_connection`
  - `serve_overrides_spoofed_remote_header`：客户端带 `X-P2R-Remote: 0`，假后端看到的是 `1`。
  - `connect_reports_revoked`：撤销后，`connect` 的状态输出包含 `revoked`，进程以退出码 5 结束。
  - `control_file_written_0600_and_removed_on_shutdown`
- [ ] **Step 2：** 预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** 全部通过；clippy 无警告。
- [ ] **Step 5：** 提交 `feat(tunnel): local control API, remote marker and status output`。

### Task 4：会合服务上线、后端路由、启动器

**Files:**
- Modify: `apps/tunnel/deploy/deploy.sh`、新增 `p2r-rendezvous.service`、`remote.json`（`relay_url`、`relay_quic_port`、`rendezvous_id`）
- Create: `services/api/p2r_api/routes/remote.py`、`tests/integration/phase02/test_remote_routes.py`
- Modify: `services/api/p2r_api/routes/__init__.py`、`scripts/launch.py`

**Interfaces:**
- 后端路由（全部只转发到控制接口；控制文件路径来自环境变量 `P2R_REMOTE_CONTROL_FILE`，默认 `~/.paper2run/remote/control.json`）：
  - `GET /api/v1/remote/status`：返回控制接口 `/status` 的内容，外加 `"remote": <本请求是否带 X-P2R-Remote: 1>`、`"available": bool`。隧道进程不在时返回 `{"available": false, "remote": ...}`，状态码 200。
  - `POST /api/v1/remote/pairing`、`GET /api/v1/remote/pairing`、`DELETE /api/v1/remote/devices/{id}`：请求带 `X-P2R-Remote: 1` 时返回 403，错误码 `REMOTE_FORBIDDEN`；隧道进程不在时返回 503，错误码 `REMOTE_UNAVAILABLE`。
- `launch.py`：后端健康检查通过后，如果 `apps/tunnel/target/release/p2r-tunnel` 不存在且装了 `cargo`，先执行 `cargo build --release`；然后以子进程启动 `serve`，参数从 `remote.json` 读取，`--backend-port` 用实际端口。后端退出时一并终止隧道进程。没有 cargo 或构建失败时，打印一行"平板远程不可用：<原因>"，然后继续启动，不影响其他功能。

- [ ] **Step 1：** 在服务器上用 `p2r-tunnel rendezvous` 生成会合节点的密钥并部署为 systemd 服务（Linux 版本仍用 Docker 交叉编译）。把节点 id 写入 `remote.json`。用 `#[ignore]` 测试 `real_rendezvous_roundtrip` 在真实服务器上验证申请桌号再查询，预期通过。同时确认服务器上原有服务（derper、8443）还在。
- [ ] **Step 2：写失败测试** `test_remote_routes.py`（用一个假控制服务器，写临时控制文件）：
  - `status_when_sidecar_absent`：控制文件不存在时，返回 200 和 `available:false`。
  - `status_proxies_and_marks_remote`：带头时 `remote` 为 true，不带为 false。
  - `remote_header_blocks_writes`：`POST /pairing` 带头返回 403，不带返回 200，且假控制服务器只收到一次请求。
  - `pairing_unavailable_returns_503`
- [ ] **Step 3：** `pytest tests/integration/phase02/test_remote_routes.py`，预期 FAIL。
- [ ] **Step 4：** 实现路由和 `launch.py` 的改动。
- [ ] **Step 5：** 根目录 `pytest` 全部通过，包括两项不变式测试。手工验证：`python scripts/launch.py` 启动后，日志里有隧道进程的 `serving id=...`，`curl /api/v1/remote/status` 返回 `available:true`。
- [ ] **Step 6：** 提交 `feat(remote): rendezvous deployment, remote API routes and launcher sidecar`。

### Task 5：电脑端设置页"平板远程"与 Electron 启动

**Files:**
- Create: `apps/web/src/api-client/remote.ts`、`apps/web/src/features/settings/RemoteSettings.tsx`、`apps/web/tests/remote-api.test.mjs`
- Modify: `apps/web/src/features/settings/Settings.tsx`（插入 `<RemoteSettings />`）、`apps/desktop/main.cjs`（与 `launch.py` 相同的启动和终止逻辑；二进制不存在时跳过，不在 Electron 里调用 cargo）

**Interfaces:**
- `remote.ts`：`createRemoteApi(fetcher)`，提供 `status()`、`startPairing()`、`pairingState()`、`revoke(id)`，响应格式与 Task 4 一致。
- 页面行为：
  - 隧道进程不在：显示"远程服务未运行"，并说明原因。
  - 电脑本机访问：显示"生成配对码"按钮。点击后用大号等宽字体显示 `NNN-NNNNN` 和倒计时，每 2 秒轮询一次 `pairingState`。成功后显示"已配对：<设备名>"，失败显示"码已过期"或"码输错，已作废"。
  - 设备列表每行显示：名称、配对时间、在线状态与连接方式（直连或中继），以及一个"撤销"按钮（需要二次确认）。
  - 平板访问（`remote:true`）：只显示设备列表，没有按钮，并提示"配对和撤销只能在电脑上操作"。

- [ ] **Step 1：写失败测试** `remote-api.test.mjs`（参照现有 `*-api.test.mjs` 的写法，用假 fetch）：
  - 四个方法的请求方法、路径和响应解析是否正确；
  - 503 和 403 能转成带错误码的错误；
  - 一个纯函数 `remoteView(status)`，在 未运行 / 本机 / 平板 三种情况下返回正确的视图状态。
- [ ] **Step 2：** `pnpm --dir apps/web test`，预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** `pnpm --dir apps/web test` 和 `pnpm --dir apps/web build` 都通过。在 Electron 开发模式里手工确认设置页能看到配对码。
- [ ] **Step 5：** 提交 `feat(web): tablet remote settings with pairing codes and device list`。

### Task 6：Android App

**Files:**
- Create: `apps/android/`（Gradle 工程，wrapper 8.11.1，包名 `run.paper2run.remote`，应用名"Paper2Run 远程"）
  - `app/src/main/java/run/paper2run/remote/`：
    - `PairActivity.kt`：配对页，输入码和设备名。
    - `MainActivity.kt`：WebView 加状态条。
    - `TunnelService.kt`：前台服务，运行 `connect` 子进程。
    - `Tunnel.kt`：封装子进程启动、解析 stdout 状态 JSON、退出码映射。
    - `Store.kt`：保存电脑 id、设备名、端口。
  - `app/src/main/res/xml/network_security_config.xml`：只对 `127.0.0.1` 允许明文。
  - `app/src/main/assets/`：`relay-ca.pem`、`remote.json`（构建时从 `apps/tunnel/deploy/` 复制）。
  - `app/src/main/jniLibs/arm64-v8a/libp2rtunnel.so`：构建时由 `apps/tunnel/scripts/build-android.sh` 的产物复制而来，不提交。
- Create: `apps/android/build-apk.sh`：依次完成 Rust 交叉编译、复制二进制和配置、`./gradlew assembleDebug`，产出 `apps/android/app/build/outputs/apk/debug/app-debug.apk`。

**行为：**
- 首次启动（还没有电脑 id）进入配对页。设备名默认取 `Build.MODEL`。输入码后运行 `libp2rtunnel.so pair`：
  - 退出码 0：保存电脑 id，进入主页面；
  - 退出码 3："配对码不对，这个码已作废，请在电脑上重新生成"；
  - 退出码 4："配对码不存在或已过期"；
  - 其他：显示错误输出的最后一行。
- 密钥存放在 `filesDir/key`。首次配对时生成，之后一直沿用这一把。
- 主页面启动前台服务 `TunnelService`。它保证同一时刻只有一个 `connect` 进程：启动新进程前先结束旧的。端口固定为 `127.0.0.1:18080`；被占用时依次尝试 18081–18089，结果存入 `Store`。
- `TunnelService` 解析 `connect --status-json` 的输出，通过广播通知 `MainActivity` 更新状态条。状态条显示四种状态："正在连接电脑…"、"已连接 · 直连"、"已连接 · 经中继"、"已被电脑撤销"。
  - 状态为 `revoked`（退出码 5）时：清除电脑 id，弹窗提示"此平板已被电脑撤销，请重新配对"，然后回到配对页。
  - 子进程意外退出时，2 秒后自动重启。
- WebView：
  - 打开 JavaScript 和 DOM storage；
  - 首次连上后加载 `http://127.0.0.1:<端口>/lab`；
  - `onShowFileChooser` 对接系统文件选择器，用于上传 PDF；
  - `DownloadListener` 用 `DownloadManager` 下载导出文件；
  - 返回键在页面内后退。
- 菜单提供"重新配对"：清除电脑 id 并停止服务（保留密钥）。
- `AndroidManifest`：
  - 权限：`INTERNET`、`FOREGROUND_SERVICE`、`FOREGROUND_SERVICE_SPECIAL_USE`、`POST_NOTIFICATIONS`；
  - `TunnelService` 设置 `foregroundServiceType="specialUse"`；
  - `android:extractNativeLibs="true"`；
  - `networkSecurityConfig` 指向上面的配置。

- [ ] **Step 1：写失败测试**（JVM 单元测试，`app/src/test/`，不依赖设备）：
  - `TunnelStatusParserTest`：三种 JSON 状态行、非 JSON 日志行（忽略），以及空行。
  - `PairResultTest`：退出码 0/3/4/其他 映射到正确的界面提示。
  - `CodeInputTest`：`123-45678`、`12345678`、`123 45678` 都能规范化为 `123-45678`；位数不对或含字母时报错。
- [ ] **Step 2：** `./gradlew testDebugUnitTest`，预期 FAIL。
- [ ] **Step 3：** 实现。
- [ ] **Step 4：** `./gradlew testDebugUnitTest` 通过；`build-apk.sh` 产出 APK；`adb -s HA2KPCA4 install -r app-debug.apk` 成功。
- [ ] **Step 5：** 提交 `feat(android): Paper2Run remote app with pairing and tunnel service`。

### Task 7：真机验收

**Files:**
- Modify: `docs/verification/2026-11-tablet-tunnel-spike.md`（追加"App 验收"一节，附截图）
- Modify: `docs/README.md`（索引）、`AGENTS.md`（在 Build & Run 下补一行"平板 App：`apps/android/build-apk.sh`"）

验收前先清掉 Plan 1 留在平板上的命令行进程和 `/data/local/tmp/p2r`，以及电脑上的测试 `serve`。验收全程使用 `python scripts/launch.py` 启动的正式电脑端（数据目录为 `.p2r-app`）。下面每一步都要截图并记录：

- [ ] **Step 1：配对。** 电脑设置页 → 生成配对码 → 平板 App 输入 → 进入主页面，状态条显示已连接；电脑端设备列表出现这台平板。
- [ ] **Step 2：输错码。** 生成新码，在平板上故意输错口令 → 显示"已作废"；再输正确的码也失败；电脑端显示"码输错，已作废"。
- [ ] **Step 3：功能一致。** 在平板上上传一篇论文，走到解析方式选择；电脑端刷新后能看到同一个任务。
- [ ] **Step 4：平板只读。** 平板打开设置页，"平板远程"分组只读；用 curl 经平板端口发 `POST /api/v1/remote/pairing`，返回 403。
- [ ] **Step 5：常驻与恢复。** 用 `am force-stop` 结束 App 再打开，不需要重新配对就能连上；重启平板后打开 App，同样能直接连上。
- [ ] **Step 6：跨网络。** 平板切到手机热点，状态条显示"经中继"，页面可用。
- [ ] **Step 7：撤销。** 电脑端撤销 → 平板几秒内弹出"已被撤销"并回到配对页；这时再访问原端口无响应。
- [ ] **Step 8：** 整理报告并提交 `docs(verification): tablet app pairing acceptance`。
