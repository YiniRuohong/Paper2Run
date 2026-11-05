"""One-click launcher for the out-of-box Paper2Run app.

Called by ``启动Paper2Run.command`` (macOS) and ``启动Paper2Run.bat``
(Windows). Bootstraps everything a fresh checkout needs and then serves the
API and the built web UI from a single local port:

1. ensure ``.venv`` exists (created from the running interpreter) and the
   ``paper2run`` package is installed into it;
2. ensure ``apps/web/dist`` exists (build with npm when available);
3. pick a free loopback port starting at 8010;
4. start uvicorn (``create_local_app``) with a persistent data dir (``.p2r-app``);
5. open the default browser once the health check passes.

Prerequisites: Python 3.11+. Node.js/npm is needed when the prebuilt
frontend bundle is missing; autonomous mode additionally needs Node >=22.19
and the Harness dependencies. Docker is needed to run experiments; without it
the app still starts but experiments cannot run.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_DIR = ROOT / ".venv"
DIST_INDEX = ROOT / "apps" / "web" / "dist" / "index.html"
DATA_DIR = ROOT / ".p2r-app"
BASE_PORT = 8010
# Python source roots (keep in sync with pyproject.toml [tool.pytest.ini_options]).
# Putting them on PYTHONPATH means a stale editable install can never shadow them.
SOURCE_ROOTS = ("services/api", "packages/contracts", "packages/artifacts", "packages/persistence",
                "packages/sandbox", "packages/docparse", "packages/evaluation", "packages/orchestration")

WINDOWS = sys.platform == "win32"


def message(text: str) -> None:
    print(text, flush=True)


def source_pythonpath() -> str:
    return os.pathsep.join([str(ROOT)] + [str(ROOT / r) for r in SOURCE_ROOTS])


def venv_python() -> Path:
    if WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(cmd: list[str], **kwargs) -> None:
    message(f"$ {' '.join(str(part) for part in cmd)}")
    kwargs.setdefault("cwd", ROOT)
    result = subprocess.run(cmd, **kwargs)
    if result.returncode != 0:
        message(f"[!] 命令失败 (exit {result.returncode}) / command failed: {' '.join(str(part) for part in cmd)}")
        raise SystemExit(result.returncode)


def ensure_venv() -> Path:
    if not VENV_DIR.is_dir():
        message("[1/3] 首次运行：创建虚拟环境 / creating virtualenv ...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)])
    python = venv_python()
    if not python.is_file():
        message(f"[!] 虚拟环境损坏（缺 {python}）；删除 .venv 目录后重试 / broken venv, remove .venv and retry")
        raise SystemExit(1)
    probe = subprocess.run(
        [str(python), "-c", "import p2r_api.main, uvicorn"],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": source_pythonpath()},
    )
    if probe.returncode != 0:
        message("[1/3] 安装后端依赖 / installing backend dependencies ...")
        run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "-e", str(ROOT)])
    return python


def ensure_frontend() -> None:
    if DIST_INDEX.is_file():
        return
    npm = shutil.which("npm")
    if npm is None:
        message("[!] 缺少前端构建产物 apps/web/dist，且未找到 npm。")
        message("    请安装 Node.js 后重试，或在一台有 Node 的机器上执行：")
        message("    cd apps/web && npm install && npm run build")
        raise SystemExit(1)
    message("[2/3] 构建前端 / building web UI ...")
    npm_dir = ROOT / "apps" / "web"
    if not (npm_dir / "node_modules").is_dir():
        run([npm, "install", "--no-fund", "--no-audit"], cwd=npm_dir)
    run([npm, "run", "build"], cwd=npm_dir)


def ensure_harness() -> dict[str, str]:
    """Prepare optional autonomous mode; old workflows remain usable on failure."""
    sys.path.insert(0, str(ROOT / "scripts" / "desktop"))
    import build_harness

    harness = Path(os.environ.get("P2R_HARNESS_DIR", ROOT / "apps/harness")).resolve()
    skills = Path(os.environ.get("P2R_SKILLS_DIR", ROOT / "resources/research-skills")).resolve()
    bundled_node, bundled_npm = build_harness.node_paths(
        ROOT / "apps/desktop/.runtime" / build_harness.host_target() / "node", build_harness.host_target())
    candidate = os.environ.get("P2R_NODE_BINARY") or (str(bundled_node) if bundled_node.is_file() else shutil.which("node"))
    # An explicit absent path stops the backend from silently finding an old Node.
    env = {"P2R_NODE_BINARY": str(Path(candidate).resolve()) if candidate else str(bundled_node),
           "P2R_HARNESS_DIR": str(harness), "P2R_SKILLS_DIR": str(skills)}
    if WINDOWS:
        message("[!] 自主模式在 Windows 暂不可用：技能注册表需要 POSIX flock/no-follow；旧流程继续启动。")
        return env
    failure_hint = "找不到可运行的 Node >=22.19；请安装完整 Node/npm"
    try:
        if not candidate:
            raise RuntimeError("missing Node")
        node = Path(candidate).resolve()
        version = build_harness.check_node(node)
        failure_hint = "apps/harness/package.json 或 package-lock.json 缺失或不可读"
        lock = harness / "package-lock.json"
        fingerprint = hashlib.sha256(lock.read_bytes() + (harness / "package.json").read_bytes() + version.encode() + build_harness.host_target().encode()).hexdigest()
        marker = harness / "node_modules/.p2r-install.json"
        try:
            installed = json.loads(marker.read_text()).get("fingerprint") == fingerprint
        except (OSError, ValueError):
            installed = False
        if installed:
            try:
                build_harness.smoke(node, harness)
            except (RuntimeError, OSError, subprocess.SubprocessError):
                installed = False
        if not installed:
            failure_hint = "未找到与 Node 一起安装的 npm CLI；请安装完整 Node/npm"
            npm = bundled_npm if node == bundled_node.resolve() else None
            if npm is None:
                executable = shutil.which("npm")
                resolved = Path(executable).resolve() if executable else None
                candidates = [node.parent / "node_modules/npm/bin/npm-cli.js", node.parent.parent / "lib/node_modules/npm/bin/npm-cli.js"]
                if resolved and resolved.suffix == ".js":
                    candidates.insert(0, resolved)
                npm = next((p for p in candidates if p.is_file()), None)
            if npm is None:
                raise RuntimeError("未找到 npm CLI；请安装完整 Node/npm，或运行 scripts/desktop/build_harness.py")
            message("[自主模式] 安装锁定的 Harness 生产依赖 / npm ci ...")
            failure_hint = "Harness npm ci 失败；请检查网络、锁文件和本机原生依赖构建条件"
            build_harness.install_dependencies(node, npm, harness)
            failure_hint = "Harness SDK/bridge 自检失败；请检查 bridge 与锁定 SDK 的兼容性"
            build_harness.smoke(node, harness)
            failure_hint = "无法保存 Harness 依赖缓存标记；请检查目录写入权限"
            marker.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
        failure_hint = "resources/research-skills 中缺少预装的八个科研技能"
        if len(list(skills.glob("*/SKILL.md"))) != 8:
            raise RuntimeError("科研 skills 不完整，需要 resources/research-skills 中的八个技能")
        message(f"[自主模式] Node {version} 与 Harness 已就绪")
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Do not print exception output: npm/Node can echo user credentials.
        message(f"[!] 自主模式未就绪：{failure_hint}。")
        message("    可运行 python scripts/desktop/build_harness.py 修复桌面运行时；服务仍会启动，但无法开始实验。")
    try:
        probe = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=10)
        docker_ready = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        docker_ready = False
    if not docker_ready:
        message("[!] Docker 未就绪：本地自主实验暂不可用；启动 Docker 后可重试。")
    return env


def free_port(base: int) -> int:
    for port in range(base, base + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise SystemExit("[!] 8010-8029 端口均被占用 / no free port in 8010-8029")


def main() -> None:
    if sys.version_info < (3, 11):
        message(f"[!] 需要 Python 3.11+，当前为 {sys.version.split()[0]} / Python 3.11+ required")
        raise SystemExit(1)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    message("Paper2Run · 论文复现工作台（开箱即用）")
    python = ensure_venv()
    ensure_frontend()
    harness_env = ensure_harness()
    port = free_port(BASE_PORT)
    url = f"http://127.0.0.1:{port}"

    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("ELECTRON_") and k.upper() not in ("NODE_OPTIONS", "NODE_PATH")}
    env.update(harness_env)
    env.update({
        "P2R_DEMO_DIR": str(DATA_DIR),  # historical variable name; it is the data dir
        "P2R_WEB_DIST": str(DIST_INDEX.parent),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": source_pythonpath(),
    })
    message(f"[3/3] 启动服务 / starting server at {url} （数据目录 .p2r-app）")
    message("    停止 / stop: 关闭本窗口或按 Ctrl+C")
    server = subprocess.Popen(
        [str(python), "-m", "uvicorn", "p2r_api.main:create_local_app", "--factory",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT, env=env,
    )
    try:
        for _ in range(120):
            if server.poll() is not None:
                message("[!] 服务进程提前退出 / server exited early")
                raise SystemExit(server.returncode or 1)
            try:
                with urllib.request.urlopen(f"{url}/healthz", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            message("[!] 健康检查超时 / health check timed out")
            server.terminate()
            raise SystemExit(1)
        message(f"✓ 已就绪 / ready: {url} （浏览器将自动打开）")
        webbrowser.open(url)
        server.wait()
    except KeyboardInterrupt:
        message("\n正在停止 / stopping ...")
    finally:
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()


if __name__ == "__main__":
    main()
