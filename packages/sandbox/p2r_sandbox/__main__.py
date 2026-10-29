"""Command-line interface: ``python -m p2r_sandbox``.

Two subcommands:

- ``run SCRIPT [--timeout S] [--max-memory-mb N] [--collect GLOB ...]
  [--json]`` - execute ``SCRIPT`` under the host-isolation stack and report
  the :class:`~p2r_sandbox.sandbox.SandboxResult`. With ``--json`` a single
  stable JSON object (sorted keys) is printed to stdout containing the
  backend name, ``network_isolated``, ``timed_out``, ``exit_code``,
  decoded ``stdout``/``stderr`` and the list of collected file names.
- ``capabilities [--json]`` - print the live-probed isolation capabilities
  of this host (key: value lines, or one JSON object with ``--json``).

The CLI depends only on this package (stdlib + ``p2r_sandbox`` itself) and
never imports other ``p2r_*`` packages, so the sandbox directory can be
dropped onto ``PYTHONPATH`` (or pip-installed from ``packages/sandbox``)
and used standalone. The CLI exit code mirrors the guest: 0 on success,
the guest's positive exit code, 1 for kills/timeouts, 2 for usage or
launch failures.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .sandbox import SandboxLimits, SandboxUnavailable, run_python_sandbox, sandbox_capabilities


def _result_to_payload(result) -> dict:
    """Stable JSON-serialisable view of a SandboxResult."""
    return {
        "backend": result.backend,
        "duration_ms": result.duration_ms,
        "exit_code": result.exit_code,
        "files": sorted(result.files),
        "network_isolated": result.network_isolated,
        "stderr": result.stderr.decode("utf-8", errors="replace"),
        "stdout": result.stdout.decode("utf-8", errors="replace"),
        "timed_out": result.timed_out,
        "truncated": result.truncated,
    }


def _guest_exit_code(result) -> int:
    """Map a SandboxResult exit code onto a CLI exit code."""
    code = result.exit_code
    if code == 0:
        return 0
    if code is not None and code > 0:
        return code
    return 1  # killed by signal / unknown


def _run_command(args: argparse.Namespace) -> int:
    try:
        with open(args.script, "r", encoding="utf-8") as fh:
            code = fh.read()
    except OSError as exc:
        print(f"error: cannot read script {args.script!r}: {exc}", file=sys.stderr)
        return 2
    try:
        limits = SandboxLimits(
            max_seconds=args.timeout,
            max_memory_mb=args.max_memory_mb,
        )
    except ValueError as exc:
        print(f"error: invalid limits: {exc}", file=sys.stderr)
        return 2
    try:
        result = run_python_sandbox(code, limits=limits, collect_files=tuple(args.collect))
    except SandboxUnavailable as exc:
        print(f"error: sandbox unavailable: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_result_to_payload(result), sort_keys=True))
    else:
        sys.stdout.write(result.stdout.decode("utf-8", errors="replace"))
        stderr_text = result.stderr.decode("utf-8", errors="replace")
        if stderr_text and not stderr_text.endswith("\n"):
            stderr_text += "\n"
        sys.stderr.write(stderr_text)
        payload = _result_to_payload(result)
        print(
            "[p2r_sandbox-cli] backend={backend} exit_code={exit_code} "
            "timed_out={timed_out} network_isolated={network_isolated} "
            "files={n_files} duration_ms={duration_ms}".format(
                n_files=len(payload["files"]), **payload
            ),
            file=sys.stderr,
        )
    return _guest_exit_code(result)


def _capabilities_command(args: argparse.Namespace) -> int:
    capabilities = sandbox_capabilities()
    if args.json:
        print(json.dumps(capabilities, sort_keys=True))
    else:
        for key in sorted(capabilities):
            print(f"{key}: {str(capabilities[key]).lower()}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m p2r_sandbox",
        description="Run untrusted Python under host isolation, or report "
        "this host's sandbox capabilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser(
        "run", help="run a script inside the sandbox"
    )
    run_parser.add_argument("script", help="path to the Python script to run")
    run_parser.add_argument(
        "--timeout", type=float, default=60.0, metavar="S",
        help="wall-clock limit in seconds (default: 60)",
    )
    run_parser.add_argument(
        "--max-memory-mb", type=int, default=512, metavar="N",
        help="resident-memory limit over the whole process tree, in MiB "
        "(default: 512)",
    )
    run_parser.add_argument(
        "--collect", nargs="+", default=[], metavar="GLOB",
        help="workdir-relative file names or top-level glob patterns to "
        "collect after the run (e.g. --collect '*.json')",
    )
    run_parser.add_argument(
        "--json", action="store_true",
        help="print the full result as one stable JSON object",
    )
    run_parser.set_defaults(func=_run_command)

    caps_parser = subparsers.add_parser(
        "capabilities", help="report this host's isolation capabilities"
    )
    caps_parser.add_argument(
        "--json", action="store_true", help="print one JSON object"
    )
    caps_parser.set_defaults(func=_capabilities_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
