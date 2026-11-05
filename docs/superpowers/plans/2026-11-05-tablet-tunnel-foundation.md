# 平板隧道基础（Plan 1/3）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建好 iroh 隧道的 Rust 核心、电脑端 HTTP 转发、平板端本地代理，并在用户华为云上部署自托管中继，用真平板实测直连/中继/降级，产出可行性报告。

**Architecture:** 一个 Rust 二进制 `p2r-tunnel`，两个子命令：`serve`（电脑端，接受已允许公钥的 iroh 连接，每个 QUIC 双向流当作一条 HTTP/1.1 连接，用 hyper 反向代理到 `127.0.0.1:<后端端口>` 并改写 Host/Origin）和 `connect`（平板端，本地 TCP 监听，每条 TCP 连接开一个 QUIC 双向流原样搬运字节）。两端只用一台自托管 iroh-relay（`https://60.204.202.153:12234`，自签证书钉死），关闭所有 n0 地址发现。

**Tech Stack:** Rust stable（≥1.91，iroh MSRV）、iroh 1.4.1、iroh-relay 1.4.1、tokio、hyper 1.x / hyper-util、clap、anyhow、tracing；Android NDK + cargo-ndk；Debian 12 + systemd。

**Spec:** `docs/superpowers/specs/2026-11-05-tablet-remote-design.md`

**本计划范围：** 只做规格 §9 的"第一步"。配对码/PAKE/设备管理/FastAPI 路由/设置页（Plan 2）和 Android App（Plan 3）不在本计划内，等本计划的可行性报告出来后再写。

## Global Constraints

- FastAPI 后端监听地址保持 `127.0.0.1`，本计划不改任何 Python/前端代码。
- 全程不访问任何 n0 公共服务：不用 `presets::N0`，清空 address lookup，relay map 只含自托管中继。
- 中继：主机 `60.204.202.153`，只用端口 **12234**（TCP 给 HTTPS 中继，UDP 给 QUIC 地址发现）；不得占用或重启服务器上已有的 `derper`（TCP 80/443、UDP 3478）和 8443 上的 `python3` 服务。
- 服务器 SSH：`ssh -i ~/.ssh/p2r_relay_ed25519 -o IdentitiesOnly=yes root@60.204.202.153`（只用密钥）。
- ALPN 固定为 `b"p2r/http/1"`。
- 私钥文件权限必须是 0600。
- 状态必须如实：连接方式只能由 iroh 报告的实际路径决定，不得推断。
- Rust 工具链用 rustup 的：命令前加 `PATH="$HOME/.cargo/bin:$PATH"`（Homebrew 的 cargo 排在 PATH 前面，它没有 Android target）。
- 测试平板：adb 序列号 `HA2KPCA4`，Lenovo TB522FU，Android 16（SDK 36），`arm64-v8a`。
- 代码放在 `apps/tunnel/`（Cargo workspace），不进 `packages/`，不影响 `test_architecture.py` / `test_packaging.py`。
- 提交信息用 conventional commits，结尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. 后端返回 SSE（`text/event-stream`）时，代理必须逐块转发，不能缓冲到响应结束——Task 3 的 `sse_chunks_arrive_before_stream_ends` 钉住。
2. 64 MB 的 PDF 上传（`P2R_PAPER_MAX_BYTES` 默认上限）经隧道必须完整到达且不 OOM——Task 3 的 `large_upload_streams_through` 钉住（测试用 16 MB，流式而非整体读入内存）。
3. 浏览器同一时间开多个连接（页面轮询 + SSE + 上传并行）时互不阻塞——Task 4 的 `parallel_connections_do_not_block` 钉住。
4. 后端没启动或端口错时，平板端应收到明确的 502 而不是连接挂起——Task 3 的 `backend_down_returns_502` 钉住。
5. 电脑端 `serve` 重启后，平板端 `connect` 不需要重启就能自动恢复——Task 4 的 `connect_recovers_after_serve_restart` 钉住。

---

### Task 1: Workspace 与密钥持久化

**Files:**
- Create: `apps/tunnel/Cargo.toml`（workspace，members = `["p2r-tunnel"]`）
- Create: `apps/tunnel/p2r-tunnel/Cargo.toml`
- Create: `apps/tunnel/p2r-tunnel/src/main.rs`（clap 骨架：`serve`、`connect`、`id` 三个子命令，暂时只实现 `id`）
- Create: `apps/tunnel/p2r-tunnel/src/keys.rs`
- Create: `apps/tunnel/.gitignore`（`target/`）

**Interfaces:**
- Produces: `keys::load_or_create(path: &Path) -> anyhow::Result<iroh::SecretKey>`；`p2r-tunnel id --key <path>` 打印公钥（`EndpointId` 的字符串形式）一行。

