"""Desktop distribution checks without network, global Node or Docker requirements."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = load_script("build_harness", "scripts/desktop/build_harness.py")
launch = load_script("_autonomous_launch", "scripts/launch.py")


def touch(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_fetch_verifies_cache_and_download(monkeypatch, tmp_path):
    payload = b"official archive bytes"
    monkeypatch.setattr(builder, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(builder, "TARGETS", {"test": ("test.zip", hashlib.sha256(payload).hexdigest())})
    calls = []
    def download(url, **kwargs):
        calls.append(url)
        return io.BytesIO(payload)
    monkeypatch.setattr(builder.urllib.request, "urlopen", download)
    archive = builder.fetch_node("test")
    assert archive.read_bytes() == payload
    assert builder.fetch_node("test") == archive
    assert len(calls) == 1
    archive.write_bytes(b"corrupt")
    assert builder.fetch_node("test").read_bytes() == payload
    assert len(calls) == 2


def test_download_mismatch_never_installs(monkeypatch, tmp_path):
    monkeypatch.setattr(builder, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(builder, "TARGETS", {"test": ("test.zip", "0" * 64)})
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"wrong"))
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        builder.fetch_node("test")
    assert not (tmp_path / f"node-v{builder.NODE_VERSION}-test.zip").exists()


def test_cross_build_fails_before_network(monkeypatch):
    monkeypatch.setattr(builder, "host_target", lambda: "darwin-arm64")
    monkeypatch.setattr(builder, "fetch_node", lambda *_: pytest.fail("must not download"))
    with pytest.raises(RuntimeError, match="blocked"):
        builder.build("win32-x64")


@pytest.mark.parametrize("target", builder.TARGETS)
def test_node_layout(target, tmp_path):
    node, npm = builder.node_paths(tmp_path, target)
    assert node.name == ("node.exe" if target.startswith("win32") else "node")
    assert npm.name == "npm-cli.js"
    assert npm.is_relative_to(tmp_path)


def test_zip_path_traversal_refused(tmp_path):
    archive = tmp_path / "node.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../escape", "no")
    with pytest.raises(RuntimeError, match="unsafe"):
        builder.extract_node(archive, tmp_path / "node", "win32-x64")
    assert not (tmp_path / "escape").exists()


def test_node_extract_keeps_npm_and_binary(tmp_path):
    archive = tmp_path / "node.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name in ["release/bin/node", "release/lib/node_modules/npm/bin/npm-cli.js"]:
            entry = tarfile.TarInfo(name)
            entry.size = 4
            entry.mode = 0o755
            tf.addfile(entry, io.BytesIO(b"data"))
    builder.extract_node(archive, tmp_path / "node", "darwin-arm64")
    node, npm = builder.node_paths(tmp_path / "node", "darwin-arm64")
    assert node.is_file() and npm.is_file()
    assert node.stat().st_mode & 0o111


def test_install_uses_pinned_node_and_clean_env(monkeypatch, tmp_path):
    node, npm = builder.node_paths(tmp_path / "node", "darwin-arm64")
    touch(npm)
    monkeypatch.setenv("ELECTRON_RUN_AS_NODE", "1")
    monkeypatch.setenv("NODE_OPTIONS", "--require malicious.js")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("NPM_CONFIG_ARCH", "x64")
    calls = []
    monkeypatch.setattr(builder.subprocess, "run", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or SimpleNamespace(returncode=0))
    builder.install_dependencies(node, npm, tmp_path / "harness")
    command, options = calls[0]
    assert command[:3] == [str(node), str(npm), "ci"]
    assert "--omit=dev" in command
    assert options["cwd"] == tmp_path / "harness"
    assert options["env"]["PATH"].split(builder.os.pathsep)[0] == str(node.parent)
    assert not {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "OPENAI_API_KEY", "NPM_CONFIG_ARCH"} & options["env"].keys()


def test_npm_failure_does_not_echo_credentials(monkeypatch, tmp_path):
    npm = tmp_path / "npm-cli.js"
    touch(npm)
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stderr="secret-key"))
    with pytest.raises(RuntimeError, match="npm ci failed") as error:
        builder.install_dependencies(tmp_path / "node", npm, tmp_path)
    assert "secret-key" not in str(error.value)


@pytest.mark.parametrize("version,valid", [("22.18.0", False), ("22.19.0", True), ("22.22.2", True), ("24.1.0", True)])
def test_node_minimum(monkeypatch, version, valid):
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"version": version, "target": "darwin-arm64"})))
    if valid:
        assert builder.check_node(Path("node"), "darwin-arm64") == version
    else:
        with pytest.raises(RuntimeError, match="22.19"):
            builder.check_node(Path("node"))


def test_node_architecture_rejected(monkeypatch):
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout='{"version":"22.22.2","target":"darwin-x64"}'))
    with pytest.raises(RuntimeError, match="architecture mismatch"):
        builder.check_node(Path("node"), "darwin-arm64")


def test_bridge_protocol_smoke_rejects_non_json(monkeypatch, tmp_path):
    results = iter([SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout="debug text\n")])
    monkeypatch.setattr(builder.subprocess, "run", lambda *a, **k: next(results))
    with pytest.raises(RuntimeError, match="not JSONL"):
        builder.smoke(tmp_path / "node", tmp_path)


def test_skill_changes_invalidate_bundle_fingerprint(tmp_path):
    harness, skills = tmp_path / "harness", tmp_path / "skills"
    touch(harness / "package.json", "{}")
    touch(harness / "package-lock.json", "{}")
    touch(harness / "bridge.mjs", "// bridge")
    touch(skills / "one/SKILL.md", "original")
    original = builder.source_fingerprint(harness, skills)
    touch(skills / "one/SKILL.md", "changed")
    assert builder.source_fingerprint(harness, skills) != original


def test_bundle_manifest_rejects_changed_resources(monkeypatch, tmp_path):
    source, skills = tmp_path / "source", tmp_path / "skills"
    root = tmp_path / "runtime/darwin-arm64"
    touch(source / "package.json", "{}")
    touch(source / "package-lock.json", "{}")
    touch(source / "bridge.mjs", "// original")
    for i in range(8):
        touch(skills / f"{i}/SKILL.md", "skill")
    shutil.copytree(source, root / "harness")
    shutil.copytree(skills, root / "research-skills")
    touch(root / "harness-runtime.json", json.dumps({"target": "darwin-arm64", "node_version": builder.NODE_VERSION, "source_sha256": builder.source_fingerprint(source, skills)}))
    monkeypatch.setattr(builder, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(builder, "HARNESS", source)
    monkeypatch.setattr(builder, "SKILLS", skills)
    monkeypatch.setattr(builder, "host_target", lambda: "darwin-arm64")
    monkeypatch.setattr(builder, "check_node", lambda *a: builder.NODE_VERSION)
    monkeypatch.setattr(builder, "smoke", lambda *a: None)
    builder.validate_bundle("darwin-arm64")
    touch(root / "research-skills/0/SKILL.md", "modified")
    with pytest.raises(RuntimeError, match="differ from the build manifest"):
        builder.validate_bundle("darwin-arm64")


def test_launch_installs_once_and_docker_missing_is_nonfatal(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "host_target", lambda: "darwin-arm64")
    harness = tmp_path / "apps/harness"
    touch(harness / "package.json", "{}")
    touch(harness / "package-lock.json", "{}")
    for i in range(8):
        touch(tmp_path / f"resources/research-skills/{i}/SKILL.md")
    node, npm = builder.node_paths(tmp_path / "apps/desktop/.runtime/darwin-arm64/node", "darwin-arm64")
    touch(node)
    touch(npm)
    for key in ("P2R_NODE_BINARY", "P2R_HARNESS_DIR", "P2R_SKILLS_DIR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(builder, "check_node", lambda *_: "22.22.2")
    monkeypatch.setattr(builder, "smoke", lambda *_: None)
    installs = []
    def install(*args):
        installs.append(args)
        (harness / "node_modules").mkdir(exist_ok=True)
    monkeypatch.setattr(builder, "install_dependencies", install)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    first = launch.ensure_harness()
    assert launch.ensure_harness() == first
    assert len(installs) == 1
    assert first["P2R_NODE_BINARY"] == str(node)
    assert "Docker 未就绪" in capsys.readouterr().out
    touch(harness / "package-lock.json", '{"changed":true}')
    launch.ensure_harness()
    assert len(installs) == 2


def test_launch_missing_node_still_starts_server(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.delenv("P2R_NODE_BINARY", raising=False)
    monkeypatch.setattr(launch.shutil, "which", lambda *_: None)
    monkeypatch.setattr(launch.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    env = launch.ensure_harness()
    assert env["P2R_NODE_BINARY"]
    assert "服务仍会启动" in capsys.readouterr().out


def test_desktop_build_validates_actual_bundle(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
    desktop = load_script("_autonomous_desktop_builder", "scripts/desktop/build_desktop.py")
    monkeypatch.setattr(desktop, "DESKTOP", tmp_path)
    calls = []
    monkeypatch.setattr(builder, "check_node", lambda node, target: calls.append(("node", node)))
    monkeypatch.setattr(builder, "smoke", lambda node, harness: calls.append(("smoke", harness)))
    monkeypatch.setattr(builder, "source_fingerprint", lambda *args: "same")
    monkeypatch.setattr(desktop, "run", lambda args: calls.append(("python", args[0])))
    desktop.validate_packaged("darwin-arm64")
    resources = tmp_path / "release/mac-arm64/Paper2Run.app/Contents/Resources"
    assert calls[0] == ("node", resources / "node/bin/node")
    assert calls[1] == ("smoke", resources / "harness")
    assert calls[2][1] == str(resources / "python/bin/python3.12")
    monkeypatch.setattr(builder, "smoke", lambda *args: (_ for _ in ()).throw(RuntimeError("SDK missing")))
    with pytest.raises(RuntimeError, match="SDK missing"):
        desktop.validate_packaged("darwin-arm64")


def test_windows_launch_prepares_autonomous_mode(monkeypatch, tmp_path, capsys):
    # Windows no longer skips the autonomous path: Node, Harness and Docker are checked.
    monkeypatch.setattr(launch, "ROOT", tmp_path)
    monkeypatch.setattr(launch, "WINDOWS", True)
    monkeypatch.setattr(builder, "host_target", lambda: "win32-x64")
    for key in ("P2R_NODE_BINARY", "P2R_HARNESS_DIR", "P2R_SKILLS_DIR"):
        monkeypatch.delenv(key, raising=False)
    node, _ = builder.node_paths(tmp_path / "apps/desktop/.runtime/win32-x64/node", "win32-x64")
    touch(node)
    checked = []
    monkeypatch.setattr(builder, "check_node", lambda path: checked.append(path) or (_ for _ in ()).throw(RuntimeError("stop")))
    probes = []
    monkeypatch.setattr(launch.subprocess, "run", lambda argv, **k: probes.append(argv) or SimpleNamespace(returncode=0, stdout=b"linux\n"))
    env = launch.ensure_harness()
    assert env["P2R_NODE_BINARY"] == str(node.resolve()) and checked == [node.resolve()]
    assert probes == [["docker", "info", "--format", "{{.OSType}}"]]
    out = capsys.readouterr().out
    assert "Windows 暂不可用" not in out and "Docker" not in out


@pytest.mark.parametrize("windows,result,expected", [
    (True, SimpleNamespace(returncode=0, stdout=b"linux\n"), None),
    (False, SimpleNamespace(returncode=0, stdout=b"linux"), None),
    (True, SimpleNamespace(returncode=0, stdout=b"windows\n"), "OSType=windows"),
    (True, SimpleNamespace(returncode=1, stdout=b""), "Docker Desktop（WSL2 后端）"),
    (False, SimpleNamespace(returncode=1, stdout=b""), "启动 Docker 后可重试"),
    (True, OSError("docker not found"), "Docker 未就绪"),
])
def test_launch_docker_status(monkeypatch, windows, result, expected):
    monkeypatch.setattr(launch, "WINDOWS", windows)
    def run(argv, **kwargs):
        assert argv == ["docker", "info", "--format", "{{.OSType}}"]
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(launch.subprocess, "run", run)
    status = launch.docker_status()
    assert status is None if expected is None else expected in status


def run_desktop_check(is_win, docker, exists=True):
    """Evaluate dockerMessage + checkAutonomousRuntime from main.cjs with stubbed IO."""
    source = (ROOT / "apps/desktop/main.cjs").read_text()
    functions = source[source.index("function dockerMessage("):source.index("function healthOk(")]
    script = ("const path=require('node:path');const IS_WIN=" + json.dumps(is_win) + ";const messages=[];const calls=[];"
              "const writeLog=x=>messages.push(x);const backendEnv=()=>({});const fs={existsSync:()=>" + json.dumps(exists) + "};"
              "const docker=" + json.dumps(docker) + ";"
              "const execFile=(cmd,args,opts,cb)=>{calls.push([cmd,args,opts.windowsHide]);"
              "if(cmd==='docker') cb(docker.error?new Error(docker.error):null, docker.stdout); else cb(null,'22.22.2\\n');};"
              + functions + ";checkAutonomousRuntime({node:'n',harness:'h',skills:'s'});console.log(JSON.stringify({messages,calls}));")
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="Node required for Electron function test")
@pytest.mark.parametrize("is_win", [True, False])
def test_desktop_runtime_check_is_the_same_on_every_platform(is_win):
    data = run_desktop_check(is_win, {"stdout": "linux\n"})
    assert data["messages"] == []
    assert ["docker", ["info", "--format", "{{.OSType}}"], True] in data["calls"]
    assert ["n", ["-p", "process.versions.node"], True] in data["calls"]
    assert not any("Windows 暂不可用" in m for m in data["messages"])


@pytest.mark.skipif(not shutil.which("node"), reason="Node required for Electron function test")
def test_desktop_docker_messages():
    missing = run_desktop_check(True, {"error": "spawn docker ENOENT"})["messages"]
    assert len(missing) == 1 and "未检测到可用的 Docker" in missing[0] and "WSL2" in missing[0]
    windows_mode = run_desktop_check(True, {"stdout": "windows\n"})["messages"]
    assert len(windows_mode) == 1 and "OSType=windows" in windows_mode[0] and "Linux 容器" in windows_mode[0]
    unknown = run_desktop_check(False, {"stdout": ""})["messages"]
    assert "OSType=未知" in unknown[0]
    # Missing Harness files are reported, and Docker is still probed.
    absent = run_desktop_check(True, {"stdout": "linux"}, exists=False)
    assert absent["calls"] == [["docker", ["info", "--format", "{{.OSType}}"], True]]
    assert "autonomous mode unavailable: missing" in absent["messages"][0]


def test_electron_resource_maps_cover_three_targets():
    config = json.loads((ROOT / "apps/desktop/package.json").read_text())["build"]
    for system in ("mac", "win"):
        resources = {item["to"]: item["from"] for item in config[system]["extraResources"]}
        assert {"python", "node", "harness", "research-skills", "harness-runtime.json"} <= resources.keys()
        assert "${arch}" in resources["harness"]
        assert resources["harness/node_modules"].endswith("/harness/node_modules")


@pytest.mark.skipif(not shutil.which("node"), reason="Node required for Electron function test")
@pytest.mark.parametrize("platform,arch", [("darwin", "arm64"), ("darwin", "x64"), ("win32", "x64")])
def test_electron_runtime_paths_and_environment(platform, arch):
    # Evaluate just the pure path/env functions; never launch Electron or a backend.
    source = (ROOT / "apps/desktop/main.cjs").read_text()
    functions = source[source.index("function runtimePaths()"):source.index("function checkAutonomousRuntime(")]
    script = "const path=require('node:path').posix; const app={isPackaged:true}; const IS_WIN=" + json.dumps(platform == "win32") + ";const dataDir='/data';const process={resourcesPath:'/bundle',env:{ELECTRON_RUN_AS_NODE:'1',NODE_OPTIONS:'bad',PYTHONPATH:'bad',KEEP:'yes'}};" + functions + ";console.log(JSON.stringify({paths:runtimePaths(),env:backendEnv(runtimePaths())}));"
    result = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    expected = "/bundle/node/node.exe" if platform == "win32" else "/bundle/node/bin/node"
    assert data["paths"]["node"] == data["env"]["P2R_NODE_BINARY"] == expected
    assert data["env"]["P2R_HARNESS_DIR"] == "/bundle/harness"
    assert data["env"]["P2R_SKILLS_DIR"] == "/bundle/research-skills"
    assert not {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "PYTHONPATH"} & data["env"].keys()
    assert data["env"]["KEEP"] == "yes"


def test_desktop_build_stages_tunnel_and_image(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT / "scripts/desktop"))
    desktop = load_script("_autonomous_desktop_stage", "scripts/desktop/build_desktop.py")
    monkeypatch.setattr(desktop.build_runtime, "RUNTIME_DIR", tmp_path / "runtime")
    tunnel, image = tmp_path / "p2r-tunnel.exe", tmp_path / "img.tar.gz"
    tunnel.write_bytes(b"MZ")
    image.write_bytes(b"gz")
    desktop.stage_extras("win32-x64", tunnel, image)
    staged = tmp_path / "runtime" / "win32-x64"
    assert (staged / "tunnel" / "p2r-tunnel.exe").read_bytes() == b"MZ"
    assert (staged / "tunnel" / "remote.json").is_file() and (staged / "tunnel" / "relay-ca.pem").is_file()
    assert (staged / "image" / "paper2run-experiment-1.tar.gz").read_bytes() == b"gz"
    desktop.stage_extras("darwin-arm64", None, None)  # both optional
    assert not (tmp_path / "runtime" / "darwin-arm64").exists()
    with pytest.raises(SystemExit, match="does not exist"):
        desktop.stage_extras("win32-x64", tmp_path / "missing.exe", None)
