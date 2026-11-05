import copy
import hashlib
import json

import pytest

from p2r_contracts.autonomous import freeze_protocol
from p2r_evaluation.autonomous import evaluate_artifacts
from p2r_evaluation.reference_verification import (
    REFERENCE_VALUES_VERSION,
    VERIFICATION_VERSION,
    reference_values_schema,
    validate_reference_values,
    verify_reference_values,
)


SOURCE = "https://example.org/paper/table-1"
SCOPE = "dataset-v1/test"


def references(**changes):
    row = {"metric": "accuracy", "value": 0.5, "tolerance": 0.0,
           "direction": "maximize", "source_url": SOURCE, "scope": SCOPE}
    row.update(changes)
    return {"schema_version": REFERENCE_VALUES_VERSION, "references": [row]}


@pytest.fixture
def experiment(tmp_path):
    (tmp_path / "predictions.json").write_text(json.dumps([
        {"id": "a", "prediction": 1}, {"id": "b", "prediction": 0}]))
    (tmp_path / "labels.json").write_text(json.dumps([
        {"id": "a", "label": 1}, {"id": "b", "label": 1}]))
    protocol = {
        "objective": "compare selected metric", "mode": "comparable",
        "method": "reference", "entrypoint": "train.py",
        "evidence": [SOURCE], "data_sources": ["dataset:v1"],
        "split_hashes": {"test": "a" * 64},
        "evaluation": {"adapter": "accuracy", "reference_value": 0.5,
                       "tolerance": 0.0, "direction": "maximize"},
        "provenance": {
            "labels_sha256": hashlib.sha256((tmp_path / "labels.json").read_bytes()).hexdigest(),
            "labels_independently_frozen": True, "code_sha256": "b" * 64,
            "environment_sha256": "c" * 64},
        "budget": {"wall_time_seconds": 60, "max_model_calls": 10,
                   "max_model_tokens": 1000, "storage_bytes": 1024}}
    return protocol, tmp_path.resolve()


def verify(experiment, document=None, **kwargs):
    protocol, root = experiment
    kwargs.setdefault("comparison_custody_verified", True)
    return verify_reference_values(
        freeze_protocol(protocol), root, references() if document is None else document,
        scope=SCOPE, user_confirmed_sources=True, **kwargs)


def test_schema_is_versioned_strict_and_detached():
    document = references()
    parsed = validate_reference_values(json.dumps(document))
    assert parsed == document
    parsed["references"][0]["value"] = 9
    assert document["references"][0]["value"] == 0.5
    schema = reference_values_schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["schema_version"]["const"] == REFERENCE_VALUES_VERSION
    assert set(schema["$defs"]["ReferenceValue"]["required"]) == {
        "metric", "value", "tolerance", "direction", "source_url", "scope"}


@pytest.mark.parametrize("field,value", [
    ("value", float("nan")), ("value", float("inf")),
    ("value", -float("inf")), ("value", True), ("value", "0.5"),
    ("value", 10**400), ("tolerance", -1), ("tolerance", float("nan")),
    ("tolerance", float("inf")), ("tolerance", False),
    ("direction", "higher"), ("source_url", ""), ("source_url", "model says 0.5"),
    ("source_url", "file:///tmp/score"), ("source_url", "https://"),
    ("metric", " "), ("scope", " "),
])
def test_invalid_reference_fields(field, value):
    with pytest.raises(ValueError):
        validate_reference_values(references(**{field: value}))


@pytest.mark.parametrize("mutation", [
    lambda d: d.pop("schema_version"),
    lambda d: d.update(schema_version="paper2run.reference_values.v99"),
    lambda d: d.update(model_response="accuracy=0.5"),
    lambda d: d["references"][0].pop("source_url"),
    lambda d: d["references"].append(copy.deepcopy(d["references"][0])),
])
def test_invalid_document_structure(mutation):
    document = references()
    mutation(document)
    with pytest.raises(ValueError):
        validate_reference_values(document)


def test_rejects_duplicate_json_keys_and_model_prose():
    for text in ['{"schema_version":"x","schema_version":"y","references":[]}',
                 'The accuracy is 0.5', '```json\n{}\n```']:
        with pytest.raises(ValueError):
            validate_reference_values(text)


def test_recomputes_metrics_and_retains_existing_evaluator_contract(experiment):
    protocol, root = experiment
    (root / "score.json").write_text('{"accuracy": 1.0}')
    before = copy.deepcopy(protocol)
    report = verify(experiment)
    assert set(report) == {"schema_version", "scope", "verdict", "comparisons", "limitations"}
    assert report["schema_version"] == VERIFICATION_VERSION
    assert report["scope"] == SCOPE
    assert report["verdict"] == "matched"
    row = report["comparisons"][0]
    assert row["observed_value"] == 0.5
    assert row["absolute_difference"] == 0.0
    assert row["within_tolerance"] is True
    assert row["source_url"] == SOURCE
    assert report["limitations"]
    assert protocol == before
    json.dumps(report, allow_nan=False)
    existing = evaluate_artifacts(freeze_protocol(protocol), root)
    assert existing["verdict"] == "comparison_available"
    assert existing["within_tolerance"] is True


@pytest.mark.parametrize("direction", ["maximize", "minimize", "target"])
@pytest.mark.parametrize("reference,tolerance,verdict", [
    (0.75, 0.25, "matched"), (0.75, 0.249, "outside_tolerance"),
    (0.25, 0.0, "outside_tolerance"),
])
def test_absolute_tolerance_boundary_all_directions(experiment, direction, reference, tolerance, verdict):
    protocol, _ = experiment
    protocol["evaluation"].update(reference_value=reference, tolerance=tolerance,
                                  direction="maximize" if direction == "target" else direction)
    report = verify(experiment, references(value=reference, tolerance=tolerance, direction=direction))
    assert report["verdict"] == verdict


