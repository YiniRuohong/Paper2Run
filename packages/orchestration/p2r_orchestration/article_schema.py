from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

EvidenceStatus = Literal["supported", "inferred", "missing", "conflicting"]
SupportStatus = Literal["supported", "review_required", "unsupported"]


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128)
    paper_id: str = Field(min_length=1, max_length=128)
    quote: str = Field(min_length=1, max_length=4000)
    page: int | None = Field(default=None, ge=1)
    section: str | None = Field(default=None, max_length=256)
    table: str | None = Field(default=None, max_length=256)
    row: str | None = Field(default=None, max_length=256)
    bbox: tuple[float, float, float, float] | None = None
    pdf_sha256: str | None = Field(default=None, min_length=64, max_length=64)

    @field_validator("pdf_sha256")
    @classmethod
    def valid_pdf_sha256(cls, value: str | None) -> str | None:
        if value is not None and (not value.isascii() or any(character not in "0123456789abcdefABCDEF" for character in value)):
            raise ValueError("pdf_sha256 must be a 64-character hexadecimal SHA-256 digest")
        return value.lower() if value is not None else value


class EvidenceField(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Any = None
    evidence_status: EvidenceStatus
    source_refs: list[str] = Field(default_factory=list)


class ExperimentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=128)
    recipe_id: str | None = None
    recipe_version: str | None = None
    support_status: SupportStatus
    parameters: dict[str, Any] = Field(default_factory=dict)
    required_dependencies: list[str] = Field(default_factory=list)
    required_datasets: list[str] = Field(default_factory=list)
    resource_requirements: dict[str, Any] = Field(default_factory=dict)
    estimated_runtime: str | None = None
    source_refs: list[str] = Field(default_factory=list)


class ExtractionDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["article-extraction.v1"] = "article-extraction.v1"
    title: EvidenceField
    task: EvidenceField
    methods: list[EvidenceField] = Field(default_factory=list)
    formulas: list[EvidenceField] = Field(default_factory=list)
    datasets: list[EvidenceField] = Field(default_factory=list)
    candidate_experiments: list[ExperimentSpec] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)


def execution_eligible(spec: ExperimentSpec) -> bool:
    """Return whether a spec has explicit support and a registered recipe identity."""
    return (
        spec.support_status == "supported"
        and bool(spec.recipe_id and spec.recipe_id.strip())
        and bool(spec.recipe_version and spec.recipe_version.strip())
    )
