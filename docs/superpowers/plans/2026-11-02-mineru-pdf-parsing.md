# MinerU PDF 解析集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让任何安装了 Paper2Run 的人上传真实 PDF 论文后，经 MinerU（在线免 key Agent API 或离线本机安装）转成带页码标记的 Markdown，再进入模型抽取。

**Architecture:** 新增独立 L1 包 `p2r_docparse`（仅标准库 + pypdf；切分、在线客户端、离线子进程、离线安装器、标记拼合、上下文裁剪）。L3 `p2r_orchestration` 新增解析服务与独立 `paper_parse` worker 线程，结果存 `paper_texts` 表 + 内容寻址 artifact。L4 `p2r_api` 新增 `/docparse/*` 与 `/papers/{id}/parse|text` 路由，Provider 抽取改读解析文本。前端在 Guided 第 1 步加模式选择弹窗、解析进度，设置页加「PDF 解析」分组。

**Tech Stack:** Python 3.11+（FastAPI、SQLAlchemy、Alembic、pytest）、pypdf 6.x、MinerU 4.1.x（仅离线、独立 venv）、React + TypeScript（node --test）。

**Spec:** `docs/superpowers/specs/2026-11-02-mineru-pdf-parsing-design.md`

## Global Constraints

- 仓库根：`/Users/andyzhao/Documents/claude code/Ali_Hackthon`；分支 `feat/mineru-pdf-parsing`；Python 命令一律用 `.venv/bin/python`。
- 分层：`p2r_docparse` 为 L1 且 STANDALONE（不导入任何 `p2r_*`）；`tests/unit/test_architecture.py`、`tests/unit/phase01/test_packaging.py` 必须始终通过。
- 三文件源根同步，顺序一致：`packages/docparse` 紧跟在 `packages/sandbox` 之后。
- 新依赖：仅 `pypdf>=6.21,<7` 进主环境；MinerU 只装进离线独立 venv，锁定 `mineru==4.1.3`（Task 13 实机验证后如需调整只改常量）。
- 网络只用 `urllib`，请求头带 `User-Agent: paper2run/0.1`。
- 在线 API：`https://mineru.net/api/v1/agent/parse/file`（POST JSON）→ PUT `file_url` → `GET https://mineru.net/api/v1/agent/parse/{task_id}`；限制单文件 ≤10 MB、≤20 页；切分上限 `max_pages=20`、`max_bytes=9_500_000`。
- 远端 URL 必须 `https` 且主机等于或以 `.` 结尾匹配 `mineru.net` / `openxlab.org.cn` / `aliyuncs.com`；不跟随重定向；单段 Markdown 下载上限 8 MB。
- 页码标记格式（逐字）：`<!-- p2r:pages {a}-{b} -->`、单页 `<!-- p2r:page {a} -->`、缺失 `<!-- p2r:pages {a}-{b} missing -->`。
- 离线子进程：字面量 argv、无 shell、环境白名单，绝不传 `MINERU_API_KEY`、绝不加 `--remote`；设 `MINERU_TELEMETRY_DISABLED=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`。
- 离线支持平台：仅 `darwin-arm64`、`win32-x64`；其它（含 Intel Mac `darwin-x64`，永久）返回 `unsupported`。
- 失败绝不在在线/离线之间自动切换。
- 公开错误码固定（见 Task 1 `ERROR_CODES`），不透出原始异常、第三方响应原文。
- 提交：Conventional Commits，结尾加 `Co-Authored-By: Claude Code <noreply@anthropic.com>`。

## Review Focus

1. **加密/损坏 PDF**：上传或切分时 pypdf 打不开，用户应得到明确的 `PDF_INVALID`，而不是 500 —— 测试加在 Task 1 与 Task 8。
2. **在线提交返回业务错误码**（如 `code=-30003` 或 HTTP 200 但 `code!=0`）：应映射为 `DOCPARSE_REMOTE_FAILED`，原始 `msg` 不进入持久化或响应 —— 测试加在 Task 3。
3. **选了离线但离线未就绪 / 环境失效**：解析任务应以 `DOCPARSE_OFFLINE_NOT_READY` 失败，绝不改走在线 —— 测试加在 Task 7。
4. **应用在解析中途重启**：`papers.status` 不能永远卡在 `parsing`；过期租约在启动时重新入队 —— 测试加在 Task 7。
5. **解析进行中用户切换模式**：正在跑的任务按开始时的模式完成，新解析用新模式；切换不打断也不串用 —— 测试加在 Task 7。

---

### Task 1: `p2r_docparse` 包骨架、注册与 PDF 切分

**Files:**
- Create: `packages/docparse/p2r_docparse/__init__.py`, `packages/docparse/p2r_docparse/errors.py`, `packages/docparse/p2r_docparse/pdf.py`, `packages/docparse/p2r_docparse/testing.py`
- Modify: `pyproject.toml`（`dependencies` 加 `"pypdf>=6.21,<7"`；`where`、`pythonpath` 在 `"packages/sandbox"` 后插入 `"packages/docparse"`；`include` 加 `"p2r_docparse*"`）, `scripts/launch.py:36-38`（`SOURCE_ROOTS` 同位置插入）, `tests/unit/test_architecture.py:13-20`（`LAYERS` 加 `"p2r_docparse": 1`，`STANDALONE` 加 `"p2r_docparse"`）
- Test: `tests/unit/docparse/test_pdf.py`

