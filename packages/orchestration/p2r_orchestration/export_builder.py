from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any, Iterable


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> bytes:
    if not isinstance(report, dict):
        raise ValueError("REPORT_INVALID")
    limitations = report.get("limitations")
    if not isinstance(limitations, list) or len(limitations) < 3 or not all(isinstance(x, str) for x in limitations):
        raise ValueError("REPORT_LIMITATIONS_MISSING")
    lines = ["# Guided report", "", f"- **Run:** `{_md(report.get('run_id', ''))}`", f"- **Revision:** `{_md(report.get('revision_id', ''))}`", f"- **Metrics source:** `{_md(report.get('metrics_source', ''))}`", "", "## Metrics", "", "| Metric | Value |", "| --- | --- |"]
    metrics = report.get("metrics", {})
    if isinstance(metrics, dict):
        for key in sorted(metrics):
            lines.append(f"| {_md(key)} | {_md(metrics[key])} |")
    lines += ["", "## Limitations", ""]
    lines += [f"- {_md(item)}" for item in limitations]
    return ("\n".join(lines) + "\n").encode("utf-8")


_NAME_RE = re.compile(r"^[^/]+/[0-9a-f]{64}\.txt$")

def _validate_name(name: Any) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("UNSAFE_ARCHIVE_PATH")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("UNSAFE_ARCHIVE_PATH")
    if "/" in name and not name.startswith("evidence/"):
        raise ValueError("UNSAFE_ARCHIVE_PATH")
    return name


def build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    if not isinstance(entries, list):
        raise ValueError("INVALID_ARCHIVE_ENTRIES")
    names: set[str] = set()
    normalized: list[tuple[str, bytes]] = []
    for name, data in entries:
        name = _validate_name(name)
        if not isinstance(data, bytes):
            raise ValueError("INVALID_ARCHIVE_BYTES")
        if name in names:
            raise ValueError("UNSAFE_ARCHIVE_PATH")
        names.add(name)
        normalized.append((name, data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(normalized):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return out.getvalue()


def build_bundle(report: dict[str, Any], manifest: dict[str, Any] | None = None, metrics: dict[str, Any] | None = None, evidence: Iterable[tuple[str, bytes]] = ()) -> bytes:
    manifest = manifest if manifest is not None else report.get("manifest", {})
    metrics = metrics if metrics is not None else report.get("metrics", {})
    base = [("report.json", canonical_json(report)), ("report.md", render_markdown(report)), ("manifest.json", canonical_json(manifest)), ("metrics.json", canonical_json(metrics))]
    evidence_entries: list[tuple[str, bytes]] = []
    for name, data in evidence:
        if not isinstance(name, str) or not name.startswith("evidence/") or not _NAME_RE.fullmatch(name):
            raise ValueError("UNSAFE_ARCHIVE_PATH")
        digest = name.split("/", 1)[1][:-4]
        if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("EVIDENCE_INTEGRITY_FAILED")
        evidence_entries.append((name, data))
    all_entries = base + evidence_entries
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in all_entries}
    all_entries.append(("checksums.json", canonical_json(checksums)))
    return build_zip(all_entries)
