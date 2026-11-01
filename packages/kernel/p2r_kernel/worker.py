"""First-layer local restrictions only; NOT a production hostile-code sandbox.

Each request starts a fresh namespace. No arbitrary objects are pickled back to
 the supervisor. OS isolation/resource quotas are required for untrusted tenants.
"""
from __future__ import annotations

import ast
import builtins
import hashlib
import io
import json
import math
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .protocol import MAX_OUTPUT

EMPTY_NAMESPACE_HASH = hashlib.sha256(b"{}").hexdigest()


class ImportNotAllowed(Exception):
    pass


class RestrictedOperation(Exception):
    pass


class BoundedOutput:
    """Internal capture state, never exposed through the cell's sys facade."""

    def __init__(self):
        self.buffered = bytearray()
        self.exceeded = False

    def write(self, value: str) -> int:
        # Encode only a bounded prefix, even when user code prints a huge string.
        encoded = value[:MAX_OUTPUT + 1].encode("utf-8", "replace")
        remaining = MAX_OUTPUT - len(self.buffered)
        self.exceeded |= len(encoded) > remaining or len(value) > MAX_OUTPUT
        self.buffered.extend(encoded[:remaining])
        return len(value)

    def getvalue(self) -> str:
        return self.buffered.decode("utf-8", "ignore")


class OutputFacade(io.TextIOBase):
    """Only stream operations are public; quota state is not exposed."""

    __slots__ = ("_output",)

    def __init__(self, output: BoundedOutput):
        self._output = output

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"limit", "buffered", "exceeded"}:
            return
        super().__setattr__(name, value)

    def write(self, value: str) -> int:
        return self._output.write(value)

    def flush(self) -> None:
        pass


def run_cell(source: str, workspace: str) -> dict[str, Any]:
    stdout, stderr = BoundedOutput(), BoundedOutput()
    stdout_facade, stderr_facade = OutputFacade(stdout), OutputFacade(stderr)
    root = Path(workspace).resolve()
    # sys is a facade, never the real process module (no modules/env/path access).
    allowed = {"math": math, "sys": SimpleNamespace(stdout=stdout_facade, stderr=stderr_facade)}

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in allowed:
            raise ImportNotAllowed()
        return allowed[name]

    def workspace_open(file, mode="r", *, encoding="utf-8"):
        if not isinstance(file, str) or mode not in {"r", "w", "a", "x", "rb", "wb", "ab", "xb"}:
            raise RestrictedOperation()
        path = (root / file).resolve()
        if not path.is_relative_to(root):
            raise RestrictedOperation()
        return builtins.open(path, mode, encoding=None if "b" in mode else encoding)

    names = (
        "abs all any bool bytes chr dict divmod enumerate filter float format "
        "frozenset int isinstance issubclass iter len list map max min next ord "
        "pow print range repr reversed round set slice sorted str sum tuple zip "
        "Exception ValueError TypeError RuntimeError ZeroDivisionError"
    ).split()
    safe = {name: getattr(builtins, name) for name in names}
    safe.update({"__import__": restricted_import, "open": workspace_open})
    namespace: dict[str, Any] = {"__builtins__": safe}
    error_code = None
    summary = ""
    try:
        tree = ast.parse(source, filename="<cell>", mode="exec")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                raise RestrictedOperation()
            if isinstance(node, ast.Name) and node.id.startswith("__"):
                raise RestrictedOperation()
        last = tree.body.pop() if tree.body and isinstance(tree.body[-1], ast.Expr) else None
        with redirect_stdout(stdout_facade), redirect_stderr(stderr_facade):
            exec(compile(tree, "<cell>", "exec"), namespace, namespace)
            if last is not None:
                value = eval(compile(ast.Expression(last.value), "<cell>", "eval"), namespace, namespace)
                if value is not None:
                    summary = repr(value).encode("utf-8", "replace")[:16 * 1024].decode("utf-8", "ignore")
    except ImportNotAllowed:
        error_code = "IMPORT_NOT_ALLOWED"
    except RestrictedOperation:
        error_code = "OPERATION_NOT_ALLOWED"
    except BaseException:
        # Exception text and tracebacks can contain paths, secrets and user data.
        error_code = "CELL_ERROR"
    if stdout.exceeded or stderr.exceeded:
        error_code = "OUTPUT_LIMIT"
    # This is a diagnostic fingerprint, not a recoverable checkpoint. Hash repr
    # incrementally rather than emitting namespace values across the boundary.
    digest = hashlib.sha256()
    for key in sorted(namespace):
        if key.startswith("__"):
            continue
        value = namespace[key]
        digest.update(key.encode())
        digest.update(type(value).__name__.encode())
        if type(value) in {str, int, float, bool, list, tuple, dict, set, bytes, type(None)}:
            digest.update(repr(value).encode("utf-8", "replace"))
    namespace_hash = digest.hexdigest() if len(namespace) > 1 else EMPTY_NAMESPACE_HASH
    return dict(stdout=stdout.getvalue(), stderr=stderr.getvalue(), result_summary=summary,
                namespace_hash=namespace_hash, error_code=error_code)


def entrypoint(source: str, workspace: str, connection) -> None:
    try:
        # JSON, not pickle: execution results cannot invoke parent-side reducers.
        try:
            connection.send_bytes(json.dumps(run_cell(source, workspace)).encode())
        except (BrokenPipeError, OSError, TypeError, ValueError):
            # Sending happens across a best-effort local pipe; this is not a
            # production hostile-code sandbox and file access remains unchanged.
            pass
    finally:
        try:
            connection.close()
        except OSError:
            pass
