from __future__ import annotations

import pytest
from pydantic import ValidationError

from p2r_orchestration.article_schema import (
    EvidenceField,
    ExperimentSpec,
    ExtractionDocument,
    SourceRef,
    execution_eligible,
)


def evidence(value, status="supported", refs=()):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def extraction_payload(**changes):
    payload = {
        "schema_version": "article-extraction.v1",
        "title": evidence(None, "missing"),
        "task": evidence("classification", "supported", ["ref-1"]),
        "methods": [],
        "formulas": [],
        "datasets": [],
        "candidate_experiments": [],
        "warnings": [],
        "limitations": [],
        "source_refs": [
            {"id": "ref-1", "paper_id": "paper-1", "quote": "classify images", "page": 1,
             "pdf_sha256": "A" * 64},
        ],
    }
    payload.update(changes)
    return payload


def experiment_payload(**changes):
    payload = {
        "candidate_id": "candidate-1",
        "recipe_id": "baseline",
        "recipe_version": "1",
        "support_status": "supported",
        "parameters": {"seed": 7},
        "required_dependencies": ["numpy"],
        "required_datasets": ["sample-data"],
        "resource_requirements": {"cpu": 2},
        "estimated_runtime": "10m",
        "source_refs": ["ref-1"],
    }
    payload.update(changes)
    return payload


def test_extraction_keeps_missing_fields_and_source_refs():
    value = ExtractionDocument.model_validate(extraction_payload())

    assert value.title.value is None
    assert value.title.evidence_status == "missing"
    assert value.task.source_refs == ["ref-1"]
    assert value.source_refs[0].id == "ref-1"
    assert value.source_refs[0].pdf_sha256 == "a" * 64


def test_schema_is_frozen_and_rejects_unknown_fields():
    value = ExtractionDocument.model_validate(extraction_payload())

    assert value.model_config["frozen"] is True
    assert value.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        value.schema_version = "article-extraction.v2"
    with pytest.raises(ValidationError):
        ExtractionDocument.model_validate(extraction_payload(extra_field=True))


def test_evidence_status_is_versioned_literal():
    assert EvidenceField.model_validate(
        evidence("value", "conflicting")
    ).evidence_status == "conflicting"
    for status in ("conflicted", "conflicting-old"):
        with pytest.raises(ValidationError):
            EvidenceField.model_validate(evidence("value", status))


def test_source_ref_accepts_missing_or_valid_pdf_sha256_and_rejects_invalid_digest():
    assert SourceRef.model_validate({
        "id": "ref-2", "paper_id": "paper-1", "quote": "unlocated"
    }).pdf_sha256 is None
    assert SourceRef.model_validate({
        "id": "ref-3", "paper_id": "paper-1", "quote": "located",
        "pdf_sha256": "A" * 64,
    }).pdf_sha256 == "a" * 64

    for digest in ("a" * 63, "z" * 64):
        with pytest.raises(ValidationError):
            SourceRef.model_validate({
                "id": "ref-4", "paper_id": "paper-1", "quote": "located",
                "pdf_sha256": digest,
            })


def test_experiment_rejects_unknown_support_status():
    with pytest.raises(ValidationError):
        ExperimentSpec.model_validate(experiment_payload(support_status="maybe"))


def test_experiment_preserves_required_fields_without_autofilling_recipe():
    value = ExperimentSpec.model_validate(experiment_payload(recipe_id=None, recipe_version=None))

    assert value.recipe_id is None
    assert value.recipe_version is None
    assert execution_eligible(value) is False


def test_execution_eligible_requires_supported_nonempty_recipe_identity():
    supported = ExperimentSpec.model_validate(experiment_payload())

    assert execution_eligible(supported) is True
    assert execution_eligible(ExperimentSpec.model_validate(
        experiment_payload(support_status="review_required")
    )) is False
    assert execution_eligible(ExperimentSpec.model_validate(
        experiment_payload(support_status="unsupported")
    )) is False
    assert execution_eligible(ExperimentSpec.model_validate(
        experiment_payload(recipe_id="")
    )) is False
    assert execution_eligible(ExperimentSpec.model_validate(
        experiment_payload(recipe_version="   ")
    )) is False
