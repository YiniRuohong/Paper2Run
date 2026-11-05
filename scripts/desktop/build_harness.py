"""Build a native-host Node/Pi runtime. Cross-platform npm installs are refused.

Run on each target host: python scripts/desktop/build_harness.py --target darwin-arm64
The official Node archive and npm lockfile are pinned; no global Node is used.
Windows artifacts only provision dependencies: autonomous mode is currently
unsupported there because CapabilityRegistry requires POSIX flock/no-follow.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / "apps/desktop/.runtime"
CACHE_DIR = ROOT / "apps/desktop/.cache"
HARNESS = ROOT / "apps/harness"
SKILLS = ROOT / "resources/research-skills"
NODE_VERSION = "22.22.2"
NODE_MIN = (22, 19, 0)
SDK = "@earendil-works/pi-coding-agent"
# Source: https://nodejs.org/dist/v22.22.2/SHASUMS256.txt
TARGETS = {
    "darwin-arm64": ("darwin-arm64.tar.gz", "db4b275b83736df67533529a18cc55de2549a8329ace6c7bcc68f8d22d3c9000"),
    "darwin-x64": ("darwin-x64.tar.gz", "12a6abb9c2902cf48a21120da13f87fde1ed1b71a13330712949e8db818708ba"),
    "win32-x64": ("win-x64.zip", "7c93e9d92bf68c07182b471aa187e35ee6cd08ef0f24ab060dfff605fcc1c57c"),
}


def host_target() -> str:
    machine = platform.machine().lower()
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine, machine)
    return f"{sys.platform}-{arch}"


def require_native(target: str) -> None:
    if target not in TARGETS:
        raise RuntimeError(f"unsupported desktop target: {target}")
    if target != host_target():
        raise RuntimeError(f"Harness native dependencies require a {target} build host; current host is {host_target()}. Cross-target npm installation is not verified and is blocked.")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch_node(target: str) -> Path:
    suffix, expected = TARGETS[target]
    name = f"node-v{NODE_VERSION}-{suffix}"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / name
    if dest.is_file() and digest(dest) == expected:
        return dest
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(f"https://nodejs.org/dist/v{NODE_VERSION}/{name}", timeout=120) as response, part.open("wb") as out:
        shutil.copyfileobj(response, out)
    if digest(part) != expected:
        raise RuntimeError(f"SHA-256 mismatch for {name}; archive not installed")
    part.replace(dest)
    return dest


def node_paths(root: Path, target: str) -> tuple[Path, Path]:
    if target.startswith("win32-"):
        return root / "node.exe", root / "node_modules/npm/bin/npm-cli.js"
    return root / "bin/node", root / "lib/node_modules/npm/bin/npm-cli.js"


def extract_node(archive: Path, dest: Path, target: str) -> None:
    with tempfile.TemporaryDirectory(prefix="node-extract-", dir=dest.parent) as work:
        staging = Path(work)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as zf:
                for name in zf.namelist():
                    if "\\" in name or not (staging / name).resolve().is_relative_to(staging.resolve()):
                        raise RuntimeError("unsafe Node archive path")
                zf.extractall(staging)
        else:
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(staging, filter="data")
        entries = list(staging.iterdir())
        if len(entries) != 1 or not node_paths(entries[0], target)[0].is_file():
            raise RuntimeError("unexpected Node archive layout")
        shutil.move(str(entries[0]), str(dest))


def clean_env(node: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("ELECTRON_", "NODE_", "NPM_CONFIG_")) and not any(s in k.upper() for s in ("API_KEY", "TOKEN", "SECRET", "PASSWORD"))}
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    env["npm_config_engine_strict"] = "true"
    return env


def check_node(node: Path, target: str | None = None) -> str:
    result = subprocess.run([str(node), "-p", "JSON.stringify({version:process.versions.node,target:process.platform+'-'+process.arch})"], check=True, capture_output=True, text=True, timeout=20, env=clean_env(node))
    info = json.loads(result.stdout)
    version = tuple(int(n) for n in info["version"].split(".")[:3])
    if version < NODE_MIN:
        raise RuntimeError(f"Node >=22.19.0 required; found {info['version']}")
    if target and info["target"] != target:
        raise RuntimeError(f"Node architecture mismatch: expected {target}, got {info['target']}")
    return info["version"]


def install_dependencies(node: Path, npm_cli: Path, harness: Path) -> None:
    # Execute npm's JS entrypoint with the pinned interpreter, also used by scripts.
    # Never copy a developer's node_modules (which can contain host-native addons).
    if not npm_cli.is_file():
        raise RuntimeError(f"npm CLI missing: {npm_cli}")
    result = subprocess.run([str(node), str(npm_cli), "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=harness, env=clean_env(node), capture_output=True, text=True, timeout=600)
    if result.returncode:
        # npm output can contain private registry credentials; do not echo it.
        raise RuntimeError(f"Harness npm ci failed (exit {result.returncode}); check the lockfile, registry access and native build prerequisites")


def smoke(node: Path, harness: Path) -> None:
    env = clean_env(node)
    imported = subprocess.run([str(node), "--input-type=module", "-e", f"const sdk=await import('{SDK}'); if(typeof sdk.createAgentSession!=='function') process.exit(2)"], cwd=harness, env=env, capture_output=True, text=True, timeout=60)
    if imported.returncode:
        raise RuntimeError("Pi SDK import smoke failed; installed runtime is unusable")
    result = subprocess.run([str(node), str(harness / "bridge.mjs")], input='{"type":"abort"}\n', cwd=harness, env=env, capture_output=True, text=True, timeout=30)
    try:
        messages = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except ValueError as exc:
        raise RuntimeError("Harness stdout is not JSONL") from exc
    if result.returncode or not any(m.get("type") == "error" and m.get("code") == "aborted" for m in messages):
        raise RuntimeError("Harness abort-before-start protocol smoke failed")


def source_fingerprint(harness: Path = HARNESS, skills: Path = SKILLS) -> str:
    h = hashlib.sha256()
    paths = [harness / "package.json", harness / "package-lock.json", *sorted(harness.glob("*.mjs")), *sorted(skills.rglob("*"))]
    for path in paths:
        if path.is_file():
            h.update(str(path.relative_to(harness) if path.is_relative_to(harness) else path.relative_to(skills)).encode())
            h.update(path.read_bytes())
    return h.hexdigest()


def validate_bundle(target: str) -> None:
    require_native(target)
    root = RUNTIME_DIR / target
    manifest = json.loads((root / "harness-runtime.json").read_text())
    if manifest.get("target") != target or manifest.get("node_version") != NODE_VERSION or manifest.get("source_sha256") != source_fingerprint(HARNESS, SKILLS):
        raise RuntimeError("Harness runtime is stale; rebuild without --skip-runtime")
    if source_fingerprint(root / "harness", root / "research-skills") != manifest["source_sha256"]:
        raise RuntimeError("Bundled Harness/skills differ from the build manifest; rebuild runtime")
    node, _ = node_paths(root / "node", target)
    if check_node(node, target) != NODE_VERSION:
        raise RuntimeError("Bundled Node version differs from pinned version")
    if len(list((root / "research-skills").glob("*/SKILL.md"))) != 8:
        raise RuntimeError("Bundled research skills are incomplete")
    smoke(node, root / "harness")


def build(target: str) -> Path:
    require_native(target)  # Fail before any download or runtime mutation.
    for name in ("package.json", "package-lock.json", "bridge.mjs"):
        if not (HARNESS / name).is_file():
            raise RuntimeError(f"Harness source missing: {name}")
    if len(list(SKILLS.glob("*/SKILL.md"))) != 8:
        raise RuntimeError("Expected eight bundled research skills")
    fingerprint = source_fingerprint(HARNESS, SKILLS)
    archive = fetch_node(target)
    root = RUNTIME_DIR / target
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="harness-build-", dir=root.parent) as work:
        staging = Path(work)
        extract_node(archive, staging / "node", target)
        node, npm = node_paths(staging / "node", target)
        if check_node(node, target) != NODE_VERSION:
            raise RuntimeError("Downloaded Node version differs from pin")
        harness = staging / "harness"
        harness.mkdir()
        for source in [HARNESS / "package.json", HARNESS / "package-lock.json", *HARNESS.glob("*.mjs")]:
            shutil.copy2(source, harness / source.name)
        install_dependencies(node, npm, harness)
        smoke(node, harness)
        shutil.copytree(SKILLS, staging / "research-skills")
        if source_fingerprint(HARNESS, SKILLS) != fingerprint:
            raise RuntimeError("Harness sources changed during build; retry after edits finish")
        manifest = {"target": target, "node_version": NODE_VERSION, "source_sha256": fingerprint, "sdk": json.loads((harness / "package.json").read_text())["dependencies"][SDK]}
        (root / "harness-runtime.json").unlink(missing_ok=True)
        for name in ("node", "harness", "research-skills"):
            if (root / name).exists():
                shutil.rmtree(root / name)
            shutil.move(str(staging / name), str(root / name))
        (root / "harness-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    validate_bundle(target)
    print(f"[harness] Node {NODE_VERSION}, SDK import and bridge protocol verified: {root}", flush=True)
    return root


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=sorted(TARGETS), default=host_target())
    try:
        build(parser.parse_args().target)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        raise SystemExit(f"[harness] {exc}") from None
