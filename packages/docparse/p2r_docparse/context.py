from __future__ import annotations

import re

from .markers import _MARKER

_REFERENCES = re.compile(
    r"^(#{1,6})\s*(?:\d+\.?\s*)?(?:references|bibliography|参考文献)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_HEADING = re.compile(r"^(#{1,6})\s", re.MULTILINE)


def _drop_references(markdown: str) -> str:
    while True:
        m = _REFERENCES.search(markdown)
        if m is None:
            return markdown
        level = len(m.group(1))
        end = len(markdown)
        for h in _HEADING.finditer(markdown, m.end()):
            if len(h.group(1)) <= level:
                end = h.start()
                break
        # Page markers inside the removed span must survive for page accounting.
        kept = "".join(k.group(0) + "\n" for k in _MARKER.finditer(markdown, m.end(), end))
        markdown = markdown[: m.start()] + kept + markdown[end:]


def trim_for_context(markdown: str, max_chars: int) -> tuple[str, str | None]:
    text = _drop_references(markdown)
    if len(text) <= max_chars:
        return text, None
    cut = text[:max_chars]
    markers = list(_MARKER.finditer(text))
    # A page range counts as sent only if its whole segment (up to the next marker) fits.
    last_page = None
    for i, m in enumerate(markers[:-1] if markers else []):
        if markers[i + 1].start() > max_chars:
            break
        if m.group("missing"):
            continue  # a failed range was never in the text
        last_page = int(m.group("b") or m.group("p"))
    if last_page is None:
        return cut, "文本超出上下文上限，已截断"
    return cut, f"仅第 1–{last_page} 页发送给模型（超出上下文上限）"
