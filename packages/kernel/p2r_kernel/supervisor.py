from __future__ import annotations

import multiprocessing as mp
from pathlib import Path

from .protocol import ExecuteCellRequest, ExecuteCellResult
from .worker import EMPTY_NAMESPACE_HASH, entrypoint


class KernelSupervisor:
    """Reusable, synchronous supervisor with a fresh namespace on every call.

    Workspace files persist, Python objects do not. Avoiding object transport
    avoids pickle-based code execution in the parent and unbounded snapshots.
    This is a first local defense, NOT a production hostile-code sandbox.
    """

    def __init__(self, workspace: str | Path):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")

    def execute(self, request: ExecuteCellRequest) -> ExecuteCellResult:
        # Revalidate even when a caller used model_construct or mutated a model.
        request = ExecuteCellRequest.model_validate(request.model_dump())
        context = mp.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(target=entrypoint, args=(request.source, str(self.workspace), child))
        started = False
        try:
            process.start()
            started = True
            child.close()
            # Drain before join: joining a child blocked on a full pipe deadlocks.
            if not parent.poll(request.timeout_seconds):
                return ExecuteCellResult(namespace_hash=EMPTY_NAMESPACE_HASH, error_code="CELL_TIMEOUT")
            try:
                result = ExecuteCellResult.model_validate_json(parent.recv_bytes(maxlength=4 * 1024 * 1024))
            except (EOFError, OSError, ValueError):
                return ExecuteCellResult(namespace_hash=EMPTY_NAMESPACE_HASH, error_code="CELL_ERROR")
            return result
        finally:
            parent.close()
            child.close()
            if started:
                if process.is_alive():
                    process.terminate()
                process.join(0.5)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
