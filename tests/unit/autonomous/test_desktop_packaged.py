"""Opt-in real packaged Electron smoke, without model calls or experiments.

P2R_DESKTOP_SMOKE_APP=/absolute/Paper2Run.app uv run --extra test python -m pytest \
    tests/unit/autonomous/test_desktop_packaged.py -q -s
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[3]
APP = os.environ.get("P2R_DESKTOP_SMOKE_APP")


def get_json(url):
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.load(response)


@pytest.mark.skipif(not APP, reason="set P2R_DESKTOP_SMOKE_APP to run a built app")
def test_packaged_electron_start_render_and_exit():
    bundle = Path(APP)
    executable = bundle / "Contents/MacOS/Paper2Run"
    resources = bundle / "Contents/Resources"
    assert executable.is_file()
    for item in ("python/bin/python3.12", "node/bin/node", "harness/bridge.mjs", "web/index.html", "harness-runtime.json"):
        assert (resources / item).is_file(), item
    assert len(list((resources / "research-skills").glob("*/SKILL.md"))) == 8
    import sys
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
    import build_harness
    build_harness.smoke(resources / "node/bin/node", resources / "harness")
    state = ROOT / "apps/desktop/.cache" / ("app-smoke-" + uuid.uuid4().hex)
    state.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ELECTRON_", "NODE_", "P2R_")) and not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
    output = (state / "electron.log").open("wb")
    child = subprocess.Popen([str(executable), "--user-data-dir=" + str(state), "--remote-debugging-port=0"], cwd=state, env=env, stdout=output, stderr=subprocess.STDOUT)
    url = None
    try:
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            assert child.poll() is None, f"Electron exited {child.returncode}; see {state}"
            log = state / "logs/backend.log"
            text = log.read_text(errors="replace") if log.exists() else ""
            match = re.search(r"backend ready at (http://127\.0\.0\.1:\d+)", text)
            devtools = state / "DevToolsActivePort"
            if match and devtools.exists():
                url = match.group(1)
                port = devtools.read_text().splitlines()[0]
                pages = get_json(f"http://127.0.0.1:{port}/json/list")
                page = next((p for p in pages if p.get("type") == "page" and p.get("url", "").startswith(url)), None)
                if page:
                    break
            time.sleep(.25)
        else:
            pytest.fail(f"packaged app did not become ready; see {state}")
        assert get_json(url + "/healthz")["status"] == "ok"
        # Built-in Node WebSocket drives CDP solely over this smoke app's local port.
        script = r"""
const ws = new WebSocket(process.argv[1]);
const timer=setTimeout(()=>{console.error('CDP timeout');process.exit(2)},15000);
ws.onopen=()=>ws.send(JSON.stringify({id:1,method:'Runtime.evaluate',params:{expression:'JSON.stringify({ready:document.readyState,title:document.title,text:document.body.innerText})',returnByValue:true}}));
ws.onmessage=e=>{const m=JSON.parse(e.data);if(m.id===1){console.log(m.result.result.value);clearTimeout(timer);ws.close();}};
"""
        rendered = None
        for _ in range(30):
            result = subprocess.run([str(resources / "node/bin/node"), "-e", script, page["webSocketDebuggerUrl"]], env=env, capture_output=True, text=True, timeout=20, check=True)
            rendered = json.loads(result.stdout)
            if rendered["ready"] == "complete" and len(rendered["text"].strip()) > 60:
                break
            time.sleep(.5)
        assert rendered["ready"] == "complete" and len(rendered["text"].strip()) > 60, rendered
        assert "Paper2Run" in rendered["title"] or "Paper2Run" in rendered["text"]
        print(json.dumps({"app": str(bundle), "state": str(state), "url": url, "title": rendered["title"], "rendered_characters": len(rendered["text"])}, ensure_ascii=False))
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=20)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        output.close()
    assert child.returncode == 0
    if url:
        with pytest.raises((OSError, urllib.error.URLError)):
            get_json(url + "/healthz")
