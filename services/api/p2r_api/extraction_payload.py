"""Pure mapping from ExtractionService's safe projection to ExtractionRecord payloads.

The provider-backed path (`ExtractionService.extract`) returns a *safe
projection*: validated `ExtractionDocument` content plus provider metadata, and
never a raw provider response, header, usage record, or secret. This module is
the only place that projection is turned into the persisted `payload_json`
shape shared by the deterministic fixture path and `GET /papers/{id}/extraction`.

No I/O, no request context — only the projection and the current paper identity.
`payload_json` never carries a secret or raw response: `provider` is the
allow-listed metadata block from the safe projection.
"""
from __future__ import annotations

from typing import Any

# Fixed keys of ExtractionRecord.payload_json (schema_version "1.0").
PAYLOAD_KEYS = (
    "extraction_id", "paper_id", "schema_version", "fields", "methods",
    "formulas", "candidate_experiments", "source_refs", "warnings", "provider",
)
# Only these keys may be copied from the safe projection into payload_json["provider"];
# this is what keeps a secret or raw response out of the payload by construction.
PROJECTION_METADATA_KEYS = (
    "provider_config_id", "model_id", "prompt_version", "policy_version",
    "content_sha256", "context_hash",
    # PDF papers only: which parsed-text artifact was sent and which backend made it.
    "source_text_sha256", "parse_backend",
)

# article_schema.ExtractionDocument field names that belong in payload_json["fields"].
_DOCUMENT_FIELDS = ("title", "task", "datasets")
# SourceRef columns of p2r_persistence.models.SourceRefRecord (payload side has no `id`).
_SOURCE_REF_KEYS = ("paper_id", "pdf_sha256", "page", "section", "table", "row", "quote", "bbox")


class ExtractionPayloadError(ValueError):
    """The safe projection is inconsistent with the current paper (refused, nothing persisted)."""


def _copy_list(items: list[Any]) -> list[Any]:
    return [dict(item) if isinstance(item, dict) else item for item in items]


def _validate_document(document: dict, *, paper_id: str, paper_sha256: str) -> None:
    """Provenance gate over the projection: every source_ref names this paper
    and this paper's artifact digest. Raises ExtractionPayloadError otherwise."""
    if not isinstance(document, dict):
        raise ExtractionPayloadError("extraction document must be an object")
    expected = (paper_sha256 or "").lower()
    if not expected:
        raise ExtractionPayloadError("paper sha256 is required")
    refs = document.get("source_refs")
    if not isinstance(refs, list):
        raise ExtractionPayloadError("source_refs must be a list")
    for ref in refs:
        if not isinstance(ref, dict):
            raise ExtractionPayloadError("source_ref must be an object")
        if ref.get("paper_id") != paper_id:
            raise ExtractionPayloadError("source_ref references a different paper")
        sha = ref.get("pdf_sha256")
        if not isinstance(sha, str) or sha.lower() != expected:
            raise ExtractionPayloadError("source_ref pdf_sha256 does not match the paper")


def build_extraction_payload(
    projection: dict[str, Any],
    *,
    paper_id: str,
    paper_sha256: str,
    extraction_id: str | None = None,
) -> dict[str, Any]:
    """Map ExtractionService's safe projection onto ExtractionRecord.payload_json.

    Raises ExtractionPayloadError when the projection does not belong to this
    paper (wrong paper_id, or a source_ref naming another paper or another
    artifact digest); the caller must not persist anything in that case.
    """
    if not isinstance(projection, dict):
        raise ExtractionPayloadError("projection must be an object")
    if projection.get("paper_id") != paper_id:
        raise ExtractionPayloadError("projection belongs to a different paper")

    document = projection.get("extraction")
    _validate_document(document, paper_id=paper_id, paper_sha256=paper_sha256)

    fields: dict[str, Any] = {}
    for name in _DOCUMENT_FIELDS:
        item = document.get(name)
        if isinstance(item, dict):
            fields[name] = {
                "value": item.get("value"),
                "evidence_status": item.get("evidence_status"),
                "source_refs": list(item.get("source_refs") or []),
            }
    # Deterministic PDF fixture convention: optimizer is a task-profile field.
    optimizer = document.get("optimizer")
    if isinstance(optimizer, dict):
        fields["optimizer"] = {
            "value": optimizer.get("value"),
            "evidence_status": optimizer.get("evidence_status"),
            "source_refs": list(optimizer.get("source_refs") or []),
        }

    methods = [
        {"id": f"mth_{index + 1}", "label": str(item.get("value")) if item.get("value") is not None else "",
         "evidence_status": item.get("evidence_status"), "source_refs": list(item.get("source_refs") or [])}
        for index, item in enumerate(document.get("methods") or []) if isinstance(item, dict)
    ]
    formulas = [
        {"id": f"eq_{index + 1}", "label": str(item.get("value")) if item.get("value") is not None else "",
         "evidence_status": item.get("evidence_status"), "source_refs": list(item.get("source_refs") or [])}
        for index, item in enumerate(document.get("formulas") or []) if isinstance(item, dict)
    ]

    support_map = {"supported": "supported", "review_required": "requires_review", "unsupported": "unsupported"}
    candidates = []
    for spec in document.get("candidate_experiments") or []:
        if not isinstance(spec, dict):
            continue
        unsupported = [] if spec.get("support_status") == "supported" else [f"support_status: {spec.get('support_status')}"]
        candidates.append({
            "id": spec.get("candidate_id"),
            "candidate_id": spec.get("candidate_id"),
            "label": spec.get("candidate_id"),
            "recipe_id": spec.get("recipe_id"),
            "recipe_version": spec.get("recipe_version"),
            "support_status": spec.get("support_status"),
            "execution_support": support_map.get(spec.get("support_status"), "requires_review"),
            "unsupported_reasons": unsupported,
            "parameters": dict(spec.get("parameters") or {}),
            "required_dependencies": _copy_list(spec.get("required_dependencies") or []),
            "required_datasets": _copy_list(spec.get("required_datasets") or []),
            "resource_requirements": dict(spec.get("resource_requirements") or {}),
            "estimated_runtime": spec.get("estimated_runtime"),
            "source_refs": _copy_list(spec.get("source_refs") or []),
            "provenance": "article_extraction",
        })

    source_refs = [
        {key: ref.get(key) for key in _SOURCE_REF_KEYS}
        for ref in document.get("source_refs") or [] if isinstance(ref, dict)
    ]

    metadata = {key: projection.get(key) for key in PROJECTION_METADATA_KEYS if key in projection}
    return {
        "extraction_id": extraction_id,
        "paper_id": paper_id,
        "schema_version": "1.0",
        "fields": fields,
        "methods": methods,
        "formulas": formulas,
        "candidate_experiments": candidates,
        "source_refs": source_refs,
        "warnings": list(document.get("warnings") or []),
        "provider": metadata,
    }
