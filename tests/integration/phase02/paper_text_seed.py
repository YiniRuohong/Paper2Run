"""Test helper: seed a parsed-text record for an uploaded PDF paper.

Provider extraction of a PDF reads the parsed Markdown (PaperTextRecord +
artifact), never the raw PDF bytes. Tests that drive that path seed the
record directly instead of running a real MinerU parse.
"""
from __future__ import annotations

from datetime import datetime

from p2r_persistence.models import PaperRecord, PaperTextRecord


def seed_paper_text(
    app,
    paper_id: str,
    markdown: str,
    *,
    status: str = "ready",
    backend: str = "online_agent",
    warnings: list[str] | None = None,
    source_sha256: str | None = None,
    created_at: datetime | None = None,
) -> str | None:
    """Store `markdown` in the artifact store and add a PaperTextRecord for the
    paper's current content. Returns the record's text_sha256 (None unless ready)."""
    runtime = app.state.runtime
    text_sha256 = None
    if status == "ready":
        text_sha256 = runtime["artifact_store"].put_bytes(markdown.encode("utf-8")).sha256
    with runtime["session_factory"].begin() as db:
        paper = db.get(PaperRecord, paper_id)
        record = PaperTextRecord(
            paper_id=paper_id,
            source_sha256=source_sha256 or paper.sha256,
            text_sha256=text_sha256,
            backend=backend,
            page_count=paper.page_count,
            pages_parsed=paper.page_count if status == "ready" else 0,
            warnings_json=list(warnings or []),
            status=status,
        )
        if created_at is not None:
            record.created_at = created_at
        db.add(record)
    return text_sha256
