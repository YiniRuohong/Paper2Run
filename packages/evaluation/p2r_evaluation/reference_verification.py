"""User-sourced references -> independent, scope-limited numerical verification.

Only trusted control-plane callers may supply ``frozen``, ``scope`` and
``user_confirmed_sources``. A URL or hash does not prove authorship: the caller
must obtain explicit user confirmation of the reference values and their sources,
never infer it from model prose or an agent-authored JSON document. Sources must
also occur verbatim in the independently frozen protocol's evidence list. No
network requests, model-response extraction, or experiment-supplied code run here.

The reference-values / independent-verification artifact separation is a design
convention only; this module includes no OpenAI4S did-arxiv code or GPL data.

``matched`` means selected numbers are inside an absolute tolerance, not that a
paper was reproduced. Direction is descriptive (as in evaluate_artifacts), not a
one-sided success threshold. ``target`` uses that same absolute-distance rule;
it does not alter the existing protocol's maximize/minimize contract.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, StringConstraints, field_validator, model_validator

from p2r_contracts.autonomous import StrictModel
from p2r_evaluation.autonomous import evaluate_artifacts

REFERENCE_VALUES_VERSION = "paper2run.reference_values.v1"
VERIFICATION_VERSION = "paper2run.independent_verification.v1"
Text = Annotated[str, StringConstraints(min_length=1, max_length=8192)]


class ReferenceValue(StrictModel):
    metric: Text
    value: float = Field(allow_inf_nan=False)
    tolerance: float = Field(ge=0, allow_inf_nan=False)
    direction: Literal["maximize", "minimize", "target"]
    source_url: Text
    scope: Text

    @field_validator("value", "tolerance", mode="before")
    @classmethod
    def finite_number(cls, value: Any) -> Any:
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("reference numbers must be finite JSON numbers, not booleans")
        return value

    @field_validator("metric", "scope")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("metric and scope must be nonblank without surrounding whitespace")
        return value

    @field_validator("source_url")
    @classmethod
    def explicit_source(cls, value: str) -> str:
        try:
            url = urlsplit(value)
            valid = (url.scheme in ("https", "http") and url.hostname
                     and url.username is None and url.password is None
                     and not any(char.isspace() or ord(char) < 32 for char in value))
            _ = url.port
        except ValueError as exc:
            raise ValueError("source_url must be an explicit HTTP(S) source URL") from exc
        if not valid:
            raise ValueError("source_url must be an explicit HTTP(S) source URL")
        return value


class ReferenceValues(StrictModel):
    schema_version: Literal["paper2run.reference_values.v1"]
    references: list[ReferenceValue]

    @model_validator(mode="after")
    def unique_metrics_per_scope(self) -> ReferenceValues:
        keys = [(row.scope, row.metric) for row in self.references]
        if len(keys) != len(set(keys)):
            raise ValueError("references must have unique (scope, metric) pairs")
        return self


def reference_values_schema() -> dict:
    """Return the versioned JSON Schema, including all required source fields."""
    return ReferenceValues.model_json_schema()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"nonfinite JSON number: {value}")


def validate_reference_values(document: dict | str) -> dict:
    """Validate a JSON object or literal JSON text; never parse model prose.

    Invalid/missing individual fields raise ValueError. An empty references list
    is valid and yields not_comparable when verified. Return a detached snapshot.
    """
    if isinstance(document, str):
        document = json.loads(document, object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
    return ReferenceValues.model_validate(document).model_dump(mode="json")


def verify_reference_values(
    frozen: dict, artifacts_dir: Path, reference_values: dict | str | None, *,
    scope: str, user_confirmed_sources: bool = False,
    comparison_custody_verified: bool = False,
) -> dict:
    """Recompute artifacts using the existing evaluator, then compare references.

    Scope is a caller-supplied, trusted dataset/split/condition identifier. The
    control plane must explicitly attest independent evaluator custody; a user or
    model flag is not such an attestation. The default denies comparison. Only
    references in that exact scope participate. Every requested metric in scope
    must be comparable for an aggregate matched/outside_tolerance verdict; partial
    coverage stays not_comparable. Auxiliary counts (total/correct) are not metrics.

    Values/tolerances must agree with the frozen evaluation snapshot; changes
    require re-freezing through the existing authorization path. For direction,
    target explicitly selects the evaluator's already symmetric distance rule.
    Existing artifact-integrity errors propagate rather than becoming a verdict.
    """
    if not isinstance(scope, str) or not scope.strip() or scope != scope.strip():
        raise ValueError("scope must be a nonblank identifier without surrounding whitespace")
    if type(user_confirmed_sources) is not bool:
        raise ValueError("user_confirmed_sources must be a trusted boolean")
    if type(comparison_custody_verified) is not bool:
        raise ValueError("comparison_custody_verified must be a trusted boolean")
    document = validate_reference_values(reference_values) if reference_values is not None else None
    # Never accept caller-supplied metrics or model-reported scores as evidence.
    evaluation = evaluate_artifacts(frozen, artifacts_dir)
    protocol = frozen["protocol"]
    selected = [row for row in document["references"] if row["scope"] == scope] if document else []
    report = {"schema_version": VERIFICATION_VERSION, "scope": scope,
              "verdict": "not_comparable", "comparisons": [],
              "limitations": [
                  "Numerical agreement within this scope only; not a claim of paper reproduction.",
                  "Source URLs require user confirmation and frozen evidence; they are not independently fetched or authenticated."]}
    limitations = report["limitations"]
    limitations.extend(evaluation["reasons"])
    if not user_confirmed_sources:
        limitations.append("Explicit user confirmation of reference values and source URLs is required; model text is not evidence.")
    if not comparison_custody_verified:
        limitations.append("Independent evaluator custody has not been verified by the control plane.")
    if not selected:
        limitations.append("No reference values available for the requested scope.")
        return report

    settings = protocol["evaluation"]
    metric = "accuracy" if settings["adapter"] == "accuracy" else settings["metric"]
    eligible = (evaluation["comparison_eligible"] and user_confirmed_sources
                and comparison_custody_verified)
    for reference in selected:
        reasons = []
        if not eligible:
            reasons.append("Independent evaluation or user source confirmation is not comparable.")
        if reference["source_url"] not in protocol["evidence"]:
            reasons.append("Reference source_url is missing from independently frozen evidence.")
        observed = evaluation["metrics"].get(reference["metric"]) if reference["metric"] == metric else None
        if observed is None:
            reasons.append("No independently recomputed comparable metric for this reference.")
        if (reference["value"] != settings["reference_value"]
                or reference["tolerance"] != settings["tolerance"]
                or reference["direction"] not in (settings["direction"], "target")):
            reasons.append("Reference value, tolerance or direction differs from frozen evaluation; re-freeze before comparing.")
        comparison = {**reference, "observed_value": observed, "absolute_difference": None,
                      "within_tolerance": None, "verdict": "not_comparable"}
        if not reasons:
            difference = abs(observed - reference["value"])
            # Finite inputs can overflow on subtraction. Such a difference is
            # necessarily beyond a finite tolerance; never emit invalid JSON.
            comparison["absolute_difference"] = difference if math.isfinite(difference) else None
            comparison["within_tolerance"] = difference <= reference["tolerance"]
            comparison["verdict"] = "matched" if comparison["within_tolerance"] else "outside_tolerance"
            if not math.isfinite(difference):
                limitations.append(f"{reference['metric']}: absolute difference exceeds finite JSON numeric range.")
        limitations.extend(f"{reference['metric']}: {reason}" for reason in reasons)
        report["comparisons"].append(comparison)

    verdicts = [row["verdict"] for row in report["comparisons"]]
    if "not_comparable" not in verdicts:
        report["verdict"] = "outside_tolerance" if "outside_tolerance" in verdicts else "matched"
    return report
