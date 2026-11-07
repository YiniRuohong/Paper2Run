"""Build the relocatable CPython runtime bundled into the Paper2Run desktop app.

Why a real interpreter instead of PyInstaller/Nuitka: the Electron shell starts
``serve.py`` with interpreter flags, and ``p2r_docparse.install`` runs
``sys.executable -m pip --python ...`` to install MinerU on demand. A frozen
executable is not a Python interpreter, so the backend must ship as a real,
relocatable CPython. We use
python-build-standalone (PBS) ``install_only_stripped`` builds, pinned below
by release + SHA-256.

Steps (stdlib only; run with any Python >= 3.11, e.g. the repo ``.venv``):

1. download the pinned PBS archive for ``--target`` into
   ``apps/desktop/.cache/`` and verify its SHA-256;
2. extract it into ``apps/desktop/.runtime/<target>/python``;
3. build the ``paper2run`` wheel (pure Python, ``py3-none-any``) and install
   it + dependencies into the runtime's site-packages with the HOST
   interpreter running pip in cross-install mode (``--target --platform
   --python-version --implementation --only-binary=:all:``), so the Windows
   runtime can be assembled on macOS and vice versa. Versions are pinned to
   the committed ``uv.lock`` (``--no-lock`` to opt out); every installed
   requirement is then re-checked against the TARGET's environment markers;
4. prune dead weight (tests, idlelib, tkinter/Tcl, ensurepip, setuptools,
   headers). pip is KEPT, intact, so the app can install MinerU on demand
   into a separate venv (``p2r_docparse.install`` runs ``-m pip --python``);
5. precompile bytecode with ``unchecked-hash`` invalidation so the read-only
   app bundle is never written to at runtime (keeps the macOS code-signature
   seal intact and works under per-machine installs);
6. when target == host: import smoke check + serve.py /healthz smoke test.

pip comes from the pinned PBS archive itself (copied to the cache and run by
the host interpreter), so the host Python does not need pip installed and the
pip version is pinned together with the runtime. User pip configuration
(``PIP_INDEX_URL`` / pip.conf mirrors) is honoured.
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
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DESKTOP = ROOT / "apps" / "desktop"
CACHE_DIR = DESKTOP / ".cache"
RUNTIME_DIR = DESKTOP / ".runtime"
SMOKE_SCRIPT = Path(__file__).resolve().parent / "smoke_runtime.py"

# Pinned python-build-standalone release. To bump: change the release/version
# and copy the three hashes from the release's SHA256SUMS asset.
PBS_RELEASE = "20261026"
PY_VERSION = "3.12.16"
PY_MINOR = "3.12"
PBS_URL = "https://github.com/astral-sh/python-build-standalone/releases/download/{release}/{name}"


@dataclass(frozen=True)
class Target:
    triple: str
    sha256: str
    # pip --platform tags, most specific first. macOS floor is 12.0 because
    # the bundled Electron requires macOS 12+; pip expands each tag downward
    # (11.0, 10.x, universal2) on its own.
    pip_platforms: tuple[str, ...]
    windows: bool
    machine: str  # PEP 508 platform_machine on the target

    @property
    def marker_env(self) -> dict[str, str]:
        """PEP 508 marker environment of the target (not of this host)."""
        return {
            "sys_platform": "win32" if self.windows else "darwin",
            "platform_system": "Windows" if self.windows else "Darwin",
            "os_name": "nt" if self.windows else "posix",
            "platform_machine": self.machine,
            "implementation_name": "cpython",
            "platform_python_implementation": "CPython",
            "python_version": PY_MINOR,
            "python_full_version": PY_VERSION,
            "implementation_version": PY_VERSION,
            "extra": "",
        }

    @property
    def archive(self) -> str:
        return f"cpython-{PY_VERSION}+{PBS_RELEASE}-{self.triple}-install_only_stripped.tar.gz"


TARGETS: dict[str, Target] = {
    "darwin-arm64": Target(
        "aarch64-apple-darwin",
        "353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e",
        ("macosx_12_0_arm64", "macosx_11_0_arm64"), windows=False, machine="arm64"),
    "darwin-x64": Target(
        "x86_64-apple-darwin",
        "37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734",
        ("macosx_12_0_x86_64",), windows=False, machine="x86_64"),
    "win32-x64": Target(
        "x86_64-pc-windows-msvc",
        "1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149",
        ("win_amd64",), windows=True, machine="AMD64"),
}

# Stdlib parts no server code path imports.
PRUNE_STDLIB = ("test", "idlelib", "tkinter", "turtledemo", "turtle.py", "ensurepip", "lib2to3")
# Site-packages entries that only matter for building packages. pip stays: the
# on-demand MinerU installer drives a --without-pip venv with ``-m pip --python``.
PRUNE_SITE = ("setuptools", "_distutils_hack", "distutils-precedence.pth")
# Never test-pruned: shipped as PBS installed it (bytecode is rebuilt like the rest).
KEEP_INTACT_SITE = ("pip",)


def log(text: str) -> None:
    print(f"[runtime] {text}", flush=True)


def host_target() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if sys.platform == "win32":
        return "win32-x64"
    raise SystemExit(f"unsupported host platform {sys.platform}/{machine}; pass --target")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_archive(target: Target) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / target.archive
    if dest.is_file():
        if sha256_of(dest) == target.sha256:
            log(f"cached {dest.name} (sha256 ok)")
            return dest
        log(f"cached {dest.name} has a wrong hash; re-downloading")
        dest.unlink()
    url = PBS_URL.format(release=PBS_RELEASE, name=target.archive)
    log(f"downloading {url}")
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, part.open("wb") as out:
        shutil.copyfileobj(response, out, 1 << 20)
    actual = sha256_of(part)
    if actual != target.sha256:
        part.unlink()
        raise SystemExit(f"SHA-256 mismatch for {target.archive}: expected {target.sha256}, got {actual}")
    part.replace(dest)
    log(f"verified sha256 {actual}")
    return dest


def extract(archive: Path, out_dir: Path) -> Path:
    """Extract into a fresh, empty staging dir and move ``python/`` into place."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f"{out_dir.name}.extract-", dir=out_dir.parent))
    try:
        with tarfile.open(archive, "r:gz") as tar:
            # 'data' filter: no absolute paths, no links escaping the tree,
            # no device files - the archive is downloaded data.
            tar.extractall(staging, filter="data")
        python_dir = staging / "python"
        if not python_dir.is_dir():
            raise SystemExit(f"unexpected archive layout in {archive.name}: no top-level python/")
        out_dir.mkdir(parents=True)
        python_dir.replace(out_dir / "python")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return out_dir / "python"


