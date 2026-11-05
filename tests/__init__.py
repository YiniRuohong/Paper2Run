"""Project test package for shared fixtures imported across suites.

Keeping an explicit package makes both ``pytest`` and ``python -m pytest``
resolve ``tests.integration`` to this repository rather than a dependency.
"""
