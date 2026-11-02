"""Offline PDF -> Markdown through the isolated mineru-kit venv."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Callable

from .errors import DocParseError
from .markers import ChunkOutcome, ParseResult, merge_outcomes
from .pdf import count_pages
from .proc import CompletedRun, mineru_kit, run_tree, venv_python  # noqa: F401  (re-exported)

_TEXT_FIELDS = ("text", "table_body", "latex")
_PROXY_NAMES = {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}

# mineru-kit 4.1.3 `-f zip` layout: markdown.md, middle_json.json, structured_content.json
# and images/ at the top level of <output>/<input stem>.zip.
_STRUCTURED_MEMBER = "structured_content.json"
_MARKDOWN_MEMBER = "markdown.md"
_MAX_MEMBER_BYTES = 32 * 1024 * 1024
_MAX_MEMBERS = 5000
# Page furniture that is not paper content. Footnotes stay: they carry real text.
_FURNITURE_TYPES = frozenset({"header", "footer", "page_number", "aside_text"})
_TITLE_TYPES = {"doc_title": 1, "paragraph_title": 2}
_IMAGE_MARKDOWN = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_BLANK_RUNS = re.compile(r"\n{3,}")
_FALLBACK_WARNING = "未能按页定位内容，引用页码只能精确到全文"


def _base_env(root: Path) -> dict[str, str]:
    kit_dir = str(mineru_kit(root).parent)
    if sys.platform == "win32":
        sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows")
        env = {"PATH": os.pathsep.join([kit_dir, rf"{sysroot}\System32", sysroot])}
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE"):
            if key in os.environ:
                env[key] = os.environ[key]
    else:
        env = {"PATH": os.pathsep.join([kit_dir, os.defpath])}
        for key in ("HOME", "TMPDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
    env["PYTHONUTF8"] = "1"
    env["MINERU_HOME"] = str(root / "mineru-home")
    env["MINERU_TELEMETRY_DISABLED"] = "1"
    return env


def offline_env(root: Path) -> dict[str, str]:
    env = _base_env(root)
    env["HF_HUB_OFFLINE"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def install_env(root: Path) -> dict[str, str]:
    """Like offline_env but lets pip and model downloads reach the network."""
    env = _base_env(root)
    for key, value in os.environ.items():
        if key.upper().startswith("PIP_") or key.lower() in _PROXY_NAMES:
            env[key] = value
    return env


def _item_text(item: dict) -> str | None:
    for field in _TEXT_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_content_list(path: Path, page_count: int) -> list[ChunkOutcome] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, list) or not data or not all(isinstance(i, dict) for i in data):
        return None
    pages: dict[int, list[str]] = {}
    for item in data:
        idx = item.get("page_idx")
        if not isinstance(idx, int) or isinstance(idx, bool):
            return None
        text = _item_text(item)
        if text is not None and 0 <= idx < page_count:
            pages.setdefault(idx + 1, []).append(text)
    return [ChunkOutcome(p, p, "\n\n".join(texts)) for p, texts in sorted(pages.items())]


def _text_of(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _block_text(block: dict) -> str | None:
    """One structured_content block as Markdown text; images are never embedded."""
    kind = block.get("type")
    if kind in _FURNITURE_TYPES:
        return None
    parts: list[str] = []
    content = _text_of(block.get("content"))
    if content:
        if kind in _TITLE_TYPES:
            level = block.get("level")
            if not isinstance(level, int) or isinstance(level, bool):
                level = _TITLE_TYPES[kind]
            content = "#" * min(max(level, 1), 6) + " " + " ".join(content.split())
        elif kind == "equation":
            content = f"$$\n{content}\n$$"
        parts.append(content)
    for key in ("captions", "footnotes"):
        extra = block.get(key)
        for sub in extra if isinstance(extra, list) else []:
            text = _text_of(sub.get("content")) if isinstance(sub, dict) else ""
            if text:
                parts.append(text)
    return "\n\n".join(parts) or None


def _structured_outcomes(raw: bytes, page_count: int) -> list[ChunkOutcome] | None:
    """One outcome per page that has text; None when the JSON is not usable."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        return None
    pages_raw = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages_raw, list):
        return None
    pages: dict[int, list[str]] = {}
    for page in pages_raw:
        if not isinstance(page, dict):
            return None
        idx, blocks = page.get("page_idx"), page.get("blocks")
        if not isinstance(idx, int) or isinstance(idx, bool) or not isinstance(blocks, list):
            return None
        if not 0 <= idx < page_count:
            continue
        for block in blocks:
            text = _block_text(block) if isinstance(block, dict) else None
            if text:
                pages.setdefault(idx + 1, []).append(text)
    if not pages:
        return None
    return [ChunkOutcome(p, p, "\n\n".join(texts)) for p, texts in sorted(pages.items())]


def _markdown_outcomes(text: str, page_count: int) -> list[ChunkOutcome] | None:
    cleaned = _BLANK_RUNS.sub("\n\n", _IMAGE_MARKDOWN.sub("", text)).strip()
    return [ChunkOutcome(1, page_count, cleaned)] if cleaned else None


def _safe_member_name(name: str) -> bool:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        return False
    if len(name) > 1 and name[1] == ":" and name[0].isalpha():
        return False
    parts = name.split("/")
    if parts[-1] == "":  # directory entry
        parts = parts[:-1]
    return bool(parts) and all(part not in ("", "..") for part in parts)


