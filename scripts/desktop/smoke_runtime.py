"""Smoke-test a Paper2Run desktop runtime the way the Electron app uses it.

Starts ``serve.py`` with the given interpreter exactly like ``main.cjs`` does
(``-I -B -X utf8``, scrubbed ``PYTHON*`` env, stdin as a pipe, throwaway
``P2R_DEMO_DIR``), then checks:

1. the ``{"event": "listening", "port": N}`` handshake line arrives;
2. ``GET /healthz`` answers ``{"status": "ok"}``;
3. ``GET /api/v1/demo`` (the first call the UI makes) answers 200 JSON;
4. ``GET /`` serves the web UI when ``--web-dist`` is given;
5. closing stdin (= the desktop app dying) makes the backend exit on its
   own within the watchdog grace period.

Standard library only; used by ``build_runtime.py`` and CI.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVE = ROOT / "apps" / "desktop" / "backend" / "serve.py"


def clean_env(data_dir: str, web_dist: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "P2R_DEMO_DIR": data_dir})
    if web_dist:
        env["P2R_WEB_DIST"] = web_dist
    return env


def get(url: str, timeout: float = 5.0) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, response.headers.get("Content-Type", ""), response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--python", required=True, help="interpreter to test (bundled runtime)")
    parser.add_argument("--serve", default=str(DEFAULT_SERVE), help="path to serve.py")
    parser.add_argument("--web-dist", default=None, help="built web UI dir to serve (optional)")
    parser.add_argument("--timeout", type=float, default=120.0, help="startup timeout in seconds")
    args = parser.parse_args(argv)
    # The backend runs with cwd = a temp data dir, so relative paths must be resolved first.
    args.python = os.path.abspath(args.python)
    args.serve = os.path.abspath(args.serve)
    if args.web_dist:
        args.web_dist = os.path.abspath(args.web_dist)

    data_dir = tempfile.mkdtemp(prefix="p2r-smoke-")
    cmd = [args.python, "-I", "-B", "-X", "utf8", args.serve]
    print(f"[smoke] $ {' '.join(cmd)}  (P2R_DEMO_DIR={data_dir})", flush=True)
    proc = subprocess.Popen(cmd, cwd=data_dir, env=clean_env(data_dir, args.web_dist),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            print(f"[backend] {line}", flush=True)
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    failures: list[str] = []
    try:
        port = None
        deadline = time.monotonic() + args.timeout
        while port is None and time.monotonic() < deadline:
            if proc.poll() is not None:
                raise SystemExit(f"[smoke] FAIL backend exited early with code {proc.returncode}")
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                continue
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "listening":
                    port = int(event["port"])
        if port is None:
            raise SystemExit("[smoke] FAIL no listening handshake before timeout")
        base = f"http://127.0.0.1:{port}"
        print(f"[smoke] handshake ok: {base}", flush=True)

        health = None
        while time.monotonic() < deadline:
            try:
                status, _, body = get(f"{base}/healthz", timeout=2)
                health = json.loads(body)
                if status == 200 and health.get("status") == "ok":
                    break
            except OSError:
                pass
            time.sleep(0.25)
        if not health or health.get("status") != "ok":
            raise SystemExit("[smoke] FAIL /healthz never became ok")
        print(f"[smoke] GET /healthz -> {health}", flush=True)

        status, ctype, body = get(f"{base}/api/v1/demo")
        print(f"[smoke] GET /api/v1/demo -> {status} {ctype} {body[:160]!r}", flush=True)
        if status != 200 or "json" not in ctype:
            failures.append("/api/v1/demo did not return 200 JSON")

        if args.web_dist:
            status, ctype, body = get(f"{base}/")
            ok = status == 200 and b'id="root"' in body
            print(f"[smoke] GET / -> {status} {ctype} ui={'ok' if ok else 'MISSING'}", flush=True)
            if not ok:
                failures.append("/ did not serve the web UI")

        # Watchdog: closing stdin is what the OS does when Electron dies.
        assert proc.stdin is not None
        proc.stdin.close()
        started = time.monotonic()
        try:
            code = proc.wait(timeout=15)
            print(f"[smoke] stdin closed -> backend exited (code {code}) after "
                  f"{time.monotonic() - started:.1f}s", flush=True)
        except subprocess.TimeoutExpired:
            failures.append("backend did not exit after stdin EOF (watchdog)")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    if failures:
        for failure in failures:
            print(f"[smoke] FAIL {failure}", flush=True)
        return 1
    print("[smoke] PASS", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
