"""Selection of a paper's parsed-text record (shared by docparse and extraction routes)."""
from __future__ import annotations

from sqlalchemy import select

from p2r_persistence.models import PaperRecord, PaperTextRecord


def latest_paper_text(db, paper: PaperRecord, *, status: str | None = None) -> PaperTextRecord | None:
    """Newest PaperTextRecord for the paper's *current* content, optionally
    restricted to one status. Records for other content digests never count."""
    query = select(PaperTextRecord).where(
        PaperTextRecord.paper_id == paper.id, PaperTextRecord.source_sha256 == paper.sha256,
    )
    if status is not None:
        query = query.where(PaperTextRecord.status == status)
    return db.scalar(query.order_by(PaperTextRecord.created_at.desc(), PaperTextRecord.id.desc()))
