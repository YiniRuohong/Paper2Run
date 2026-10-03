from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from p2r_codegen import CodegenError, audit_payload
from p2r_persistence.models import AttemptRecord, RepairProposalRecord

_HASH = re.compile(r"[0-9a-f]{64}")


def _hash(value: str, name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _ordinal(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
        raise ValueError("repair_ordinal must be an integer from 1 through 5")
    return value


def create_attempt(db: Session, *, run_id: str, attempt_index: int, source: str,
                   status: str = "candidate", repair_ordinal: int | None = None,
                   base_code_hash: str, patch_hash: str | None = None,
                   validation_json: dict[str, Any] | None = None,
                   code_manifest_json: dict[str, Any] | None = None,
                   error_json: dict[str, Any] | None = None) -> AttemptRecord:
    if source not in {"template", "model", "repair"}:
        raise ValueError("invalid attempt source")
    if status not in {"candidate", "proposed", "validated", "rejected"}:
        raise ValueError("invalid attempt status")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 0:
        raise ValueError("attempt_index must be non-negative")
    if repair_ordinal is not None:
        _ordinal(repair_ordinal)
    _hash(base_code_hash, "base_code_hash")
    if patch_hash is not None:
        _hash(patch_hash, "patch_hash")
    row = AttemptRecord(run_id=run_id, attempt_index=attempt_index, repair_ordinal=repair_ordinal,
                        source=source, status=status, base_code_hash=base_code_hash,
                        patch_hash=patch_hash, validation_json=validation_json or {},
                        code_manifest_json=code_manifest_json or {}, error_json=error_json)
    db.add(row)
    db.flush()
    return row


def get_attempt_for_run(db: Session, *, run_id: str, attempt_id: str) -> AttemptRecord:
    row = db.scalar(select(AttemptRecord).where(AttemptRecord.id == attempt_id, AttemptRecord.run_id == run_id))
    if row is None:
        raise ValueError("attempt does not belong to run")
    return row


def create_repair_proposal(db: Session, *, run_id: str, attempt_id: str, repair_ordinal: int,
                           base_code_hash: str, patch_hash: str,
                           proposal_json: dict[str, Any], validation: dict[str, Any],
                           status: str = "proposed") -> RepairProposalRecord:
    attempt = get_attempt_for_run(db, run_id=run_id, attempt_id=attempt_id)
    ordinal = _ordinal(repair_ordinal)
    if status not in {"proposed", "accepted", "rejected", "needs_revision"}:
        raise ValueError("invalid repair proposal status")
    if attempt.source != "repair":
        raise ValueError("repair proposal requires a repair attempt")
    if attempt.repair_ordinal is not None and attempt.repair_ordinal != ordinal:
        raise ValueError("proposal ordinal does not match attempt")

    base = _hash(base_code_hash, "base_code_hash")
    patch = _hash(patch_hash, "patch_hash")
    if not isinstance(proposal_json, dict):
        raise ValueError("proposal_json must be an object")
    try:
        audited = audit_payload(source="repair", base_hash=base, patch_hash=patch,
                                validation=validation, repair_ordinal=ordinal)
    except (CodegenError, TypeError) as exc:
        raise ValueError(str(exc)) from exc
    # Stored proposal data is deliberately a closed audit schema; arbitrary
    # model output (and any secret-like fields) never crosses this boundary.
    if proposal_json != audited:
        raise ValueError("proposal_json must equal the closed repair audit payload")
    if attempt.base_code_hash != base:
        raise ValueError("proposal base hash does not match attempt")
    if db.scalar(select(RepairProposalRecord).where(RepairProposalRecord.attempt_id == attempt_id,
                                                    RepairProposalRecord.repair_ordinal == ordinal)):
        raise ValueError("repair ordinal already proposed for attempt")
    row = RepairProposalRecord(run_id=run_id, attempt_id=attempt_id, repair_ordinal=ordinal,
                               base_code_hash=base, patch_hash=patch,
                               proposal_json=audited, status=status)
    db.add(row)
    db.flush()
    return row


def list_repair_proposals(db: Session, *, run_id: str) -> list[RepairProposalRecord]:
    return list(db.scalars(select(RepairProposalRecord).where(RepairProposalRecord.run_id == run_id)
                            .order_by(RepairProposalRecord.created_at, RepairProposalRecord.id)).all())
