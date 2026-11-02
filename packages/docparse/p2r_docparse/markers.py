from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

_SEP = "\n\n"
_MARKER = re.compile(
    r"^<!-- p2r:(?:pages (?P<a>\d+)-(?P<b>\d+)(?P<missing> missing)?|page (?P<p>\d+)) -->$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ChunkOutcome:
    first_page: int
    last_page: int
    markdown: str | None  # None = chunk failed


@dataclass(frozen=True)
class ParseResult:
    markdown: str
    backend: Literal["online_agent", "offline_mineru"]
    backend_version: str
    page_count: int
    pages_parsed: int
    chunks: list[dict]  # {"first_page", "last_page", "status": "done" | "missing"}
    warnings: list[str]


def _marker(first: int, last: int, missing: bool) -> str:
    if first == last and not missing:
        return f"<!-- p2r:page {first} -->"
    suffix = " missing" if missing else ""
    return f"<!-- p2r:pages {first}-{last}{suffix} -->"


def merge_outcomes(outcomes: list[ChunkOutcome]) -> str:
    parts = []
    for o in outcomes:
        if o.markdown is None:
            parts.append(_marker(o.first_page, o.last_page, True))
        else:
            parts.append(_marker(o.first_page, o.last_page, False) + "\n" + o.markdown)
    return _SEP.join(parts)


def split_merged(markdown: str) -> list[ChunkOutcome]:
    found = list(_MARKER.finditer(markdown))
    outcomes = []
    for i, m in enumerate(found):
        if m.group("p") is not None:
            first = last = int(m.group("p"))
        else:
            first, last = int(m.group("a")), int(m.group("b"))
        if m.group("missing"):
            outcomes.append(ChunkOutcome(first, last, None))
            continue
        end = found[i + 1].start() if i + 1 < len(found) else len(markdown)
        body = markdown[m.end() + 1 : end]
        if i + 1 < len(found) and body.endswith(_SEP):
            body = body[: -len(_SEP)]
        outcomes.append(ChunkOutcome(first, last, body))
    return outcomes


def missing_warnings(outcomes: list[ChunkOutcome]) -> list[str]:
    out = []
    for o in outcomes:
        if o.markdown is not None:
            continue
        span = str(o.first_page) if o.first_page == o.last_page else f"{o.first_page}–{o.last_page}"
        out.append(f"第 {span} 页解析失败，未包含")
    return out