def layout(python_dir: Path, target: Target) -> dict[str, Path]:
    if target.windows:
        lib = python_dir / "Lib"
        return {"exe": python_dir / "python.exe", "lib": lib, "site": lib / "site-packages"}
    lib = python_dir / "lib" / f"python{PY_MINOR}"
    return {"exe": python_dir / "bin" / f"python{PY_MINOR}", "lib": lib, "site": lib / "site-packages"}


def pip_tool(site_packages: Path) -> Path:
    """Copy the archive's pure-Python pip out of the runtime.

    The host runs this copy, not the runtime's own pip, so only the tool root
    is on the pip process's sys.path while the runtime site-packages fills up.
    """
    tool_root = CACHE_DIR / f"pip-tool-{PBS_RELEASE}"
    if not (tool_root / "pip" / "__main__.py").is_file():
        source = site_packages / "pip"
        if not source.is_dir():
            raise SystemExit(f"no pip in {site_packages}; cannot install packages")
        shutil.rmtree(tool_root, ignore_errors=True)
        shutil.copytree(source, tool_root / "pip", ignore=shutil.ignore_patterns("__pycache__"))
    return tool_root / "pip"


def run(cmd: list[str], **kwargs) -> None:
    log("$ " + " ".join(str(part) for part in cmd))
    subprocess.run([str(part) for part in cmd], check=True, **kwargs)