- [ ] **Step 1: 写失败测试**（`keys.rs` 内 `#[cfg(test)]`，用 `tempfile`）
  - `creates_key_with_0600`：文件不存在时创建，`metadata().permissions().mode() & 0o777 == 0o600`（`#[cfg(unix)]`）。
  - `reload_returns_same_key`：调用两次，`public()` 相等。
  - `rejects_corrupt_file`：文件内容为 `b"garbage"` 时返回 `Err`，且不覆盖原文件。
- [ ] **Step 2:** `PATH="$HOME/.cargo/bin:$PATH" cargo test --manifest-path apps/tunnel/Cargo.toml` → FAIL（函数未定义）。
- [ ] **Step 3:** 实现 `load_or_create`：以 32 字节原始私钥存储；创建时先写临时文件、设 0600、再 rename；父目录不存在则创建。
- [ ] **Step 4:** 重跑测试 → PASS；`cargo run -- id --key /tmp/k` 打印一行公钥，两次运行相同。
- [ ] **Step 5:** 提交 `feat(tunnel): scaffold p2r-tunnel workspace with persistent identity`。

### Task 2: 只用自托管中继的 Endpoint

**Files:**
- Create: `apps/tunnel/p2r-tunnel/src/endpoint.rs`
- Create: `apps/tunnel/p2r-tunnel/tests/relay_only.rs`

**Interfaces:**
- Produces:
  ```rust
  pub struct NetConfig {
      pub relay_url: iroh::RelayUrl,          // 例 https://60.204.202.153:12234
      pub relay_ca_pem: Option<Vec<u8>>,      // 自签 CA，None = 系统根证书
      pub relay_only: bool,                   // true = clear_ip_transports，用于强制中继测试
  }
  pub async fn bind(cfg: &NetConfig, key: iroh::SecretKey) -> anyhow::Result<iroh::Endpoint>;
  pub fn peer_addr(cfg: &NetConfig, peer: iroh::EndpointId) -> iroh::EndpointAddr; // id + relay_url，无直连地址
  pub const ALPN: &[u8] = b"p2r/http/1";
  ```
- 说明：只有一台中继，所以对端地址 = 公钥 + 固定中继 URL，不需要任何地址发现服务；iroh 会在中继连接建立后自行尝试打洞升级为直连。

- [ ] **Step 1: 写失败测试** `tests/relay_only.rs`：启动 iroh 自带的测试中继（`iroh` 的 `test-utils` feature，dev-dependency），两端各 `bind`，B 用 `peer_addr` 连 A，开双向流收发 `b"ping"`/`b"pong"`。
  - `connects_via_relay_without_discovery`：通过。
  - `no_default_address_lookup`：断言构建出的 endpoint 没有配置任何 address lookup（按 1.4.1 API 能查到的方式断言；查不到就用 `clear_address_lookup()` 后连接一个只给公钥、不给 relay 的 `EndpointAddr`，断言连接失败）。
- [ ] **Step 2:** 跑 → FAIL。
- [ ] **Step 3:** 实现 `bind`：从 `Endpoint::builder` 的空白起点构建（不用 N0 preset），`secret_key`、`alpns(vec![ALPN.to_vec()])`、`relay_mode(RelayMode::Custom(RelayMap::from(relay_url)))`、`clear_address_lookup()`；`relay_ca_pem` 存在时通过 `ca_roots_config`/`ca_tls_config` 只信任该 CA；`relay_only` 时 `clear_ip_transports()`。具体方法签名以 docs.rs/iroh/1.4.1 为准。
- [ ] **Step 4:** 跑 → PASS。
- [ ] **Step 5:** 提交 `feat(tunnel): endpoint pinned to a single self-hosted relay`。

### Task 3: `serve`——电脑端 HTTP 反向代理

**Files:**
- Create: `apps/tunnel/p2r-tunnel/src/serve.rs`
- Create: `apps/tunnel/p2r-tunnel/src/allow.rs`
- Create: `apps/tunnel/p2r-tunnel/tests/serve_proxy.rs`
- Modify: `apps/tunnel/p2r-tunnel/src/main.rs`（接上 `serve` 子命令）

**Interfaces:**
- Consumes: Task 2 的 `bind`、`ALPN`、`NetConfig`。
- Produces:
  - `allow::load(path: &Path) -> anyhow::Result<HashSet<EndpointId>>`：每行一个公钥，`#` 开头为注释，空行忽略。每次新连接时重新读文件（本计划不做热推送；Plan 2 会换成控制接口）。
  - `serve::run(ep: Endpoint, backend_port: u16, allow_path: PathBuf) -> anyhow::Result<()>`
  - CLI：`p2r-tunnel serve --key <path> --relay <url> [--relay-ca <pem>] --backend-port <u16> --allow <path>`
  - 每条连接日志一行：`peer=<id> path=<direct|relay> rtt_ms=<n>`，路径变化时再打一行。

