"""Run evidence registration against the existing ArtifactStore.

Evidence entries are the runner's real output, content-addressed through the
same store as every other artifact and registered on the Run's event log.
Nothing here invents bytes: the only input is the result the runner actually
returned for this run.
"""
from __future__ import annotations

import hashlib
from typing import Any
from sqlalchemy import select

from p2r_persistence.models import RunEventRecord
from .export_builder import canonical_json

EVIDENCE_EVENT_TYPE = "run.evidence.registered"
EVIDENCE_MEDIA_TYPES = frozenset({"application/json", "text/plain"})
_EVIDENCE_KEYS = frozenset({"run_id", "sha256", "bytes", "media_type"})


def register_run_output(run_id: str, runner_result: Any, store) -> list[dict[str, Any]]:
    """Content-address the runner's real output and return its registration entry."""
    output = canonical_json(runner_result)
    address = store.put_bytes(output)
    return [{
        "run_id": run_id,
        "sha256": address.sha256,
        "bytes": address.bytes,
        "media_type": "application/json",
    }]


def _validated_entry(entry: Any, run_id: str) -> dict[str, Any]:
    if not isinstance(entry, dict) or _EVIDENCE_KEYS != frozenset(entry):
        raise ValueError("REPORT_INPUT_INVALID")
    if entry["run_id"] != run_id:
        raise ValueError("REPORT_INPUT_INVALID")
    sha256 = entry["sha256"]
    if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("REPORT_INPUT_INVALID")
    nbytes = entry["bytes"]
    if type(nbytes) is not int or nbytes < 0:
        raise ValueError("REPORT_INPUT_INVALID")
    media_type = entry["media_type"]
    if not isinstance(media_type, str) or media_type not in EVIDENCE_MEDIA_TYPES:
        raise ValueError("REPORT_INPUT_INVALID")
    return {"run_id": entry["run_id"], "sha256": sha256, "bytes": nbytes, "media_type": media_type}


def read_registered_entries(db, run_id: str, store) -> list[dict[str, Any]]:
    """Return this Run's registered evidence after re-verifying store bytes.

    Runs completed before registration (or without a store) legitimately have
    no entries; a present but malformed registration fails the report build
    instead of being silently frozen.
    """
    event = db.scalar(
        select(RunEventRecord)
        .where(RunEventRecord.run_id == run_id, RunEventRecord.type == EVIDENCE_EVENT_TYPE)
        .order_by(RunEventRecord.seq.desc())
    )
    if event is None:
        return []
    payload = event.payload_json
    raw = payload.get("evidence") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("REPORT_INPUT_INVALID")
    entries = []
    for item in raw:
        entry = _validated_entry(item, run_id)
        content = store.open_bytes(entry["sha256"])
        if hashlib.sha256(content).hexdigest() != entry["sha256"] or len(content) != entry["bytes"]:
            raise ValueError("REPORT_INPUT_INVALID")
        entries.append(entry)
    return entries
