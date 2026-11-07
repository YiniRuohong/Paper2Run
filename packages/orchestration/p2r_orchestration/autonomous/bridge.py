"""Subprocess transport only: Pi SDK owns the model/tool loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_PASSED = ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "LANG")
# Node on Windows needs a temp dir (TEMP/TMP) and reads its home from USERPROFILE.
_PASSED_WINDOWS = ("TEMP", "TMP")


def _windows() -> bool:
    return os.name == "nt"


def harness_environment(host: dict, session_dir, *, windows: bool) -> dict:
    """No provider credentials, NODE_OPTIONS, SSH_AUTH_SOCK, or global SDK settings.

    The home directory is per session. On Windows USERPROFILE/APPDATA/LOCALAPPDATA
    point into it too, so the SDK never reads the user's real profile.
    """
    names = _PASSED + (_PASSED_WINDOWS if windows else ())
    environment = {k: host[k] for k in names if k in host}
    home = Path(session_dir) / ".home"
    environment["HOME"] = str(home)
    if windows:
        environment.update(USERPROFILE=str(home), APPDATA=str(home / "AppData" / "Roaming"),
                           LOCALAPPDATA=str(home / "AppData" / "Local"))
    home.mkdir(parents=True, exist_ok=True)
    return environment


class HarnessBridge:
    def __init__(self):
        self.process = None
        self.write_lock = threading.Lock()
        self.aborted = threading.Event()

    def send(self, value):
        with self.write_lock:
            if self.process and self.process.poll() is None:
                try:
                    self.process.stdin.write(json.dumps(value, allow_nan=False) + "\n")
                    self.process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    pass

    def abort(self):
        self.aborted.set()
        self.send({"type": "abort"})

    def steer(self, message):
        self.send({"type": "steer", "message": message})

    def run(self, config, prompt, callback):
        if self.aborted.is_set():
            return
        root = Path(os.environ.get("P2R_HARNESS_DIR", Path(__file__).resolve().parents[4] / "apps" / "harness"))
        # An absolute P2R_NODE_BINARY (node.exe on Windows) is used as given.
        node = os.environ.get("P2R_NODE_BINARY", "node")
        windows = _windows()
        environment = harness_environment(os.environ, config["session_dir"], windows=windows)
        # Node writes UTF-8; never decode JSONL with a Windows ANSI code page.
        options = {"creationflags": CREATE_NO_WINDOW} if windows else {}
        self.process = subprocess.Popen([node, str(root / "bridge.mjs")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8", bufsize=1, env=environment, **options)
        messages = queue.Queue(maxsize=128)
        def reader():
            try:
                while True:
                    line = self.process.stdout.readline(8 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 8 * 1024 * 1024:
                        raise ValueError("SDK record exceeds limit")
                    messages.put(json.loads(line), timeout=10)
            except Exception:
                try:
                    messages.put({"type": "error", "code": "bridge_protocol_error", "message": "invalid SDK output"}, timeout=1)
                except queue.Full:
                    pass
            finally:
                try:
                    messages.put(None, timeout=1)
                except queue.Full:
                    pass
        threading.Thread(target=reader, daemon=True, name="autonomous-sdk-output").start()
        self.send({"type": "start", "config": config, "prompt": prompt})
        wall = (config.get("limits") or {}).get("wall_time_seconds")  # None: no wall-time limit
        deadline = float("inf") if wall is None else time.monotonic() + wall
        terminal, abort_at = False, None
        try:
            while True:
                if self.aborted.is_set() or time.monotonic() >= deadline:
                    if abort_at is None:
                        self.abort()
                        abort_at = time.monotonic()
                    if time.monotonic() - abort_at > 3:
                        break
                try:
                    message = messages.get(timeout=.2)
                except queue.Empty:
                    if self.process.poll() is not None:
                        break
                    continue
                if message is None:
                    break
                if message.get("type") == "tool_request" and self.aborted.is_set():
                    continue
                response = callback(message)
                if response is not None:
                    self.send(response)
                if message.get("type") in ("result", "error"):
                    terminal = True
                    break
            if not terminal and not self.aborted.is_set():
                callback({"type": "error", "code": "bridge_disconnected", "message": "SDK ended without a result"})
            if time.monotonic() >= deadline:
                callback({"type": "error", "code": "wall_time_exceeded", "message": "wall-time budget exhausted"})
        finally:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for stream in (self.process.stdin, self.process.stdout):
                stream.close()
