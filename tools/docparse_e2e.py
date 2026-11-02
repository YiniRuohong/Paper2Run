"""Command line end-to-end check for the MinerU PDF parsing feature.

Modes (exactly one per run), each printing exactly ONE line of JSON on stdout:

    --online <pdf>                      parse through the online Agent API
    --offline-install <root>            install the isolated offline MinerU runtime
    --offline <pdf> --root <root>       parse with the installed offline runtime
                    [--timeout <sec>]   hard kill after this many seconds

The parsed Markdown is also written to <results-dir>/<mode>-<pdf stem>.md
(default results dir: ~/p2r-e2e/results). Failures print the same JSON with
ok=false and a fixed error_code, never raw subprocess or server text, and exit
non-zero (1; 2 for bad arguments; 130 when interrupted).

Run it with the repo venv, for example:

    .venv/bin/python -I tools/docparse_e2e.py --online paper.pdf

p2r_docparse is a standalone package, so the script puts its source root on
sys.path itself (the way scripts/launch.py SOURCE_ROOTS does); no PYTHONPATH is
needed, and `-I` (which ignores PYTHONPATH) works.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parents[1]
# p2r_docparse imports no other p2r_* package, so its own root is all this tool needs.
SOURCE_ROOTS = ("packages/docparse",)
for _rel in SOURCE_ROOTS:
    _src = str(ROOT / _rel)
    if _src not in sys.path:
        sys.path.insert(0, _src)

from p2r_docparse.errors import DocParseError  # noqa: E402

DEFAULT_RESULTS_DIR = Path.home() / "p2r-e2e" / "results"
DEFAULT_OFFLINE_TIMEOUT = 1200.0


class _InputMissing(Exception):
    pass


# ---- argument parsing -------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="docparse_e2e", allow_abbrev=False,
                                     description="End-to-end check of MinerU PDF parsing.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--online", metavar="PDF", type=Path, dest="online_pdf",
                       help="parse PDF through the online Agent API")
    group.add_argument("--offline", metavar="PDF", type=Path, dest="offline_pdf",
                       help="parse PDF with the installed offline runtime (needs --root)")
    group.add_argument("--offline-install", metavar="ROOT", type=Path, dest="install_root",
                       help="install the offline MinerU runtime into ROOT")
    parser.add_argument("--root", type=Path, help="offline runtime root (with --offline)")
    parser.add_argument("--timeout", type=float,
                        help=f"offline parse timeout in seconds (default {DEFAULT_OFFLINE_TIMEOUT:g})")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR,
                        help="where the parsed Markdown is written")
    args = parser.parse_args(argv)

    if args.online_pdf is not None:
        args.mode, args.pdf, args.root_given = "online", args.online_pdf, args.root
    elif args.offline_pdf is not None:
        args.mode, args.pdf, args.root_given = "offline", args.offline_pdf, args.root
    else:
        args.mode, args.pdf, args.root_given = "offline-install", None, args.root

    if args.mode == "offline":
        if args.root is None:
            parser.error("--offline requires --root")
        if args.timeout is None:
            args.timeout = DEFAULT_OFFLINE_TIMEOUT
        if not args.timeout > 0:
            parser.error("--timeout must be greater than 0")
    else:
        if args.root_given is not None:
            parser.error("--root is only valid with --offline")
        if args.timeout is not None:
            parser.error("--timeout is only valid with --offline")
        if args.mode == "offline-install":
            args.root = args.install_root
    del args.root_given
    return args


# ---- real backends (imported lazily so the module loads without side effects) --
def _default_online(data: bytes):
    from p2r_docparse.online import OnlineAgentClient, parse_online

    return parse_online(data, client=OnlineAgentClient())


def _default_offline(data: bytes, root: Path, timeout: float):
    from p2r_docparse.install import MINERU_VERSION, OfflineInstaller
    from p2r_docparse.offline import parse_offline

    st = OfflineInstaller(root).state()
    if st.state == "unsupported":
        raise DocParseError("DOCPARSE_OFFLINE_UNSUPPORTED")
    if st.state != "ready":
        raise DocParseError("DOCPARSE_OFFLINE_NOT_READY")
    return parse_offline(data, root=root, tier=st.tier or "basic",
                         version=st.version or MINERU_VERSION, timeout=timeout)


def _default_install(root: Path):
    from p2r_docparse.install import OfflineInstaller

    installer = OfflineInstaller(root)
    st = installer.state()
    if st.state == "unsupported":
        return st, []
    started = time.monotonic()
    log: list[dict[str, Any]] = []
    last: object = object()
    try:
        installer.start()
        while True:
            st = installer.wait(1.0)
            if st.state == "installing" and st.step != last:
                last = st.step
                log.append({"elapsed_seconds": round(time.monotonic() - started, 1),
                            "step": st.step, "progress": st.progress})
            if st.state != "installing":
                break
    except KeyboardInterrupt:
        installer.cancel()
        raise
    log.append({"elapsed_seconds": round(time.monotonic() - started, 1),
                "step": st.state, "progress": st.progress})
    return st, log


_DEFAULT_BACKENDS = {"online": _default_online, "offline": _default_offline,
                     "install": _default_install}


# ---- result document --------------------------------------------------------
def _empty_doc(mode: str) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "mode": mode, "ok": False, "page_count": None, "pages_parsed": None, "chunks": [],
        "warnings": [], "markdown_sha256": None, "markdown_chars": None,
        "elapsed_seconds": 0.0, "backend": None, "backend_version": None, "error_code": None,
    }
    if mode == "offline-install":
        doc.update(state=None, tier=None, version=None, bytes_on_disk=None, step_log=[])
    return doc


def _run_parse(args: argparse.Namespace, fns: dict, doc: dict[str, Any]) -> None:
    if not args.pdf.is_file():
        raise _InputMissing
    data = args.pdf.read_bytes()
    if args.mode == "online":
        result = fns["online"](data)
    else:
        result = fns["offline"](data, args.root, args.timeout)
    markdown = result.markdown
    encoded = markdown.encode("utf-8")
    args.results_dir.mkdir(parents=True, exist_ok=True)
    (args.results_dir / f"{args.mode}-{args.pdf.stem}.md").write_bytes(encoded)
    doc.update(page_count=result.page_count, pages_parsed=result.pages_parsed,
               chunks=result.chunks, warnings=result.warnings,
               markdown_sha256=hashlib.sha256(encoded).hexdigest(),
               markdown_chars=len(markdown), backend=result.backend,
               backend_version=result.backend_version)


def _run_install(args: argparse.Namespace, fns: dict, doc: dict[str, Any]) -> None:
    state, steps = fns["install"](args.root)
    doc.update(state=state.state, tier=state.tier, version=state.version,
               bytes_on_disk=state.bytes_on_disk, step_log=steps)
    if state.state == "unsupported":
        doc["error_code"] = "DOCPARSE_OFFLINE_UNSUPPORTED"
    elif state.state != "ready":
        doc["error_code"] = "E2E_INSTALL_FAILED"  # state.error is deliberately not echoed


def _execute(args: argparse.Namespace, fns: dict) -> dict[str, Any]:
    doc = _empty_doc(args.mode)
    started = time.monotonic()
    try:
        if args.mode == "offline-install":
            _run_install(args, fns, doc)
        else:
            _run_parse(args, fns, doc)
    except DocParseError as e:
        doc["error_code"] = e.code
    except _InputMissing:
        doc["error_code"] = "E2E_INPUT_MISSING"
    except KeyboardInterrupt:
        doc["error_code"] = "E2E_INTERRUPTED"
    except Exception:  # noqa: BLE001 - only a fixed code leaves this tool
        doc["error_code"] = "E2E_UNEXPECTED"
    doc["elapsed_seconds"] = round(time.monotonic() - started, 3)
    doc["ok"] = doc["error_code"] is None
    return doc


def main(argv: list[str] | None = None, *, out: TextIO | None = None,
         backends: dict | None = None) -> int:
    args = parse_args(argv)
    doc = _execute(args, {**_DEFAULT_BACKENDS, **(backends or {})})
    stream = out if out is not None else sys.stdout
    stream.write(json.dumps(doc) + "\n")
    stream.flush()
    if doc["ok"]:
        return 0
    return 130 if doc["error_code"] == "E2E_INTERRUPTED" else 1


if __name__ == "__main__":
    sys.exit(main())