- [ ] **Step 1: 写失败测试** `tests/serve_proxy.rs`（测试中继 + 本地 hyper 假后端，假后端回显收到的 `Host`、`Origin`、`Sec-Fetch-Site` 头）：
  - `rejects_peer_not_in_allowlist`：未列入的 peer 连接被关闭，假后端收到 0 个请求。
  - `rewrites_host_and_origin`：客户端发 `Host: 127.0.0.1:18080`、`Origin: http://127.0.0.1:18080`、`Sec-Fetch-Site: same-origin`，假后端看到 `Host: 127.0.0.1:<backend_port>`、`Origin: http://127.0.0.1:<backend_port>`、无 `Sec-Fetch-Site`；无 Origin 的请求不凭空添加 Origin。
  - `sse_chunks_arrive_before_stream_ends`：假后端每 200 ms 发一个 SSE 事件共 5 个，客户端在 700 ms 内收到至少 2 个事件。
  - `large_upload_streams_through`：16 MB 请求体，假后端收到的 SHA-256 与发送方一致。
  - `backend_down_returns_502`：backend_port 指向无人监听的端口，客户端收到状态 502。
  - `allowlist_reloaded_per_connection`：先空允许列表被拒，写入公钥后新连接成功。
- [ ] **Step 2:** 跑 → FAIL。
- [ ] **Step 3:** 实现：accept 循环 → 检查 `remote_id` 在允许列表 → 每个 `accept_bi` 用 `tokio::io::join(recv, send)` 包成 IO，交给 `hyper::server::conn::http1`（`serve_connection` 开 keep-alive），请求经 `hyper-util` legacy client 转发到 `http://127.0.0.1:<backend_port>`，body 全程流式（不 `collect`）。路径类型从 iroh 1.4.1 的连接路径信息读取（以 docs.rs 为准）。
- [ ] **Step 4:** 跑 → PASS。
- [ ] **Step 5:** 提交 `feat(tunnel): serve forwards allowlisted peers to the local backend`。

### Task 4: `connect`——平板端本地代理

**Files:**
- Create: `apps/tunnel/p2r-tunnel/src/connect.rs`
- Create: `apps/tunnel/p2r-tunnel/tests/end_to_end.rs`
- Modify: `apps/tunnel/p2r-tunnel/src/main.rs`

**Interfaces:**
- Consumes: Task 2、Task 3。
- Produces:
  - `connect::run(ep: Endpoint, peer: EndpointAddr, listen: SocketAddr) -> anyhow::Result<()>`
  - CLI：`p2r-tunnel connect --key <path> --relay <url> [--relay-ca <pem>] [--relay-only] --peer <id> --listen 127.0.0.1:18080`
  - 维护一个 iroh `Connection`，断开后指数退避重连（初始 500 ms，上限 10 s）；每条本地 TCP 连接 `open_bi` 后双向 `copy`。日志同 Task 3 的 `path=` 格式。

- [ ] **Step 1: 写失败测试** `tests/end_to_end.rs`（测试中继 + 假后端 + 进程内 serve 与 connect）：
  - `get_through_tunnel`：`reqwest` 访问 `http://127.0.0.1:<listen>/healthz` 得到假后端的 200 和正文。
  - `parallel_connections_do_not_block`：一条 SSE 长连接挂着时，并发 20 个 GET 在 2 s 内全部完成。
  - `connect_recovers_after_serve_restart`：关掉 serve 端 endpoint 再以同一密钥重启，10 s 内新请求成功，connect 进程未重启。
  - `relay_only_reports_relay`：`relay_only = true` 时日志/状态报告 `path=relay`。
- [ ] **Step 2:** 跑 → FAIL。
- [ ] **Step 3:** 实现 `connect::run`。
- [ ] **Step 4:** 跑全部测试 → PASS；`cargo clippy --all-targets -- -D warnings` 无警告。
- [ ] **Step 5:** 提交 `feat(tunnel): connect exposes the remote backend on a local port`。

### Task 5: 部署自托管中继到华为云

**Files:**
- Create: `apps/tunnel/deploy/relay.toml`
- Create: `apps/tunnel/deploy/p2r-relay.service`
- Create: `apps/tunnel/deploy/make-cert.sh`（生成自签 CA + 带 IP SAN `60.204.202.153` 的服务器证书，CA 有效期 10 年）
- Create: `apps/tunnel/deploy/deploy.sh`（构建、上传、安装、重启，幂等）
- Create: `apps/tunnel/deploy/README.md`（部署与回滚命令）

**Interfaces:**
- Produces：`https://60.204.202.153:12234` 上可用的 iroh-relay；客户端信任文件 `apps/tunnel/deploy/relay-ca.pem`（提交，公开无害）；CA 私钥与服务器私钥**不提交**（加入 `.gitignore`，只存在本机 `~/.paper2run/relay-secrets/` 与服务器 `/etc/p2r-relay/`，0600）。