@pytest.mark.parametrize("field", ["evidence", "data_sources", "split_hashes",
                                  "labels_sha256", "labels_independently_frozen",
                                  "code_sha256", "environment_sha256"])
def test_missing_independent_evidence_never_matches(experiment, field):
    protocol, _ = experiment
    target = protocol if field in protocol else protocol["provenance"]
    target.pop(field)
    report = verify(experiment)
    assert report["verdict"] == "not_comparable"
    assert any(field in reason for reason in report["limitations"])


@pytest.mark.parametrize("mode", ["exploratory", "reduced", "original"])
def test_noncomparable_mode_never_matches(experiment, mode):
    experiment[0]["mode"] = mode
    assert verify(experiment)["verdict"] == "not_comparable"


def test_confirmation_cannot_come_from_reference_document(experiment):
    protocol, root = experiment
    report = verify_reference_values(freeze_protocol(protocol), root, references(), scope=SCOPE)
    assert report["verdict"] == "not_comparable"
    assert any("user" in reason for reason in report["limitations"])
    with pytest.raises(ValueError):
        verify_reference_values(freeze_protocol(protocol), root, references(),
                                scope=SCOPE, user_confirmed_sources="true")


def test_source_must_be_in_frozen_evidence(experiment):
    report = verify(experiment, references(source_url="https://example.org/another-paper"))
    assert report["verdict"] == "not_comparable"


@pytest.mark.parametrize("document", [
    {"schema_version": REFERENCE_VALUES_VERSION, "references": []},
    references(metric="unavailable"), references(metric="total", value=2),
    references(scope="another-dataset/test"),
])
def test_absent_or_incomparable_reference(experiment, document):
    assert verify(experiment, document)["verdict"] == "not_comparable"


def test_missing_document_and_frozen_reference(experiment):
    protocol, root = experiment
    report = verify_reference_values(freeze_protocol(protocol), root, None,
                                    scope=SCOPE, user_confirmed_sources=True)
    assert report["verdict"] == "not_comparable"
    protocol["evaluation"].pop("reference_value")
    assert verify(experiment)["verdict"] == "not_comparable"


def test_changed_reference_requires_refreezing(experiment):
    assert verify(experiment, references(tolerance=1.0))["verdict"] == "not_comparable"


def test_partial_metric_coverage_never_claims_full_match(experiment):
    document = references()
    document["references"].append(references(metric="mae")["references"][0])
    report = verify(experiment, document)
    assert report["verdict"] == "not_comparable"
    assert len(report["comparisons"]) == 2


def test_missing_artifact_and_tampered_labels(experiment):
    protocol, root = experiment
    (root / "predictions.json").unlink()
    assert verify(experiment)["verdict"] == "not_comparable"
    (root / "predictions.json").write_text('[{"id":"a","prediction":1}]')
    (root / "labels.json").write_text('[{"id":"a","label":1}]')
    with pytest.raises(ValueError, match="labels differ"):
        verify(experiment)


@pytest.mark.parametrize("metric,expected", [("mae", 0.5), ("rmse", (0.5)**0.5)])
def test_regression_metrics_are_independently_recomputed(experiment, metric, expected):
    protocol, _ = experiment
    protocol["evaluation"].update(adapter="regression", metric=metric,
                                  reference_value=expected, tolerance=1e-12, direction="minimize")
    report = verify(experiment, references(metric=metric, value=expected, tolerance=1e-12,
                                          direction="minimize"))
    assert report["verdict"] == "matched"
    assert report["comparisons"][0]["observed_value"] == pytest.approx(expected)


def test_other_scopes_do_not_affect_selected_scope(experiment):
    document = references()
    document["references"].append(references(scope="different/test", value=99)["references"][0])
    report = verify(experiment, document)
    assert report["verdict"] == "matched"
    assert len(report["comparisons"]) == 1


def test_extreme_finite_difference_remains_valid_json(experiment):
    protocol, root = experiment
    (root / "predictions.json").write_text('[{"id":"a","prediction":1e308}]')
    (root / "labels.json").write_text('[{"id":"a","label":0}]')
    protocol["provenance"]["labels_sha256"] = hashlib.sha256((root / "labels.json").read_bytes()).hexdigest()
    protocol["evaluation"].update(adapter="regression", metric="mae",
                                  reference_value=-1e308, direction="minimize")
    report = verify(experiment, references(metric="mae", value=-1e308, direction="minimize"))
    assert report["verdict"] == "outside_tolerance"
    assert report["comparisons"][0]["absolute_difference"] is None
    assert report["comparisons"][0]["within_tolerance"] is False
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("custody", [False, None])
def test_unverified_custody_never_matches(experiment, custody):
    protocol, root = experiment
    kwargs = {} if custody is None else {"comparison_custody_verified": custody}
    report = verify_reference_values(freeze_protocol(protocol), root, references(),
                                    scope=SCOPE, user_confirmed_sources=True, **kwargs)
    assert report["verdict"] == "not_comparable"
    assert report["comparisons"][0]["within_tolerance"] is None
    assert any("custody" in reason for reason in report["limitations"])


@pytest.mark.parametrize("custody", ["true", 1, None])
def test_custody_requires_explicit_boolean(experiment, custody):
    with pytest.raises(ValueError, match="custody"):
        verify(experiment, comparison_custody_verified=custody)


def test_tampered_envelope_is_rejected(experiment):
    protocol, root = experiment
    frozen = freeze_protocol(protocol)
    frozen["protocol"]["objective"] = "tampered"
    with pytest.raises(ValueError, match="integrity"):
        verify_reference_values(frozen, root, references(), scope=SCOPE, user_confirmed_sources=True)
