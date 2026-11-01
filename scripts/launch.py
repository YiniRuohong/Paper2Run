"""One-click launcher for the out-of-box Paper2Run app.

Called by ``启动Paper2Run.command`` (macOS) and ``启动Paper2Run.bat``
(Windows). Bootstraps everything a fresh checkout needs and then serves the
API and the built web UI from a single local port:

1. ensure ``.venv`` exists (created from the running interpreter) and the
   ``paper2run`` package is installed into it;
2. ensure ``apps/web/dist`` exists (build with npm when available);
3. pick a free loopback port starting at 8010;
4. start uvicorn (``create_demo_app``) with a persistent demo dir;
5. open the default browser once the health check passes.

Prerequisites: Python 3.11+. Node.js/npm is only needed when the prebuilt
frontend bundle is missing.
"""
from __future__ import annotations

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
DEMO_DIR = ROOT / ".p2r-app"
BASE_PORT = 8010
# Python source roots (keep in sync with pyproject.toml [tool.pytest.ini_options]).
# Putting them on PYTHONPATH means a stale editable install can never shadow them.
SOURCE_ROOTS = ("services/api", "packages/contracts", "packages/artifacts", "packages/persistence",
                "packages/sandbox", "packages/evaluation", "packages/codegen", "packages/runner",
                "packages/kernel", "packages/orchestration")

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
    result = subprocess.run(cmd, cwd=ROOT, **kwargs)
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
        [str(python), "-c", "import p2r_api, p2r_runner, p2r_kernel, uvicorn"],
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
    port = free_port(BASE_PORT)
    url = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env.update({
        "P2R_DEMO_DIR": str(DEMO_DIR),
        "P2R_WEB_DIST": str(DIST_INDEX.parent),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": source_pythonpath(),
    })
    message(f"[3/3] 启动服务 / starting server at {url} （数据目录 .p2r-app）")
    message("    停止 / stop: 关闭本窗口或按 Ctrl+C")
    server = subprocess.Popen(
        [str(python), "-m", "uvicorn", "p2r_api.main:create_demo_app", "--factory",
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
