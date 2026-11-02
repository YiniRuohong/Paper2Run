"""User's parse-mode choice (online API vs offline MinerU), persisted as JSON."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from .atomic import write_json_atomic

Mode = Literal["online", "offline"]
_MODES = ("online", "offline")


class DocParseSettings:
    def __init__(self, root: Path) -> None:
        self._path = Path(root) / "settings.json"

    def mode(self) -> Mode | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("mode") if isinstance(data, dict) else None
        return value if value in _MODES else None

    def set_mode(self, mode: Mode) -> None:
        if mode not in _MODES:
            raise ValueError(f"invalid parse mode: {mode!r}")
        write_json_atomic(self._path, {"mode": mode})