**Interfaces:**
- Produces:
  - `class DocParseError(Exception)`，属性 `code: str`；`ERROR_CODES = frozenset({"DOCPARSE_PDF_INVALID","DOCPARSE_PAGE_TOO_LARGE","DOCPARSE_RATE_LIMITED","DOCPARSE_REMOTE_FAILED","DOCPARSE_TIMEOUT","DOCPARSE_UNSAFE_URL","DOCPARSE_RESPONSE_TOO_LARGE","DOCPARSE_OFFLINE_NOT_READY","DOCPARSE_OFFLINE_FAILED","DOCPARSE_OFFLINE_UNSUPPORTED","DOCPARSE_CANCELLED","DOCPARSE_MODE_UNSET"})`；构造时 code 不在集合中抛 `ValueError`。
  - `count_pages(pdf: bytes) -> int`：pypdf 读取；加密（空密码也打不开）、损坏、0 页 → `DocParseError("DOCPARSE_PDF_INVALID")`。
  - `@dataclass(frozen=True) class Chunk: first_page: int; last_page: int; data: bytes`（页码 1 起、闭区间）。
  - `plan_chunks(pdf: bytes, *, max_pages: int = 20, max_bytes: int = 9_500_000) -> list[Chunk]`。
  - `testing.minimal_pdf(pages: list[str], *, object_streams: bool = False) -> bytes`：纯标准库生成每页一行 Helvetica 文本的合法 PDF（离线冒烟与测试共用）。`object_streams=True` 时把页对象放进 zlib 压缩的对象流（`/Type /ObjStm`）并用交叉引用流，模拟 pdfTeX 默认输出——原始字节中不出现 `/Type /Page`。

- [ ] **Step 1: 写失败测试** `tests/unit/docparse/test_pdf.py`：

```python
from p2r_docparse.errors import DocParseError
from p2r_docparse.pdf import count_pages, plan_chunks
from p2r_docparse.testing import minimal_pdf

def test_count_pages(): assert count_pages(minimal_pdf(["a", "b", "c"])) == 3
def test_invalid_pdf():
    with pytest.raises(DocParseError) as e: count_pages(b"not a pdf")
    assert e.value.code == "DOCPARSE_PDF_INVALID"
def test_encrypted_pdf_is_invalid(): ...  # pypdf PdfWriter.encrypt("pw") 生成，断言 code == "DOCPARSE_PDF_INVALID"
def test_exactly_20_pages_one_chunk():
    assert [(c.first_page, c.last_page) for c in plan_chunks(minimal_pdf([str(i) for i in range(20)]))] == [(1, 20)]
def test_21_pages_two_chunks():
    assert [(c.first_page, c.last_page) for c in plan_chunks(minimal_pdf([str(i) for i in range(21)]))] == [(1, 20), (21, 21)]
def test_oversize_chunk_halves(): # max_bytes 设为单页 PDF 体积的 2.5 倍、10 页 → 每段 ≤ max_bytes 且区间连续覆盖 1..10
def test_single_page_too_large():
    with pytest.raises(DocParseError) as e: plan_chunks(minimal_pdf(["x"]), max_bytes=10)
    assert e.value.code == "DOCPARSE_PAGE_TOO_LARGE"
def test_chunk_is_valid_pdf(): # 每个 chunk.data 经 count_pages 等于 last-first+1
def test_object_stream_pdf():
    data = minimal_pdf(["a", "b", "c"], object_streams=True)
    assert b"/Type /Page" not in data and b"/Type/Page" not in data
    assert count_pages(data) == 3
```

- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/docparse/test_pdf.py -v`，预期 ImportError。
- [ ] **Step 3: 安装依赖并实现** — `uv pip install --python .venv/bin/python -e .`；实现 errors/pdf/testing；完成全部注册修改。`plan_chunks` 先按 20 页分区间，每区间用 `PdfWriter` 写出，超 `max_bytes` 则对半递归，单页仍超则抛错。
- [ ] **Step 4: 运行通过** — `.venv/bin/python -m pytest tests/unit/docparse tests/unit/test_architecture.py tests/unit/phase01/test_packaging.py -v`，全部 PASS。
- [ ] **Step 5: 提交** — `feat(docparse): add standalone p2r_docparse package with PDF chunking`

### Task 2: 页码标记拼合/拆分、解析结果类型、上下文裁剪

**Files:**
- Create: `packages/docparse/p2r_docparse/markers.py`, `packages/docparse/p2r_docparse/context.py`
- Test: `tests/unit/docparse/test_markers.py`, `tests/unit/docparse/test_context.py`

**Interfaces:**
- Consumes: Task 1 `Chunk`。
- Produces:
  - `@dataclass(frozen=True) class ChunkOutcome: first_page: int; last_page: int; markdown: str | None`（`None` = 失败）。
  - `@dataclass(frozen=True) class ParseResult: markdown: str; backend: Literal["online_agent","offline_mineru"]; backend_version: str; page_count: int; pages_parsed: int; chunks: list[dict]; warnings: list[str]`；`chunks` 元素为 `{"first_page": int, "last_page": int, "status": "done"|"missing"}`。
  - `merge_outcomes(outcomes: list[ChunkOutcome]) -> str`、`split_merged(markdown: str) -> list[ChunkOutcome]`（只识别行首、逐字匹配的标记；`missing` 段返回 `markdown=None`）。
  - `missing_warnings(outcomes) -> list[str]`：每个失败段一条 `"第 {a}–{b} 页解析失败，未包含"`（单页 `"第 {a} 页解析失败，未包含"`）。
  - `trim_for_context(markdown: str, max_chars: int) -> tuple[str, str | None]`：先删参考文献节，再截断；截断时返回警告 `"仅第 1–{N} 页发送给模型（超出上下文上限）"`，N 为截断点之前最后一个标记的末页；无标记时警告 `"文本超出上下文上限，已截断"`；未截断返回 `None`。

- [ ] **Step 1: 写失败测试**：
  - `test_merge_split_roundtrip`：三段（1–20 有文本、21–40 `None`、41–41 有文本）→ `split_merged(merge_outcomes(x)) == x`；合并文本含逐字 `<!-- p2r:pages 21-40 missing -->` 与 `<!-- p2r:page 41 -->`。
  - `test_missing_warnings`：上例 → `["第 21–40 页解析失败，未包含"]`。
  - `test_marker_inside_line_not_split`：正文行中间出现标记串不被识别。
  - `test_trim_drops_references_keeps_appendix`：`# 1 Intro…# References…# A Appendix…` → 结果不含 References 节内容、含 Appendix，未超长时警告为 `None`。
  - `test_trim_truncates_with_page_warning`：标记 1–20、21–40 的文本，`max_chars` 落在第二段中 → 警告 `"仅第 1–20 页发送给模型（超出上下文上限）"`，结果长度 ≤ `max_chars`。
  - `test_trim_handles_chinese_heading`：`## 参考文献` 同样被删。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/docparse/test_markers.py tests/unit/docparse/test_context.py -v`。
- [ ] **Step 3: 实现** — 参考文献标题正则：`^(#{1,6})\s*(?:\d+\.?\s*)?(references|bibliography|参考文献)\s*$`（忽略大小写，多行），删到下一个同级或更高级标题为止。
- [ ] **Step 4: 运行通过**（同上命令）。
- [ ] **Step 5: 提交** — `feat(docparse): page markers, parse result type and context trimming`

### Task 3: 在线 Agent API 客户端与分段解析

**Files:**
- Create: `packages/docparse/p2r_docparse/http.py`, `packages/docparse/p2r_docparse/online.py`
- Test: `tests/unit/docparse/test_online.py`

**Interfaces:**
- Consumes: Task 1 `plan_chunks`、`DocParseError`；Task 2 `ChunkOutcome`、`ParseResult`、`merge_outcomes`、`split_merged`、`missing_warnings`。
- Produces:
  - `@dataclass HttpRequest: method: str; url: str; headers: dict[str,str]; body: bytes | None; timeout: float`；`@dataclass HttpResponse: status: int; body: bytes`；`Transport = Callable[[HttpRequest], HttpResponse]`；`urllib_transport(req) -> HttpResponse`（不跟随重定向，3xx 直接作为响应返回；读取 body 时超过 `max_body` 抛 `DOCPARSE_RESPONSE_TOO_LARGE`）。
  - `check_remote_url(url: str) -> None`（不合规抛 `DOCPARSE_UNSAFE_URL`）。
  - `class OnlineAgentClient(transport: Transport = urllib_transport, *, base_url="https://mineru.net", poll_interval=2.5, chunk_timeout=300.0, max_retries=5, sleep=time.sleep, clock=time.monotonic)`，方法 `parse_chunk(data: bytes, file_name: str) -> str`。
  - `parse_online(pdf: bytes, *, client: OnlineAgentClient, previous_markdown: str | None = None, on_progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] = lambda: False) -> ParseResult`：`backend="online_agent"`、`backend_version="agent-v1"`；`previous_markdown` 中区间完全相同且非 missing 的段直接复用；某段重试耗尽 → 该段 `None`；全部段失败抛 `DOCPARSE_REMOTE_FAILED`。

- [ ] **Step 1: 写失败测试**（全部用假 `Transport`，记录请求序列，`sleep` 为空函数）：
  - `test_happy_path_single_chunk`：请求序列为 POST `/api/v1/agent/parse/file`（JSON 含 `file_name`、`enable_table: true`、`enable_formula: true`、`is_ocr: false`，无 `Authorization` 头，含 `User-Agent: paper2run/0.1`）→ PUT `file_url`（无 `Content-Type`）→ GET 轮询（`pending`→`done`）→ GET `markdown_url`；返回 Markdown。
  - `test_429_backoff_then_success`：提交先 429 两次再成功，断言 `sleep` 被调用且最终成功。
  - `test_business_error_code_maps`：提交返回 HTTP 200 `{"code": -30003, "msg": "SECRET-RAW"}` → `DocParseError.code == "DOCPARSE_REMOTE_FAILED"`，且 `"SECRET-RAW"` 不在 `str(err)` 中。（Review Focus 2）
  - `test_failed_state_maps`：轮询 `state=failed` → 该段失败。
  - `test_unsafe_urls_rejected`：`file_url` 为 `http://…`、`https://evil.example/…`、`https://mineru.net.evil.com/…` 各自 → `DOCPARSE_UNSAFE_URL`。
  - `test_chunk_timeout`：一直 `running`，`clock` 前进超 `chunk_timeout` → 该段失败。
  - `test_parse_online_splits_and_merges`：21 页 PDF → 两段请求，合并文本含 `<!-- p2r:pages 1-20 -->` 与 `<!-- p2r:page 21 -->`，`page_count == 21`、`pages_parsed == 21`。
  - `test_partial_failure_and_retry_reuses`：第 2 段失败 → 结果 `warnings == ["第 21 页解析失败，未包含"]`、`pages_parsed == 20`；再以该 Markdown 作 `previous_markdown` 调用 → 只发出 1 次提交。
  - `test_connection_error_retries`：transport 抛 `OSError` 至重试耗尽 → 段失败而非异常外泄。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/docparse/test_online.py -v`。
- [ ] **Step 3: 实现** — 退避：`min(2 ** attempt, 30)` 秒；可重试条件：429、5xx、`OSError`/超时；`file_name` 用 `paper-p{a}-{b}.pdf`。
- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: 提交** — `feat(docparse): MinerU agent API client with chunked upload and merge`

### Task 4: 离线解析子进程

**Files:**
- Create: `packages/docparse/p2r_docparse/proc.py`, `packages/docparse/p2r_docparse/offline.py`
- Test: `tests/unit/docparse/test_offline.py`

**Interfaces:**
- Consumes: Task 2 `ParseResult`、`ChunkOutcome`、`merge_outcomes`。
- Produces:
  - `@dataclass CompletedRun: returncode: int; stdout_tail: str; stderr_tail: str; timed_out: bool`。
  - `run_tree(argv: list[str], *, env: dict[str,str], cwd: Path, timeout: float, cancelled: Callable[[], bool] = lambda: False) -> CompletedRun`：POSIX `start_new_session=True` + `os.killpg(SIGKILL)`；Windows `CREATE_NEW_PROCESS_GROUP` + `taskkill /PID {pid} /T /F`；输出只保留尾部 4 KB。
  - `venv_python(root: Path) -> Path`、`mineru_kit(root: Path) -> Path`（POSIX `venv/bin/…`，Windows `venv\Scripts\….exe`）。
  - `offline_env(root: Path) -> dict[str,str]`：只含 `PATH`（venv bin 在前 + 系统默认）、Windows 下 `SYSTEMROOT`/`WINDIR`/`TEMP`/`TMP`/`USERPROFILE`、POSIX 下 `HOME`/`TMPDIR`、`MINERU_HOME=<root>/mineru-home`、`MINERU_TELEMETRY_DISABLED=1`、`HF_HUB_OFFLINE=1`、`TRANSFORMERS_OFFLINE=1`、`PIP_*` 之外的一切均不继承。
  - `parse_offline(pdf: bytes, *, root: Path, tier: str, version: str, timeout: float = 1200.0, run=run_tree, cancelled=...) -> ParseResult`：argv `[str(mineru_kit(root)), "parse", <in.pdf>, "-o", <out_dir>, "--tier", tier]`；`backend="offline_mineru"`、`backend_version=version`（调用方传入安装状态中的版本）。输出解析：若输出目录内存在含 `page_idx` 的内容列表 JSON，则按页生成 `ChunkOutcome`（逐页标记）；否则读取 `.md`，整份作为 `1..page_count` 一段。非零退出或超时 → `DOCPARSE_OFFLINE_FAILED` / `DOCPARSE_TIMEOUT`。

- [ ] **Step 1: 写失败测试**（用测试内生成的假 `mineru-kit` Python 脚本放到临时 `root/venv/bin`，Windows 分支用 monkeypatch 平台判断只测路径计算）：
  - `test_env_whitelist`：在 `os.environ` 注入 `MINERU_API_KEY=k`、`OPENAI_API_KEY=k` 后，`offline_env` 结果不含这两个键，含 `MINERU_TELEMETRY_DISABLED == "1"`、`HF_HUB_OFFLINE == "1"`。
  - `test_argv_has_no_remote`：假脚本把 argv 写入文件，断言不含 `--remote`、含 `--tier basic`。
  - `test_page_level_output`：假脚本写出含 `page_idx` 0/1 的 JSON → 结果含 `<!-- p2r:page 1 -->` 与 `<!-- p2r:page 2 -->`。
  - `test_markdown_only_output`：假脚本只写 `.md` → 结果含 `<!-- p2r:pages 1-2 -->`。
  - `test_timeout_kills_tree`：假脚本启动一个子进程后 `sleep 60`，`timeout=1` → `DOCPARSE_TIMEOUT`，且子进程 pid 已不存在（POSIX 下用 `os.kill(pid, 0)` 断言 `ProcessLookupError`）。
  - `test_windows_paths`：monkeypatch 为 win32 → `mineru_kit(root) == root/"venv"/"Scripts"/"mineru-kit.exe"`。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/docparse/test_offline.py -v`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: 提交** — `feat(docparse): offline mineru-kit runner with env whitelist and tree kill`

### Task 5: 离线安装器、平台判定、模式设置

**Files:**
- Create: `packages/docparse/p2r_docparse/platform.py`, `packages/docparse/p2r_docparse/install.py`, `packages/docparse/p2r_docparse/settings.py`
- Test: `tests/unit/docparse/test_install.py`, `tests/unit/docparse/test_settings.py`

**Interfaces:**
- Consumes: Task 4 `run_tree`、`venv_python`、`mineru_kit`、`offline_env`、`parse_offline`；Task 1 `testing.minimal_pdf`。
- Produces:
  - `current_platform() -> str`（`"darwin-arm64"|"darwin-x64"|"win32-x64"|"linux-x64"|…`）；`OFFLINE_PLATFORMS = frozenset({"darwin-arm64", "win32-x64"})`。
  - `MINERU_VERSION = "4.1.3"`；`PIP_MIRROR = "https://mirrors.aliyun.com/pypi/simple/"`；`MIN_FREE_BYTES = 6 * 1024**3`；`STEPS = ("preflight", "venv", "packages", "tier", "models", "smoke")`。
  - `@dataclass InstallState: state: Literal["not_installed","installing","ready","broken","failed","unsupported"]; step: str | None; progress: float; tier: str | None; version: str | None; bytes_on_disk: int; error: str | None`，`to_json() -> dict`。
  - `class OfflineInstaller(root: Path, *, base_python: Path = Path(sys.executable), run=run_tree, disk_free=lambda p: shutil.disk_usage(p).free, platform=current_platform)`：`state() -> InstallState`（读 `<root>/install-state.json`；平台不支持恒为 `unsupported`）、`start() -> InstallState`（后台 daemon 线程；已在安装 → 原样返回；不支持 → 抛 `DOCPARSE_OFFLINE_UNSUPPORTED`）、`cancel() -> InstallState`、`uninstall() -> InstallState`、`ensure_healthy() -> bool`（`mineru-kit --version` 30 s；失败则只重做 `venv`+`packages`，成功返回 True，状态短暂为 `broken`）、`wait(timeout: float) -> InstallState`（测试用）。
  - 安装步骤：preflight（`disk_free(root) >= MIN_FREE_BYTES`，否则 failed `"磁盘空间不足（需要约 6 GB）"`）→ venv（若 `base_python -m pip --version` 成功：`-m venv --without-pip` + 后续用 `base_python -m pip --python <venv_python>`；否则 `-m venv` 带 pip，用 venv 自己的 pip）→ packages（`install --disable-pip-version-check --no-input mineru==MINERU_VERSION`，失败后加 `-i PIP_MIRROR` 重试一次）→ tier（`mineru-kit hardware detect --json`，取推荐档位，取不到用 `"basic"`）→ models（tier 为 `flash` 跳过；否则 `models download --tier T` 再 `models verify --tier T`）→ smoke（`parse_offline(minimal_pdf(["Paper2Run smoke"]))` 输出含 `smoke`）。任一步失败：删除 `venv`，`mineru-home` 保留，状态 `failed` 带中文错误。
  - `class DocParseSettings(root: Path)`：`mode() -> Literal["online","offline"] | None`、`set_mode(mode: Literal["online","offline"]) -> None`（`<root>/settings.json`，原子写）。

- [ ] **Step 1: 写失败测试**（注入假 `run` 记录 argv 并按脚本返回结果；`platform` 注入）：
  - `test_unsupported_platform`：`platform=lambda: "darwin-x64"` → `state().state == "unsupported"`，`start()` 抛 `DOCPARSE_OFFLINE_UNSUPPORTED`。
  - `test_happy_install_sequence`：断言 argv 顺序覆盖 venv → pip install `mineru==4.1.3` → `hardware detect --json` → `models download --tier standard` → `models verify --tier standard`；最终 `ready`、`tier == "standard"`、`version == "4.1.3"`。
  - `test_base_without_pip_uses_venv_pip`：假 `run` 让 `-m pip --version` 失败 → venv 命令不含 `--without-pip`。
  - `test_mirror_fallback`：第一次 pip install 返回 1 → 第二次 argv 含 `-i https://mirrors.aliyun.com/pypi/simple/`。
  - `test_flash_skips_models`：detect 推荐 `flash` → 无 `models download` 调用。
  - `test_low_disk_fails`：`disk_free` 返回 1 GB → `failed`，error 含「磁盘空间不足」。
  - `test_cancel_cleans_venv`：在 packages 步骤中 `cancel()` → `not_installed`，`root/venv` 不存在。
  - `test_state_persists`：新建 `OfflineInstaller(root)` 读到上次 `ready`。
  - `test_ensure_healthy_rebuilds_without_models`：`--version` 失败 → 只出现 venv/pip 调用，无 `models download`。
  - `test_settings_roundtrip`：`mode()` 初始 `None`；`set_mode("online")` 后新实例读回 `"online"`；非法值抛 `ValueError`。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/docparse/test_install.py tests/unit/docparse/test_settings.py -v`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过**；再跑 `.venv/bin/python -m pytest tests/unit -q` 确认无回归。
- [ ] **Step 5: 提交** — `feat(docparse): on-demand offline MinerU installer and mode settings`

### Task 6: `paper_texts` 表

**Files:**
- Modify: `packages/persistence/p2r_persistence/models.py`（`SourceRefRecord` 之前新增模型）
- Create: `packages/persistence/alembic/versions/0019_paper_texts.py`
- Test: `tests/unit/phase02/test_paper_texts_model.py`

**Interfaces:**
- Produces: `class PaperTextRecord(TimestampMixin, Base)`，表 `paper_texts`：`id`（`"ptx_" + uuid4`）、`paper_id`（FK papers, CASCADE, index）、`source_sha256: str(64)`、`text_sha256: str(64) | None`、`backend: str(32) | None`、`backend_version: str(64) | None`、`page_count: int`、`pages_parsed: int`、`chunk_map_json: JSON`、`warnings_json: JSON`、`status: str(32)`（CheckConstraint `status IN ('parsing','ready','failed')`）、`error_json: JSON | None`。迁移 `revision="0019"`、`down_revision="0018"`，含 downgrade。

- [ ] **Step 1: 写失败测试**：`test_paper_text_roundtrip`（SQLite 内存库建表、插入、按 `paper_id` 查回）；`test_status_constraint`（插入 `status="done"` 抛 `IntegrityError`）；`test_migration_heads`（`alembic.script.ScriptDirectory` 的 head 为 `"0019"`）。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/phase02/test_paper_texts_model.py -v`。
- [ ] **Step 3: 实现模型与迁移**（迁移写法照 `0018_drop_identity_invites.py`，`TS = sa.DateTime(timezone=True)`）。
- [ ] **Step 4: 运行通过**；`.venv/bin/python -m pytest tests -q -k "migration or alembic or model"` 无回归。
- [ ] **Step 5: 提交** — `feat(persistence): paper_texts table for parsed paper Markdown`

### Task 7: 解析服务与 `paper_parse` worker

**Files:**
- Create: `packages/orchestration/p2r_orchestration/docparse_service.py`, `packages/orchestration/p2r_orchestration/docparse_jobs.py`
- Test: `tests/unit/phase02/test_docparse_jobs.py`

**Interfaces:**
- Consumes: Task 3 `parse_online`、`OnlineAgentClient`；Task 4 `parse_offline`；Task 5 `OfflineInstaller`、`DocParseSettings`；Task 6 `PaperTextRecord`；`JobRepository.enqueue/claim/heartbeat/complete/fail/requeue_expired`；artifact store `put_bytes`/`open_bytes`。
- Produces:
  - `class DocParseService(root: Path, *, settings: DocParseSettings | None = None, installer: OfflineInstaller | None = None, online_client: OnlineAgentClient | None = None, parse_online_fn=parse_online, parse_offline_fn=parse_offline)`：`mode()`、`parse(pdf: bytes, *, mode: str, previous_markdown: str | None, on_progress) -> ParseResult`（`mode="offline"` 时先 `installer.state().state == "ready"` 且 `ensure_healthy()`，否则 `DOCPARSE_OFFLINE_NOT_READY`；随后以 `tier=state.tier`、`version=state.version` 调 `parse_offline_fn`；绝不回退在线）。
  - `PARSE_JOB_KIND = "paper_parse"`；`enqueue_paper_parse(db, *, paper_id: str, mode: str, idempotency_key: str) -> Job`：`payload_ref = f"{paper_id}:{mode}"`（模式在入队时锁定），写 `PaperTextRecord(status="parsing")`，`papers.status="parsing"`。
  - `process_next_parse_job(session_factory, artifact_store, service: DocParseService, *, worker_id: str = "docparse") -> bool`：解析期间另起心跳线程每 20 s `heartbeat(lease_seconds=60)`；成功写 artifact、`PaperTextRecord(ready)`、`papers.status="ready"`；`DocParseError` → `PaperTextRecord(failed, error_json={"code": e.code})`、`papers.status="failed"`；进度写入 `chunk_map_json` 供轮询。重试时从该论文最近一次 `ready` 记录的 Markdown 取 `previous_markdown`。
  - `start_docparse_worker(session_factory, artifact_store, service) -> tuple[threading.Event, threading.Thread]`（启动先 `requeue_expired(kinds=(PARSE_JOB_KIND,))`）、`stop_docparse_worker(stop, thread, timeout=2.0) -> bool`。

- [ ] **Step 1: 写失败测试**（SQLite 内存库、`LocalArtifactStore(tmp_path)`、假 `parse_*_fn`）：
  - `test_online_parse_success`：入队 → 处理 → `PaperTextRecord.status == "ready"`、`papers.status == "ready"`、artifact 内容等于假 Markdown、`backend == "online_agent"`。
  - `test_partial_result_is_ready_with_warnings`：假结果带 `warnings=["第 21–40 页解析失败，未包含"]` → `ready` 且 `warnings_json` 等于该列表。
  - `test_offline_not_ready_never_falls_back`：模式 offline、installer 状态 `not_installed` → `failed`，`error_json == {"code": "DOCPARSE_OFFLINE_NOT_READY"}`，假在线函数调用次数为 0。（Review Focus 3）
  - `test_mode_locked_at_enqueue`：以 online 入队后把设置改成 offline，再处理 → 调用的是在线函数；之后新入队走离线。（Review Focus 5）
  - `test_expired_lease_requeued_on_start`：手动把任务置为 running 且 `lease_until` 过去 → `start_docparse_worker` 启动后任务被处理完成，论文不再停留 `parsing`。（Review Focus 4）
  - `test_retry_passes_previous_markdown`：已有部分成功的 ready 记录 → 再入队处理时假在线函数收到的 `previous_markdown` 等于该 Markdown。
  - `test_heartbeat_extends_lease`：假解析阻塞 45 s（用可控时钟/事件模拟）期间 `heartbeat` 至少被调用一次。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/phase02/test_docparse_jobs.py -v`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过**；`.venv/bin/python -m pytest tests/unit/test_architecture.py -v` PASS。
- [ ] **Step 5: 提交** — `feat(orchestration): paper_parse job worker with mode lock and lease heartbeat`

### Task 8: API 路由、上传页数修正、运行时接线

**Files:**
- Create: `services/api/p2r_api/routes/docparse.py`
- Modify: `services/api/p2r_api/routes/__init__.py`（注册 router）, `services/api/p2r_api/routes/papers.py:25,107-121`（页数改 `count_pages`；PDF 上传成功且已设模式时自动 `enqueue_paper_parse`）, `services/api/p2r_api/dependencies.py:15`（`build_runtime` 增加 `runtime["docparse_service"]`）, `services/api/p2r_api/main.py:16-38,81-114`（`create_demo_app` 安装 docparse worker lifespan）
- Test: `tests/integration/phase02/test_docparse_http.py`

**Interfaces:**
- Consumes: Task 5 `OfflineInstaller`、`DocParseSettings`；Task 7 全部。
- Produces（均在 `/api/v1`，响应用现有 envelope，错误用现有 `_error` 风格）：
  - `docparse_root() -> Path`（`dependencies.py`）：`P2R_DOCPARSE_DIR`，否则 `Path(os.environ.get("P2R_DEMO_DIR", ".p2r-demo")) / "docparse"`。
  - demo lifespan 启动 worker 后，若 `installer.state().state == "ready"`，在后台线程调用一次 `installer.ensure_healthy()`（应用被移动/升级后的自愈；不阻塞启动）。
  - `GET /docparse/status` → `{"mode": null|"online"|"offline", "offline": InstallState.to_json(), "platform": str}`。
  - `PUT /docparse/mode` body `{"mode": "online"|"offline"}` → 同 status。
  - `POST /docparse/offline/install` → 202 + status；不支持平台 409 `DOCPARSE_OFFLINE_UNSUPPORTED`。
  - `POST /docparse/offline/cancel`、`DELETE /docparse/offline` → status。
  - `POST /papers/{paper_id}/parse`（需 `Idempotency-Key`）→ 202 `{"paper_id", "job_id", "status": "parsing"}`；未设模式 409 `DOCPARSE_MODE_UNSET`；非 PDF 409 `PAPER_NOT_PDF`。
  - `GET /papers/{paper_id}/text` → `{"status": "none"|"parsing"|"ready"|"failed", "backend", "page_count", "pages_parsed", "chunks", "warnings", "error_code", "preview"}`（`preview` 为 Markdown 前 4000 字符，仅 ready 时有）。

- [ ] **Step 1: 写失败测试**（照 `test_extraction_provider.py` 的 `create_app` + 注入 session_factory/artifact_store 方式；`docparse_service` 注入假实现）：
  - `test_compressed_pdf_upload_counts_pages`：`minimal_pdf(["a","b","c"], object_streams=True)`（先断言原 `_PAGE_RE.findall` 在该字节上为空）上传 → 202、`page_count == 3`。
  - `test_invalid_pdf_upload`：上传加密 PDF → 422 `PDF_INVALID`。（Review Focus 1）
  - `test_upload_autoenqueues_when_mode_set`：先 `PUT /docparse/mode online`，上传 → `papers.status == "parsing"` 且存在 `paper_parse` 任务。
  - `test_upload_waits_when_mode_unset`：未设模式上传 → `status == "uploaded"`，无任务；`POST /papers/{id}/parse` → 409 `DOCPARSE_MODE_UNSET`。
  - `test_status_and_mode_roundtrip`、`test_install_unsupported_platform_409`（注入 `darwin-x64`）。
  - `test_text_endpoint_states`：none → parsing → ready（含 preview）逐一断言。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/integration/phase02/test_docparse_http.py -v`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过**；`.venv/bin/python -m pytest -q` 全量无回归。
- [ ] **Step 5: 提交** — `feat(api): docparse routes, PDF page count via pypdf, parse worker wiring`

### Task 9: 抽取改读解析文本

**Files:**
- Modify: `services/api/p2r_api/routes/papers.py:403-425`, `services/api/p2r_api/extraction_payload.py:24-27`, `packages/orchestration/p2r_orchestration/extraction_service.py:24-48,202-220,280-289`
- Test: `tests/integration/phase02/test_extraction_parsed_text.py`, `tests/unit/phase02/test_extraction_service.py`（追加用例）

**Interfaces:**
- Consumes: Task 2 `trim_for_context`；Task 6 `PaperTextRecord`。
- Produces:
  - Provider 抽取分支：PDF 论文取 `paper_id` 最新 `status="ready"` 且 `source_sha256 == paper.sha256` 的 `PaperTextRecord`，以其 Markdown 字节作为 `content`；无 → 409 `PAPER_TEXT_NOT_READY`。路由在 `build_extraction_payload` 前向 projection 加 `source_text_sha256`、`parse_backend`，并把 `PaperTextRecord.warnings_json` 追加到 `extraction["warnings"]`。
  - `PROJECTION_METADATA_KEYS` 追加 `"source_text_sha256", "parse_backend"`。
  - `ExtractionService.extract` 用 `trim_for_context(text, self._max_context_bytes)` 取代直接切片，警告追加进 projection 的 `extraction["warnings"]`；`_SYSTEM_PROMPT` 末尾追加逐字句：`"Lines like <!-- p2r:page 7 --> or <!-- p2r:pages 21-40 --> mark the PDF page(s) of the text that follows; set source_ref.page to the first page of the marked span."`

- [ ] **Step 1: 写失败测试**：
  - `test_pdf_without_text_409`：PDF 论文无 ready 文本 → 409 `PAPER_TEXT_NOT_READY`。
  - `test_pdf_with_text_sends_markdown`：假 provider 捕获用户消息 → 含 `<!-- p2r:pages 1-20 -->`，不含 `%PDF`；持久化 payload 的 `provider.source_text_sha256` 等于记录值、`provider.parse_backend == "online_agent"`；`source_refs[*].pdf_sha256` 仍为 PDF sha。
  - `test_parse_warnings_propagate`：记录 `warnings_json=["第 21–40 页解析失败，未包含"]` → payload `warnings` 含该条。
  - `test_trim_warning_in_projection`（单元）：超长 Markdown → projection `extraction["warnings"]` 含 `"仅第 1–20 页发送给模型（超出上下文上限）"`。
  - `test_text_paper_path_unchanged`：文本论文抽取行为与之前一致（复用现有断言）。
- [ ] **Step 2: 运行确认失败**。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过**；`.venv/bin/python -m pytest -q` 全量无回归。
- [ ] **Step 5: 提交** — `feat(extraction): feed parsed Markdown to provider extraction with page markers`

### Task 10: 桌面运行时保留 pip

**Files:**
- Modify: `scripts/desktop/build_runtime.py:114`（`PRUNE_SITE` 去掉 `"pip"`）, `scripts/desktop/smoke_runtime.py`（增加 `-m pip --version` 成功的检查）
- Test: `tests/unit/phase01/test_desktop_runtime_prune.py`

- [ ] **Step 1: 写失败测试**：`test_pip_not_pruned`（导入 `build_runtime`，断言 `"pip" not in PRUNE_SITE` 且 `"ensurepip" in PRUNE_STDLIB`）。
- [ ] **Step 2: 运行确认失败** — `.venv/bin/python -m pytest tests/unit/phase01/test_desktop_runtime_prune.py -v`。
- [ ] **Step 3: 修改两处脚本**。
- [ ] **Step 4: 运行通过**；本机 `python scripts/desktop/build_desktop.py --skip-web --dir` 后 `smoke_runtime.py` 通过（输出含 pip 版本）。
- [ ] **Step 5: 提交** — `build(desktop): keep pip in bundled runtime for on-demand MinerU install`

### Task 11: 前端 API 客户端与 Guided 状态逻辑

**Files:**
- Modify: `apps/web/src/api-client/guided.ts`（`GUIDED_PATHS` 与 `createGuidedApi`）, `apps/web/src/features/guided/realflow.ts`, `apps/web/src/features/guided/realflow-state.ts`
- Create: `apps/web/src/features/docparse/docparse.ts`（纯函数：状态 → 中文标签、是否可抽取）
- Test: `apps/web/tests/docparse-api.test.mjs`, `apps/web/tests/docparse-realflow.test.mjs`

**Interfaces:**
- Consumes: Task 8 全部端点与 DTO。
- Produces:
  - 类型 `DocParseStatus`、`PaperText`；校验器 `validateDocParseStatus`、`validatePaperText`。
  - API 方法：`getDocParseStatus(signal?)`、`setDocParseMode(mode)`、`startOfflineInstall()`、`cancelOfflineInstall()`、`uninstallOffline()`、`parsePaper(paperId, key)`、`getPaperText(paperId, signal?)`。
  - `RealFlowController` 新方法：`chooseParseMode(mode: 'online'|'offline')`（设模式；离线则开始安装；安装就绪或在线时调用 `parsePaper`）、`pollPaperText()`、`retryParse()`。状态新增 `docparse: { needsChoice: boolean; status: DocParseStatus | null; text: PaperText | null }`。
  - `canExtract(state) -> boolean`：文本论文恒 true；PDF 论文仅当 `text.status === 'ready'`。

- [ ] **Step 1: 写失败测试**：
  - api：各方法的路径、方法、`Idempotency-Key`（仅 `parsePaper`）、`X-Actor-ID`、请求体只含允许字段；`validatePaperText` 拒绝未知 `status`。
  - realflow：PDF 上传后 `mode=null` → `needsChoice === true` 且未调用 `parsePaper`；`chooseParseMode('online')` → 调用 `setDocParseMode` 与 `parsePaper`；`text.status==='parsing'` 时 `canExtract` 为 false，`ready` 时为 true；`chooseParseMode('offline')` 在安装未就绪时不调用 `parsePaper`、不调用在线相关方法。
- [ ] **Step 2: 运行确认失败** — `pnpm --dir apps/web test`。
- [ ] **Step 3: 实现**。
- [ ] **Step 4: 运行通过** — `pnpm --dir apps/web test && pnpm --dir apps/web typecheck`。
- [ ] **Step 5: 提交** — `feat(web): docparse API client and guided parse-mode state`

### Task 12: 前端界面：选择弹窗、解析进度、设置分组

**Files:**
- Create: `apps/web/src/features/docparse/ModeDialog.tsx`, `apps/web/src/features/docparse/ParseProgress.tsx`, `apps/web/src/features/docparse/DocParseSettings.tsx`, `apps/web/src/features/docparse/docparse.css`
- Modify: `apps/web/src/features/guided/RealFlowPanel.tsx`（第 1 步渲染弹窗与进度，抽取按钮用 `canExtract`）, `apps/web/src/features/settings/Settings.tsx`（「本地服务」之后插入 `<DocParseSettings />`）

**Interfaces:**
- Consumes: Task 11 控制器方法与 `canExtract`、`docparse.ts` 标签函数。
- 固定文案（逐字）：
  - 在线卡片标题「在线解析」，说明「无需安装，马上可用。论文会上传到 MinerU 官方服务器（mineru.net）解析；超过 20 页会自动分段。」
  - 离线卡片标题「离线解析」，说明「需下载约 1–3 GB（视本机硬件），之后完全在本机解析，不会上传论文。CPU 上解析较慢。」
  - 平台不支持时离线卡片不渲染。
  - 失败时按钮「重试」，以及「改用离线解析」/「改用在线解析」（仅在另一模式可用时出现，点击才切换）。
  - 设置分组标题「PDF 解析」，行：「当前方式」（Segmented 在线/离线）、「离线组件」（状态 + 档位 + 占用空间 + 安装/取消/卸载按钮）。

- [ ] **Step 1: 实现组件**，样式沿用现有 `ui-*` 类与 `settings-*` 结构。
- [ ] **Step 2: 类型检查与构建** — `pnpm --dir apps/web typecheck && pnpm --dir apps/web build`，均成功。
- [ ] **Step 3: 本机界面走查** — 用 `run` skill 启动 demo（`P2R_DEMO_DIR` 指向临时目录），浏览器中：上传 PDF → 弹窗出现 → 选在线 → 进度显示 → 完成后抽取按钮可用；设置页出现「PDF 解析」分组。截图存档。
- [ ] **Step 4: 提交** — `feat(web): parse-mode dialog, parse progress and PDF parsing settings`

### Task 13: 真实端到端验证与验证记录

**Files:**
- Create: `tools/docparse_e2e.py`（命令行：`--online <pdf>`、`--offline-install <root>`、`--offline <pdf> --root <root>`；输出一行 JSON：`page_count`、`pages_parsed`、`chunks`、`warnings`、`markdown_sha256`、耗时）, `docs/verification/2026-11-02-mineru-pdf-parsing.md`
- Modify: `docs/README.md`（索引加验证记录）, 如实机发现差异则修 `p2r_docparse/install.py` 的 `MINERU_VERSION`/档位字段名、`offline.py` 的输出格式解析（各自补测试）

- [ ] **Step 1: 准备测试论文** — 下载一篇 >20 页的真实 arXiv PDF（默认 ViT，`https://arxiv.org/pdf/2010.11929v2`，22 页）到新建空目录 `~/p2r-e2e/paper/`，记录 sha256。
- [ ] **Step 2: Mac 在线** — `.venv/bin/python -I tools/docparse_e2e.py --online ~/p2r-e2e/paper/2010.11929v2.pdf`；预期 `page_count == 22`、两段、`pages_parsed == 22`、无警告。
- [ ] **Step 3: Mac 离线** — `--offline-install ~/p2r-e2e/docparse`，预期 `ready`、档位与 `hardware detect` 一致；断网（关闭 Wi‑Fi 由用户确认）后 `--offline … --root ~/p2r-e2e/docparse` 成功。核对 `mineru-kit parse` 实际输出目录结构，确认 Task 4 的页级解析逻辑命中；不命中则修正并补测试。
- [ ] **Step 4: Mac 全链路** — 用 `run` skill 启动桌面开发版，界面走完：上传 ViT PDF → 在线解析 → 抽取 → 六步至导出；再把模式切到离线重复一次解析与抽取。
- [ ] **Step 5: Windows 实机（`win`，tailscale `100.90.199.48`，`ssh root@…` 进 WSL）** — 先验证互操作：`/mnt/c/Windows/System32/cmd.exe /c ver` 能输出 Windows 版本（不行则停下，请用户在 `win` 桌面打开 WSL 终端或改用 Windows OpenSSH）。在 `C:\p2r-e2e\` 独立目录放置 `win32-x64` 内置运行时（由 `build_desktop.py --target win32-x64 --dir` 产出，或同版本 Windows CPython 3.12 + 本仓库 wheel），通过 Windows 原生 `python.exe` 依次运行 Step 2、Step 3（含 `hardware detect` 档位记录）与 `--offline` 解析；另跑一次 `timeout=5` 的解析，用 `tasklist` 确认无残留 `mineru-kit`/`python` 子进程。不修改该机器全局 Python 与系统配置。
- [ ] **Step 6: 写验证记录** — 逐项记录命令、输出摘要、档位、耗时、截图路径与未覆盖范围（Windows 界面点击、Intel Mac 不支持）。
- [ ] **Step 7: 全量回归** — `.venv/bin/python -m pytest -q`、`pnpm --dir apps/web test`、`pnpm --dir apps/web typecheck` 全部通过。
- [ ] **Step 8: 提交** — `docs(verification): MinerU PDF parsing end-to-end on macOS and Windows`