def pip(tool: Path, *args: str) -> None:
    env = dict(os.environ)
    env.update({"PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
    # Running the pip *directory* puts only the tool root on sys.path; -E/-s
    # keep the host's PYTHONPATH and user site out of the pip process.
    run([sys.executable, "-E", "-s", str(tool), *args], env=env, cwd=ROOT)


def build_wheel(tool: Path, work: Path) -> Path:
    wheel_dir = work / "wheel"
    pip(tool, "wheel", str(ROOT), "--no-deps", "--wheel-dir", str(wheel_dir), "--quiet")
    wheels = sorted(wheel_dir.glob("paper2run-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one paper2run py3-none-any wheel, found {[w.name for w in wheels]}")
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
    log(f"built {wheels[0].name} ({len(names)} files)")
    return wheels[0]


def lock_constraints(work: Path) -> Path | None:
    """pip constraints pinning every dependency to the committed ``uv.lock``.

    uv.lock is a universal (all-platform) resolution, so the same pins serve
    the macOS and Windows runtimes and the shipped app matches the lock.
    """
    lock = ROOT / "uv.lock"
    if not lock.is_file():
        return None
    import tomllib

    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    pins = sorted(f"{p['name']}=={p['version']}" for p in data.get("package", [])
                  if "version" in p and p["name"] != "paper2run")
    path = work / "constraints.txt"
    path.write_text("\n".join(pins) + "\n", encoding="utf-8")
    log(f"pinning {len(pins)} packages from uv.lock")
    return path


def install_packages(tool: Path, wheel: Path, site: Path, target: Target, work: Path, *,
                     use_lock: bool) -> None:
    staging = work / "site"
    args = ["install", "--target", str(staging), "--python-version", PY_MINOR,
            "--implementation", "cp", "--abi", "cp" + PY_MINOR.replace(".", ""),
            "--only-binary=:all:", "--no-compile", "--no-warn-script-location", "--quiet"]
    for tag in target.pip_platforms:
        args += ["--platform", tag]
    constraints = lock_constraints(work) if use_lock else None
    if constraints is not None:
        args += ["--constraint", str(constraints)]
    pip(tool, *args, str(wheel))
    # --target drops console scripts with a shebang pointing at the HOST
    # interpreter; the app never uses them.
    for scripts in ("bin", "Scripts"):
        shutil.rmtree(staging / scripts, ignore_errors=True)
    shutil.copytree(staging, site, dirs_exist_ok=True)


def verify_requirements(tool: Path, site: Path, target: Target) -> None:
    """Fail if any installed distribution's requirement is missing ON THE TARGET.

    pip evaluates environment markers (``sys_platform == 'win32'`` ...)
    against the host even in ``--platform`` mode, so a cross-build can
    silently drop a Windows-only dependency. Re-evaluate every
    ``Requires-Dist`` against the target's marker environment instead.
    """
    from email.parser import Parser

    sys.path.insert(0, str(tool.parent))
    try:
        from pip._vendor.packaging.requirements import Requirement
        from pip._vendor.packaging.utils import canonicalize_name
    finally:
        sys.path.pop(0)
    metas = {}
    for meta_path in site.glob("*.dist-info/METADATA"):
        meta = Parser().parsestr(meta_path.read_text(encoding="utf-8", errors="replace"))
        metas[canonicalize_name(meta["Name"])] = meta
    env = target.marker_env
    missing = []
    for name, meta in sorted(metas.items()):
        for raw in meta.get_all("Requires-Dist") or []:
            req = Requirement(raw)
            if req.marker is not None and not req.marker.evaluate(env):
                continue
            if canonicalize_name(req.name) not in metas:
                missing.append(f"{name} requires {raw!r}")
    if missing:
        raise SystemExit("runtime is missing target dependencies:\n  " + "\n  ".join(missing))
    log(f"verified requirements of {len(metas)} distributions for {env['sys_platform']}/{env['platform_machine']}")


def remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def prune(python_dir: Path, paths: dict[str, Path], target: Target) -> None:
    lib, site = paths["lib"], paths["site"]
    for name in PRUNE_STDLIB:
        remove(lib / name)
    for name in PRUNE_SITE:
        remove(site / name)
    for dist_info in list(site.glob("setuptools-*.dist-info")):
        remove(dist_info)
    # Test suites shipped inside third-party packages (never inside p2r_* or pip).
    for tests in [p for p in site.rglob("*") if p.is_dir() and p.name in ("tests", "test")]:
        top = tests.relative_to(site).parts[0]
        if tests.exists() and not top.startswith("p2r_") and top not in KEEP_INTACT_SITE:
            remove(tests)
    for cache in [p for p in python_dir.rglob("__pycache__") if p.is_dir()]:
        remove(cache)
    if target.windows:
        for name in ("tcl", "include", "libs", "Scripts"):
            remove(python_dir / name)
        for pattern in ("_tkinter*.pyd", "tcl*.dll", "tk*.dll"):
            for dll in (python_dir / "DLLs").glob(pattern):
                remove(dll)
    else:
        for name in ("include", "share"):
            remove(python_dir / name)
        remove(python_dir / "lib" / "pkgconfig")
        for pattern in ("libtcl*", "libtk*", "tcl*", "tk*", "itcl*", "thread*"):
            for entry in (python_dir / "lib").glob(pattern):
                remove(entry)
        for so in (lib / "lib-dynload").glob("_tkinter*"):
            remove(so)
        keep = {f"python{PY_MINOR}", "python3", "python"}
        for entry in (python_dir / "bin").iterdir():
            if entry.name not in keep:
                remove(entry)


def compile_bytecode(paths: dict[str, Path], target_is_host: bool) -> None:
    """Precompile with unchecked-hash pycs: valid regardless of file mtimes."""
    if target_is_host:
        interpreter = str(paths["exe"])
    elif sys.version_info[:2] == tuple(int(p) for p in PY_MINOR.split(".")):
        interpreter = sys.executable  # same bytecode magic as the target runtime
    else:
        log(f"skip bytecode precompile: host Python {sys.version_info[0]}.{sys.version_info[1]} != {PY_MINOR}")
        return
    run([interpreter, "-I", "-m", "compileall", "-q", "-j", "0",
         "--invalidation-mode", "unchecked-hash", str(paths["lib"])])


def write_manifest(python_dir: Path, paths: dict[str, Path], target_name: str, wheel: Path) -> None:
    packages = sorted(p.name[: -len(".dist-info")] for p in paths["site"].glob("*.dist-info"))
    manifest = {
        "target": target_name,
        "python": PY_VERSION,
        "python_build_standalone": PBS_RELEASE,
        "archive": TARGETS[target_name].archive,
        "archive_sha256": TARGETS[target_name].sha256,
        "paper2run_wheel": wheel.name,
        "packages": packages,
    }
    (python_dir / "p2r-runtime.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def smoke(paths: dict[str, Path]) -> None:
    run([paths["exe"], "-I", "-c",
         "import p2r_api.main, p2r_sandbox.autonomous, uvicorn, sys; print('imports ok', sys.executable)"])
    run([sys.executable, str(SMOKE_SCRIPT), "--python", str(paths["exe"])])


def build(target_name: str, *, do_smoke: bool = True, use_lock: bool = True) -> Path:
    target = TARGETS[target_name]
    target_is_host = target_name == host_target()
    out_dir = RUNTIME_DIR / target_name
    archive = fetch_archive(target)
    python_dir = extract(archive, out_dir)
    paths = layout(python_dir, target)
    if not paths["exe"].is_file():
        raise SystemExit(f"interpreter missing after extraction: {paths['exe']}")
    tool = pip_tool(paths["site"])
    with tempfile.TemporaryDirectory(prefix="p2r-runtime-") as tmp:
        work = Path(tmp)
        wheel = build_wheel(tool, work)
        install_packages(tool, wheel, paths["site"], target, work, use_lock=use_lock)
        prune(python_dir, paths, target)
        verify_requirements(tool, paths["site"], target)
        write_manifest(python_dir, paths, target_name, wheel)
    compile_bytecode(paths, target_is_host)
    log(f"runtime ready: {python_dir} ({dir_size(python_dir) / 1e6:.1f} MB)")
    if do_smoke and target_is_host:
        smoke(paths)
    elif not target_is_host:
        log(f"cross-built for {target_name}: smoke test skipped (run it on a {target_name} host)")
    return python_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--target", choices=sorted(TARGETS), default=None,
                        help="runtime target (default: this host)")
    parser.add_argument("--no-smoke", action="store_true", help="skip the host smoke test")
    parser.add_argument("--no-lock", action="store_true",
                        help="resolve the newest versions allowed by pyproject.toml instead of uv.lock pins")
    args = parser.parse_args(argv)
    build(args.target or host_target(), do_smoke=not args.no_smoke, use_lock=not args.no_lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