def _read_zip(path: Path, page_count: int) -> tuple[list[ChunkOutcome], bool]:
    """Read the two text members in memory. Nothing from the archive touches the disk.
    The flag is True when only the whole-document Markdown was usable."""
    wanted: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > _MAX_MEMBERS:
                raise DocParseError("DOCPARSE_OFFLINE_FAILED")
            for info in infos:
                if not _safe_member_name(info.filename) or info.file_size > _MAX_MEMBER_BYTES:
                    raise DocParseError("DOCPARSE_OFFLINE_FAILED")
            for info in infos:
                if info.filename not in (_STRUCTURED_MEMBER, _MARKDOWN_MEMBER):
                    continue
                if info.filename in wanted:
                    raise DocParseError("DOCPARSE_OFFLINE_FAILED")
                with archive.open(info) as member:
                    data = member.read(_MAX_MEMBER_BYTES + 1)
                if len(data) > _MAX_MEMBER_BYTES:
                    raise DocParseError("DOCPARSE_OFFLINE_FAILED")
                wanted[info.filename] = data
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError,
            NotImplementedError, zlib.error):
        raise DocParseError("DOCPARSE_OFFLINE_FAILED") from None
    structured = wanted.get(_STRUCTURED_MEMBER)
    if structured is not None:
        outcomes = _structured_outcomes(structured, page_count)
        if outcomes:
            return outcomes, False
    markdown = wanted.get(_MARKDOWN_MEMBER)
    if markdown is not None:
        try:
            outcomes = _markdown_outcomes(markdown.decode("utf-8"), page_count)
        except UnicodeError:
            outcomes = None
        if outcomes:
            return outcomes, True
    raise DocParseError("DOCPARSE_OFFLINE_FAILED")


def _read_output(out_dir: Path, page_count: int) -> tuple[list[ChunkOutcome], bool]:
    """Turn mineru-kit output into per-page outcomes or one whole-document segment.
    The real `-f zip` archive is read first; plain-directory content-list JSON and
    Markdown stay as fallbacks. The flag is True for the whole-document Markdown fallback. Kept in one place so the layout can be adjusted."""
    archives = sorted(out_dir.rglob("*.zip"))
    if archives:
        return _read_zip(archives[0], page_count)
    for path in sorted(out_dir.rglob("*.json")):
        outcomes = _read_content_list(path, page_count)
        if outcomes:
            return outcomes, False
    for path in sorted(out_dir.rglob("*.md")):
        try:
            outcomes = _markdown_outcomes(path.read_text(encoding="utf-8"), page_count)
        except (OSError, UnicodeError):
            continue
        if outcomes:
            return outcomes, True
    raise DocParseError("DOCPARSE_OFFLINE_FAILED")


def _span(first: int, last: int) -> str:
    return f"第 {first} 页" if first == last else f"第 {first}–{last} 页"


def _chunks_and_warnings(outcomes: list[ChunkOutcome], page_count: int) -> tuple[list[dict], list[str]]:
    """Contiguous done/missing ranges over 1..page_count, one warning per missing range."""
    have = {p for o in outcomes for p in range(o.first_page, o.last_page + 1)}
    chunks: list[dict] = []
    for page in range(1, page_count + 1):
        status = "done" if page in have else "missing"
        if chunks and chunks[-1]["status"] == status:
            chunks[-1]["last_page"] = page
        else:
            chunks.append({"first_page": page, "last_page": page, "status": status})
    warnings = [f"{_span(c['first_page'], c['last_page'])}未提取到文字（可能是空白页或纯图页）"
                for c in chunks if c["status"] == "missing"]
    return chunks, warnings


def parse_offline(pdf: bytes, *, root: Path, tier: str, version: str, timeout: float = 1200.0,
                  run: Callable[..., CompletedRun] = run_tree,
                  cancelled: Callable[[], bool] = lambda: False) -> ParseResult:
    root = Path(root).resolve()
    page_count = count_pages(pdf)
    with tempfile.TemporaryDirectory(prefix="p2r-offline-") as tmp:
        work = Path(tmp)
        in_pdf, out_dir = work / "in.pdf", work / "out"
        in_pdf.write_bytes(pdf)
        out_dir.mkdir()
        argv = [str(mineru_kit(root)), "parse", str(in_pdf), "-o", str(out_dir), "-f", "zip",
                "--tier", tier]
        try:
            done = run(argv, env=offline_env(root), cwd=work, timeout=timeout, cancelled=cancelled)
        except OSError:
            raise DocParseError("DOCPARSE_OFFLINE_FAILED") from None
        if getattr(done, "cancelled", False):
            raise DocParseError("DOCPARSE_CANCELLED")
        if done.timed_out:
            raise DocParseError("DOCPARSE_TIMEOUT")
        if done.returncode != 0:
            raise DocParseError("DOCPARSE_OFFLINE_FAILED")
        outcomes, whole_document = _read_output(out_dir, page_count)
    if whole_document:
        pages_parsed = page_count
        chunks = [{"first_page": 1, "last_page": page_count, "status": "done"}]
        warnings = [_FALLBACK_WARNING]
    else:
        chunks, warnings = _chunks_and_warnings(outcomes, page_count)
        pages_parsed = sum(c["last_page"] - c["first_page"] + 1 for c in chunks if c["status"] == "done")
    return ParseResult(
        markdown=merge_outcomes(outcomes), backend="offline_mineru", backend_version=version,
        page_count=page_count, pages_parsed=pages_parsed, chunks=chunks, warnings=warnings,
    )
