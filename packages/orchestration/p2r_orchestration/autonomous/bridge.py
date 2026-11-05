"""Subprocess transport only: Pi SDK owns the model/tool loop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


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
        node = os.environ.get("P2R_NODE_BINARY", "node")
        # No provider credentials, NODE_OPTIONS, SSH_AUTH_SOCK, or global SDK settings.
        environment = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "WINDIR", "TMPDIR", "LANG") if k in os.environ}
        environment["HOME"] = str(Path(config["session_dir"]) / ".home")
        Path(environment["HOME"]).mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen([node, str(root / "bridge.mjs")], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1, env=environment)
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
        deadline = time.monotonic() + config["limits"]["wall_time_seconds"]
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
