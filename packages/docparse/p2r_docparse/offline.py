"""Offline PDF -> Markdown through the isolated mineru-kit venv."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable

from .errors import DocParseError
from .markers import ChunkOutcome, ParseResult, merge_outcomes
from .pdf import count_pages
from .proc import CompletedRun, mineru_kit, run_tree, venv_python  # noqa: F401  (re-exported)

_TEXT_FIELDS = ("text", "table_body", "latex")
_PROXY_NAMES = {"http_proxy", "https_proxy", "no_proxy", "all_proxy"}


def _base_env(root: Path) -> dict[str, str]:
    kit_dir = str(mineru_kit(root).parent)
    if os.name == "nt":
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
    except (OSError, ValueError):
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


def _read_output(out_dir: Path, page_count: int) -> list[ChunkOutcome]:
    """Turn mineru-kit output into per-page outcomes (content-list JSON) or one
    whole-document segment (Markdown). Kept in one place so the layout can be adjusted."""
    for path in sorted(out_dir.rglob("*.json")):
        outcomes = _read_content_list(path, page_count)
        if outcomes:
            return outcomes
    for path in sorted(out_dir.rglob("*.md")):
        try:
            return [ChunkOutcome(1, page_count, path.read_text(encoding="utf-8").strip())]
        except (OSError, UnicodeError):
            continue
    raise DocParseError("DOCPARSE_OFFLINE_FAILED")


def parse_offline(pdf: bytes, *, root: Path, tier: str, version: str, timeout: float = 1200.0,
                  run: Callable[..., CompletedRun] = run_tree,
                  cancelled: Callable[[], bool] = lambda: False) -> ParseResult:
    page_count = count_pages(pdf)
    with tempfile.TemporaryDirectory(prefix="p2r-offline-") as tmp:
        work = Path(tmp)
        in_pdf, out_dir = work / "in.pdf", work / "out"
        in_pdf.write_bytes(pdf)
        out_dir.mkdir()
        argv = [str(mineru_kit(root)), "parse", str(in_pdf), "-o", str(out_dir), "--tier", tier]
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
        outcomes = _read_output(out_dir, page_count)
    markdown = merge_outcomes(outcomes)
    return ParseResult(
        markdown=markdown, backend="offline_mineru", backend_version=version,
        page_count=page_count, pages_parsed=page_count,
        chunks=[{"first_page": 1, "last_page": page_count, "status": "done"}], warnings=[],
    )