- [ ] **Step 1:** `make-cert.sh` 生成证书，`openssl x509 -in server.pem -noout -ext subjectAltName` 输出含 `IP Address:60.204.202.153`。
- [ ] **Step 2:** 用 Docker（OrbStack）`--platform linux/amd64` 的 `rust:1-bookworm` 镜像 `cargo install iroh-relay --version 1.4.1 --features server --locked`（feature 名以 crates.io 页面为准），产物 `iroh-relay` 在 `file` 下显示 `ELF 64-bit LSB ... x86-64`。
- [ ] **Step 3:** `relay.toml`：HTTPS 绑定 `0.0.0.0:12234`，manual cert 模式指向 `/etc/p2r-relay/server.pem` 与 `server.key`；QUIC 地址发现绑定 UDP `0.0.0.0:12234`；关闭 STUN/其它会占用 80/443/3478 的监听；systemd unit 以专用用户 `p2r-relay` 运行、`Restart=always`。
- [ ] **Step 4:** `deploy.sh` 部署后验证：
  - 服务器上 `ss -lntup | grep 12234` 同时有 tcp 与 udp 监听；`ss -lntup` 中 `derper` 与 8443 仍在（与部署前对比）。
  - 本机 `curl --cacert apps/tunnel/deploy/relay-ca.pem -sS -o /dev/null -w '%{http_code}' https://60.204.202.153:12234/` 返回 `200`（如 1.4.1 的根路径不是 200，则用其健康检查路径，记录在 README）。
  - 把 Task 2 的 `relay_only.rs` 加一个 `#[ignore]` 测试 `real_relay_roundtrip`，环境变量 `P2R_RELAY_URL`/`P2R_RELAY_CA` 指向真实中继，`cargo test -- --ignored real_relay_roundtrip` 通过。
- [ ] **Step 5:** 提交 `feat(tunnel): self-hosted iroh relay deployment for 60.204.202.153:12234`。

### Task 6: 平板实测与可行性报告

**Files:**
- Create: `apps/tunnel/scripts/build-android.sh`（`cargo ndk -t arm64-v8a build --release`）
- Create: `docs/verification/2026-11-tablet-tunnel-spike.md`

**Interfaces:**
- Consumes: Task 1–5 全部。

- [ ] **Step 1: 工具链**：用 Android SDK 安装 NDK（`sdkmanager "ndk;<最新稳定版>"`；无 cmdline-tools 时先装 cmdline-tools），`rustup target add aarch64-linux-android`，`cargo install cargo-ndk`。`build-android.sh` 产出 `target/aarch64-linux-android/release/p2r-tunnel`，`file` 显示 `ARM aarch64 ... Android`。
- [ ] **Step 2: 部署到平板**：`adb -s HA2KPCA4 push` 二进制与 `relay-ca.pem` 到 `/data/local/tmp/p2r/`，`chmod 755`；`adb shell /data/local/tmp/p2r/p2r-tunnel id --key /data/local/tmp/p2r/key` 打印公钥，写入电脑端允许列表。
- [ ] **Step 3: 起两端**：电脑上用现有方式启动后端（`python scripts/launch.py`，记下端口），再起 `p2r-tunnel serve`；平板 `adb shell` 后台起 `connect --listen 127.0.0.1:18080`；`adb -s HA2KPCA4 forward tcp:18080 tcp:18080`，在 Mac 上 `curl http://127.0.0.1:18080/healthz` 得到 200（这条请求实际经过 USB→平板→网络→电脑）。
- [ ] **Step 4: 三个场景，各测一次并记录**（每项都记 `path=`、首包延迟、`/healthz` 平均 RTT（20 次）、20 MB 上传耗时、一次真实运行的 SSE 日志是否实时滚动）：
  - A 同一 Wi-Fi；
  - B 跨网络：平板连手机热点（4G/5G），电脑留在原网络——这是规格的核心场景；
  - C 强制中继：B 的网络下加 `--relay-only`。
- [ ] **Step 5: 真页面**：场景 B 下 `adb shell am start -a android.intent.action.VIEW -d http://127.0.0.1:18080/lab`，在平板浏览器里走一遍上传论文 → 抽取，截图（`adb exec-out screencap -p`）放进报告；记录任何因 Host/Origin/cookie 导致的失败。
- [ ] **Step 6: 写报告** `docs/verification/2026-11-tablet-tunnel-spike.md`：三场景数据表、截图、观察到的问题、对 Plan 2/3 的结论（是否继续 iroh；4G 下直连成功率；中继带宽是否够 64 MB 上传）。只写实际测到的，未测的明确写"未测"。
- [ ] **Step 7:** 提交 `docs(verification): tablet tunnel spike results`。
