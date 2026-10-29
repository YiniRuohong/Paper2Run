"""Path setup for benchmark unit tests: put tools/benchmark on sys.path.

The benchmark CLI lives outside the pyproject pythonpath entries (it is a
standalone tool, not an installed package), so tests import it by path.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOLS_BENCHMARK_DIR = Path(__file__).resolve().parents[3] / "tools" / "benchmark"
if str(TOOLS_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_BENCHMARK_DIR))
