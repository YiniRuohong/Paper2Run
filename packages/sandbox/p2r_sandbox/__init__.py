"""p2r_sandbox: the Docker execution target for autonomous experiments (Paper2Run).

The only public API is :mod:`p2r_sandbox.autonomous` (``ExecutionTarget``,
``ExecutionError``): one dedicated container per target configuration, reached
through the local Docker CLI or a strict OpenSSH alias. The package is
standalone (stdlib only) and never imports other ``p2r_*`` packages.
"""
