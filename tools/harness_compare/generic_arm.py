"""Generic harness arm: the same Pi SDK agent loop, model and Docker image as Paper2Run, but only the
four tools a general coding-agent harness gives (bash/read/write/edit). No protocol freezing, no evidence
gate, no program-side evaluation, no independent review, no repair loop: the agent's own final message is
the result. This is the "sandbox + Pi wrapper" baseline.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import subprocess
import time
from uuid import uuid4

from p2r_orchestration.autonomous.bridge import HarnessBridge

IMAGE = "paper2run-experiment:1"
MAX_OUTPUT = 30000
TRANSIENT = {"model_error", "sdk_error", "bridge_disconnected", "bridge_protocol_error", "stdin_eof"}

SYSTEM_PROMPT = """You are an expert coding assistant working inside a Linux Docker container. Your working directory is /work.
Tools: bash (run a shell command in the container), read (read a file), write (create/overwrite a file), edit (replace exact
text in a file). Use the tools to actually complete the user's task end to end; do not merely describe what you would do.
When you are done, reply with a concise final report of what you did and the results."""


def _tool(name, description, properties, required):
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False}}


TOOLS = [
    _tool("bash", "Run a bash command in /work inside the container. Returns exit code and combined output (tail). Commands are killed after timeout seconds (default and max 280); run longer jobs in the background with nohup and poll.",
          {"command": {"type": "string"}, "timeout": {"type": "integer", "minimum": 1, "maximum": 280}}, ["command"]),
    _tool("read", "Read a text file. Optional 1-based line offset and line limit.",
          {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
           "limit": {"type": "integer", "minimum": 1}}, ["path"]),
    _tool("write", "Create or overwrite a file with the given content.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _tool("edit", "Replace one exact occurrence of old_text with new_text in a file.",
          {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}},
          ["path", "old_text", "new_text"]),
]


class Sandbox:
    def __init__(self, workspace: Path, name: str):
        self.workspace, self.name = workspace.resolve(), name

    def __enter__(self):
        subprocess.run(["docker", "run", "-d", "--name", self.name, "--label", "paper2run.harness-compare=1",
                        "--mount", f"type=bind,src={self.workspace},dst=/work", "-w", "/work",
                        IMAGE, "sleep", "infinity"], check=True, capture_output=True)
        return self

    def __exit__(self, *exc):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)

    def host_path(self, path: str) -> Path:
        pure = PurePosixPath(path)
        if pure.is_absolute():
            pure = pure.relative_to("/work") if pure.is_relative_to("/work") else None
        if pure is None or ".." in pure.parts:
            raise ValueError("path must stay inside /work")
        return self.workspace / pure

    def bash(self, command, timeout=280):
        try:
            done = subprocess.run(["docker", "exec", "-w", "/work", self.name, "bash", "-lc", command],
                                  capture_output=True, timeout=min(int(timeout), 280))  # the bridge aborts a tool call after 300 s
            out = (done.stdout + done.stderr).decode("utf-8", "replace")
            code = done.returncode
        except subprocess.TimeoutExpired as exc:
            out = ((exc.stdout or b"") + (exc.stderr or b"")).decode("utf-8", "replace") + "\n[timed out]"
            code = 124
        tail = out[-MAX_OUTPUT:]
        return f"exit_code={code}\n" + (("[output truncated]\n" if len(out) > MAX_OUTPUT else "") + tail)

    def read(self, path, offset=1, limit=2000):
        lines = self.host_path(path).read_text("utf-8", "replace").splitlines()
        chunk = lines[offset - 1: offset - 1 + limit]
        text = "\n".join(chunk)[:MAX_OUTPUT]
        more = len(lines) > offset - 1 + limit
        return text + (f"\n[{len(lines) - (offset - 1 + limit)} more lines]" if more else "")

    def write(self, path, content):
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, "utf-8")
        return f"wrote {len(content.encode())} bytes to {path}"

    def edit(self, path, old_text, new_text):
        target = self.host_path(path)
        text = target.read_text("utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once (found {count})")
        target.write_text(text.replace(old_text, new_text, 1), "utf-8")
        return f"edited {path}"


# The same report deliverable Paper2Run's system prompt demands, given to the generic agent as a user requirement.
REPORT_REQUIREMENT = """Report deliverable: write /work/report.md, a professional experiment report in Simplified Chinese (Markdown) with
every section: title, abstract, background, setup (hardware, software versions, data, hyperparameters as a table), method,
deviations (each difference from the paper and why), results (exact numbers from your result files, Markdown tables,
embedded figures), discussion (what the numbers do and do not support), limitations, conclusion. Make at least one figure
with a separate plotting script (e.g. plot_results.py) that reads only your result/log files and saves a 300 dpi PNG
(matplotlib; English text inside figures) and embed it in report.md.
"""


def run_generic(name: str, paper_text: str, spec: str, model: dict, out: Path, wall_seconds: int,
                report: bool = True) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    workspace, session = out / "work", out / "session"
    workspace.mkdir(exist_ok=True)
    session.mkdir(exist_ok=True)
    prompt = (f"PAPER (full extracted text):\n<<<\n{paper_text}\n>>>\n\n{spec}\n"
              + (REPORT_REQUIREMENT if report else "")
              + "Finish with a short final message of what you ran and the final test metric, and end it with one line "
              "`FINAL_METRIC: <metric name>=<value>`.")
    stats = {"arm": "generic", "scenario": name, "calls": 0, "input_tokens": 0, "output_tokens": 0,
             "total_tokens": 0, "tool_calls": 0, "tool_errors": 0, "error": None, "final_text": ""}
    transcript = (out / "transcript.jsonl").open("w", encoding="utf-8", buffering=1)
    started = time.monotonic()
    with Sandbox(workspace, f"hc-generic-{name}-{uuid4().hex[:6]}") as box:
        def callback(message):
            kind = message.get("type")
            if kind == "tool_request":
                stats["tool_calls"] += 1
                args = message.get("arguments") or {}
                try:
                    result, is_error = getattr(box, message["name"])(**args), False
                except Exception as exc:  # tool errors go back to the model, like any harness
                    result, is_error = f"{type(exc).__name__}: {exc}", True
                    stats["tool_errors"] += 1
                transcript.write(json.dumps({"t": round(time.monotonic() - started, 1), "tool": message["name"],
                                             "args": args, "is_error": is_error, "result": str(result)[:4000]},
                                            ensure_ascii=False) + "\n")
                return {"type": "tool_result", "id": message["id"], "result": result, "is_error": is_error}
            if kind == "event":
                event = message.get("event") or {}
                if event.get("type") == "turn_start":
                    stats["calls"] += 1
                if event.get("type") == "message_end" and (event.get("message") or {}).get("role") == "assistant":
                    m = event["message"]
                    u = m.get("usage") or {}
                    stats["input_tokens"] += sum(u.get(k, 0) or 0 for k in ("input", "cacheRead", "cacheWrite"))
                    stats["output_tokens"] += u.get("output", 0) or 0
                    stats["total_tokens"] += u.get("totalTokens", 0) or 0
                    text = "".join(p.get("text", "") for p in m.get("content", []) if isinstance(p, dict) and p.get("type") == "text")
                    if text:
                        stats["final_text"] = text
                    transcript.write(json.dumps({"t": round(time.monotonic() - started, 1), "assistant": text[:4000],
                                                 "stop": m.get("stopReason")}, ensure_ascii=False) + "\n")
            elif kind == "ready":
                stats["session_file"] = message.get("session_file")
            elif kind == "result":
                stats["error"] = None
                if message.get("text"):
                    stats["final_text"] = message["text"]
            elif kind == "error":
                stats["error"] = {"code": message.get("code"), "message": str(message.get("message"))[:500]}
            return None

        config = {"cwd": str(session), "session_dir": str(session), "model": model,
                  "system_prompt": SYSTEM_PROMPT, "tools": TOOLS, "limits": {"wall_time_seconds": wall_seconds}}
        stats["resumes"] = 0
        while True:
            HarnessBridge().run(config, prompt, callback)
            left = wall_seconds - (time.monotonic() - started)
            # Same policy as the Paper2Run arm: resume a transient provider failure up to 3 times.
            if (not stats["error"] or stats["error"]["code"] not in TRANSIENT or stats["resumes"] >= 3
                    or left < 60 or not stats.get("session_file")):
                break
            stats["resumes"] += 1
            print(f"[generic {name}] auto-resume #{stats['resumes']} after {stats['error']['code']}", flush=True)
            config = {**config, "session_file": stats["session_file"], "limits": {"wall_time_seconds": int(left)}}
            prompt = "Continue the task from where you stopped."
    transcript.close()
    stats["wall_seconds"] = round(time.monotonic() - started, 1)
    (out / "final.md").write_text(stats["final_text"], encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats
