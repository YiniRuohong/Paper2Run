"""Lenient normalization of model output before extraction validation.

Models without native structured output drift from the contract in
predictable ways: extra keys, a bare string where an evidence object is
expected, one object where a list is expected, missing provenance
identifiers. This module maps such output onto the ``ExtractionDocument``
shape *before* the authoritative pydantic validation runs.

Honesty rules — normalization never upgrades evidence:
- unknown keys are dropped, or kept under ``value`` when they are the only
  content of an evidence object;
- a value without an evidence status becomes ``inferred``; an absent field
  becomes ``missing``; nothing is ever marked ``supported`` here;
- a candidate experiment without a valid status becomes ``review_required``;
- a source ref lacking ``paper_id``/``pdf_sha256`` gets the current paper's
  identifiers, but a ref naming a *different* paper is left as-is so the
  provenance check still rejects it; a ref without a quote is dropped;
- every change is reported back so the caller can surface it as a warning.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA_VERSION = "article-extraction.v1"
_EVIDENCE_STATUSES = {"supported", "inferred", "missing", "conflicting"}
_SUPPORT_STATUSES = {"supported", "review_required", "unsupported"}
_EVIDENCE_KEYS = {"value", "evidence_status", "source_refs"}
_SPEC_KEYS = {
    "candidate_id", "recipe_id", "recipe_version", "support_status", "parameters",
    "required_dependencies", "required_datasets", "resource_requirements",
    "estimated_runtime", "source_refs",
}
_REF_KEYS = {"id", "paper_id", "quote", "page", "section", "table", "row", "bbox", "pdf_sha256"}
_TOP_KEYS = {
    "schema_version", "title", "task", "methods", "formulas", "datasets",
    "candidate_experiments", "warnings", "limitations", "source_refs",
}
_MAX_NOTES = 12


class _Notes:
    def __init__(self) -> None:
        self.items: list[str] = []

    def add(self, text: str) -> None:
        if text not in self.items:
            self.items.append(text)

    def summary(self) -> str | None:
        if not self.items:
            return None
        shown = "; ".join(self.items[:_MAX_NOTES])
        more = len(self.items) - _MAX_NOTES
        suffix = f"; and {more} more" if more > 0 else ""
        return f"Model output was normalized leniently before validation: {shown}{suffix}."


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _scalar_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _str_list(value: Any, path: str, notes: _Notes) -> list[str]:
    if value is not None and not isinstance(value, list):
        notes.add(f"{path} wrapped into a list")
    out: list[str] = []
    for item in _as_list(value):
        if item is None:
            continue
        if not isinstance(item, str):
            notes.add(f"{path} items converted to text")
        out.append(_scalar_text(item))
    return out


def _ref_ids(value: Any, path: str, notes: _Notes) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, str) and item:
            out.append(item)
        elif isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]:
            out.append(item["id"])
            notes.add(f"{path} objects reduced to their ids")
        elif item is not None:
            notes.add(f"{path} entries without an id dropped")
    return out


def _evidence(value: Any, path: str, notes: _Notes) -> dict[str, Any]:
    if value is None:
        notes.add(f"{path} absent, marked missing")
        return {"value": None, "evidence_status": "missing", "source_refs": []}
    if not isinstance(value, dict):
        notes.add(f"{path} given as a bare value, marked inferred")
        return {"value": value, "evidence_status": "inferred", "source_refs": []}
    extra = {key: value[key] for key in value if key not in _EVIDENCE_KEYS and key not in ("source_ref",)}
    if "value" in value:
        payload = value["value"]
        if extra:
            notes.add(f"{path} unknown fields dropped ({', '.join(sorted(extra))})")
    elif extra:
        payload = extra
        notes.add(f"{path} fields ({', '.join(sorted(extra))}) kept under value")
    else:
        payload = None
    status = value.get("evidence_status")
    if status not in _EVIDENCE_STATUSES:
        status = "inferred" if payload is not None else "missing"
        notes.add(f"{path} evidence_status missing or invalid, set to {status}")
    refs = value.get("source_refs", value.get("source_ref"))
    return {"value": payload, "evidence_status": status, "source_refs": _ref_ids(refs, f"{path}.source_refs", notes)}


def _optional_text(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        text = str(value).lower()
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = _scalar_text(value)
    return text[:limit] if limit else text


def _spec(value: Any, index: int, notes: _Notes) -> dict[str, Any] | None:
    path = f"candidate_experiments[{index}]"
    if not isinstance(value, dict):
        notes.add(f"{path} is not an object, dropped")
        return None
    value = dict(value)
    # Aliases models commonly use for contract fields (the API exposes the
    # status as "execution_support"); the canonical name always wins.
    for alias, field in (("execution_support", "support_status"), ("id", "candidate_id"),
                         ("source_ref", "source_refs")):
        if alias in value and field not in value:
            value[field] = value.pop(alias)
            notes.add(f"{path}.{alias} read as {field}")
    if value.get("support_status") == "requires_review":
        value["support_status"] = "review_required"
    extra = sorted(key for key in value if key not in _SPEC_KEYS)
    if extra:
        notes.add(f"{path} unknown fields dropped ({', '.join(extra)})")
    candidate_id = value.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        candidate_id = f"candidate-{index + 1}"
        notes.add(f"{path} candidate_id generated")
    status = value.get("support_status")
    if status not in _SUPPORT_STATUSES:
        status = "review_required"
        notes.add(f"{path} support_status missing or invalid, set to review_required")
    parameters = value.get("parameters")
    resources = value.get("resource_requirements")
    if parameters is not None and not isinstance(parameters, dict):
        notes.add(f"{path}.parameters is not an object, cleared")
    if resources is not None and not isinstance(resources, dict):
        notes.add(f"{path}.resource_requirements is not an object, cleared")
    return {
        "candidate_id": candidate_id[:128],
        "recipe_id": _optional_text(value.get("recipe_id")),
        "recipe_version": _optional_text(value.get("recipe_version")),
        "support_status": status,
        "parameters": parameters if isinstance(parameters, dict) else {},
        "required_dependencies": _str_list(value.get("required_dependencies"), f"{path}.required_dependencies", notes),
        "required_datasets": _str_list(value.get("required_datasets"), f"{path}.required_datasets", notes),
        "resource_requirements": resources if isinstance(resources, dict) else {},
        "estimated_runtime": _optional_text(value.get("estimated_runtime")),
        "source_refs": _ref_ids(value.get("source_refs"), f"{path}.source_refs", notes),
    }


def _page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 1:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit() and int(value) >= 1:
        return int(value)
    return None


def _bbox(value: Any) -> list[float] | None:
    if (isinstance(value, list) and len(value) == 4
            and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value)):
        return [float(item) for item in value]
    return None


def _source_ref(value: Any, index: int, paper_id: str, pdf_sha256: str, notes: _Notes) -> dict[str, Any] | None:
    path = f"source_refs[{index}]"
    if not isinstance(value, dict):
        notes.add(f"{path} is not an object, dropped")
        return None
    value = dict(value)
    for alias in ("text", "excerpt", "snippet"):
        if alias in value and "quote" not in value:
            value["quote"] = value.pop(alias)
            notes.add(f"{path}.{alias} read as quote")
    quote = value.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        notes.add(f"{path} has no quote, dropped")
        return None
    extra = sorted(key for key in value if key not in _REF_KEYS)
    if extra:
        notes.add(f"{path} unknown fields dropped ({', '.join(extra)})")
    ref_id = value.get("id")
    if not isinstance(ref_id, str) or not ref_id.strip():
        ref_id = f"ref-{index + 1}"
        notes.add(f"{path} id generated")
    ref_paper = value.get("paper_id")
    if not isinstance(ref_paper, str) or not ref_paper.strip():
        ref_paper = paper_id
        notes.add(f"{path} paper_id filled from the current paper")
    ref_sha = value.get("pdf_sha256")
    if not isinstance(ref_sha, str) or not ref_sha.strip():
        ref_sha = pdf_sha256
        notes.add(f"{path} pdf_sha256 filled from the current paper")
    page = _page(value.get("page"))
    if value.get("page") is not None and page is None:
        notes.add(f"{path} invalid page dropped")
    return {
        "id": ref_id[:128],
        "paper_id": ref_paper,
        "quote": quote[:4000],
        "page": page,
        "section": _optional_text(value.get("section"), 256),
        "table": _optional_text(value.get("table"), 256),
        "row": _optional_text(value.get("row"), 256),
        "bbox": _bbox(value.get("bbox")),
        "pdf_sha256": ref_sha.strip(),
    }


def normalize_extraction_payload(payload: dict[str, Any], *, paper_id: str, pdf_sha256: str) -> dict[str, Any]:
    """Return a contract-shaped copy of ``payload`` with a normalization warning.

    Validation, provenance checks and recipe gating still run afterwards on
    the returned document; this only removes formatting drift.
    """
    notes = _Notes()
    extra = sorted(key for key in payload if key not in _TOP_KEYS)
    if extra:
        notes.add(f"unknown top-level fields dropped ({', '.join(extra)})")
    if payload.get("schema_version") not in (None, SCHEMA_VERSION):
        notes.add("schema_version corrected")
    candidates = [
        spec for index, item in enumerate(_as_list(payload.get("candidate_experiments")))
        if (spec := _spec(item, index, notes)) is not None
    ]
    refs = [
        ref for index, item in enumerate(_as_list(payload.get("source_refs")))
        if (ref := _source_ref(item, index, paper_id, pdf_sha256.lower(), notes)) is not None
    ]
    document: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "title": _evidence(payload.get("title"), "title", notes),
        "task": _evidence(payload.get("task"), "task", notes),
        "methods": [_evidence(item, f"methods[{i}]", notes) for i, item in enumerate(_as_list(payload.get("methods")))],
        "formulas": [_evidence(item, f"formulas[{i}]", notes) for i, item in enumerate(_as_list(payload.get("formulas")))],
        "datasets": [_evidence(item, f"datasets[{i}]", notes) for i, item in enumerate(_as_list(payload.get("datasets")))],
        "candidate_experiments": candidates,
        "warnings": _str_list(payload.get("warnings"), "warnings", notes),
        "limitations": _str_list(payload.get("limitations"), "limitations", notes),
        "source_refs": refs,
    }
    summary = notes.summary()
    if summary is not None:
        document["warnings"].append(summary)
    return document
