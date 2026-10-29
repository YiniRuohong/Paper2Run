"""p2r_sandbox: isolated local execution of untrusted Python (Paper2Run).

Public contract (fixed across the sandbox / codegen / runner tasks):

- :class:`SandboxLimits` - per-run resource limits
- :class:`SandboxResult` - outcome of one run (``backend`` names the
  isolation backend actually used: "seatbelt" / "bwrap" / "unshare" / "none")
- :func:`run_python_sandbox` - execute guest code under host isolation
- :func:`sandbox_capabilities` - live-probed capability report; legacy keys
  ``{"seatbelt", "rlimits", "platform_walls"}`` keep their semantics, newer
  keys (``sensitive_read_denied``, ``bubblewrap``, ``unshare_net``) are
  additive only
- :func:`validate_output_files` - shape-check predictions/labels, returns
  ``(predictions, labels)`` or raises :class:`SandboxOutputError`
- :class:`SandboxUnavailable` - sandbox could not be established at all

Command line: ``python -m p2r_sandbox run script.py [--json]`` and
``python -m p2r_sandbox capabilities [--json]``. The package is standalone
(zero third-party dependencies) and never imports other ``p2r_*`` packages.

Isolation layers and their honest degradation matrix are documented in
:mod:`p2r_sandbox.sandbox`.
"""

from .errors import SandboxError, SandboxOutputError, SandboxUnavailable
from .sandbox import (
    MAX_COLLECT_FILE_BYTES,
    RLIMIT_FSIZE_BYTES,
    SandboxLimits,
    SandboxResult,
    run_python_sandbox,
    sandbox_capabilities,
)
from .validation import validate_output_files

__all__ = [
    "MAX_COLLECT_FILE_BYTES",
    "RLIMIT_FSIZE_BYTES",
    "SandboxError",
    "SandboxLimits",
    "SandboxOutputError",
    "SandboxResult",
    "SandboxUnavailable",
    "run_python_sandbox",
    "sandbox_capabilities",
    "validate_output_files",
]
