"""Exception hierarchy for the p2r_sandbox package."""


class SandboxError(Exception):
    """Base class for every error raised by p2r_sandbox."""


class SandboxUnavailable(SandboxError):
    """The sandbox could not be established at all (launch/preexec failure).

    Raised when the child process cannot be spawned or when the preexec
    resource-limit setup fails unexpectedly. Never raised for failures that
    happen *inside* the sandboxed script - those are reported through
    ``SandboxResult.exit_code`` / ``stderr``.
    """


class SandboxOutputError(SandboxError):
    """A collected output file is missing or has an invalid shape.

    Raised by :func:`p2r_sandbox.validate_output_files` when
    ``predictions.json`` / ``labels.json`` are absent, not valid UTF-8 JSON,
    or do not match the agreed schema.
    """
