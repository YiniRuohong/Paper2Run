"""Build the Paper2Run desktop app: web UI -> bundled runtime -> electron-builder.

Examples::

    python scripts/desktop/build_desktop.py                        # this host
    python scripts/desktop/build_desktop.py --target win32-x64     # from macOS
    python scripts/desktop/build_desktop.py --skip-web --dir       # fast local app

Output goes to ``apps/desktop/release/``. Standard library only; needs pnpm on
PATH (plus Node.js) and network access for the first run.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_runtime  # noqa: E402

ROOT = build_runtime.ROOT
DESKTOP = build_runtime.DESKTOP
WEB_INDEX = ROOT / "apps" / "web" / "dist" / "index.html"
BUILDER_FLAGS = {
    "darwin-arm64": ("--mac", "--arm64"),
    "darwin-x64": ("--mac", "--x64"),
    "win32-x64": ("--win", "--x64"),
}


def run(cmd: list[str], **kwargs) -> None:
    print("[desktop] $ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--target", choices=sorted(BUILDER_FLAGS), default=None, help="default: this host")
    parser.add_argument("--skip-web", action="store_true", help="reuse the existing apps/web/dist")
    parser.add_argument("--skip-runtime", action="store_true", help="reuse the existing apps/desktop/.runtime/<target>")
    parser.add_argument("--dir", action="store_true", help="unpacked app only, no installers")
    args = parser.parse_args(argv)

    target = args.target or build_runtime.host_target()
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        raise SystemExit("pnpm not found on PATH")

    if args.skip_web:
        if not WEB_INDEX.is_file():
            raise SystemExit(f"--skip-web given but {WEB_INDEX} does not exist")
    else:
        run([pnpm, "--dir", "apps/web", "install", "--frozen-lockfile"])
        run([pnpm, "--dir", "apps/web", "build"])

    if args.skip_runtime:
        if not (build_runtime.RUNTIME_DIR / target / "python").is_dir():
            raise SystemExit(f"--skip-runtime given but no runtime for {target}")
    else:
        build_runtime.build(target)

    run([pnpm, "--dir", "apps/desktop", "install", "--frozen-lockfile"])
    builder = [pnpm, "--dir", "apps/desktop", "exec", "electron-builder", *BUILDER_FLAGS[target], "--publish", "never"]
    if args.dir:
        builder.append("--dir")
    # A Developer ID in the keychain must not change what an unsigned build does.
    run(builder, env={**os.environ, "CSC_IDENTITY_AUTO_DISCOVERY": "false"})
    print(f"[desktop] done: {DESKTOP / 'release'}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
