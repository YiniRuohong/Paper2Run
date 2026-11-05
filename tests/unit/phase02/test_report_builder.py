import pytest
from p2r_orchestration.report_builder import LIMITATIONS, build_report, report_hash


def snapshot():
    return {"run_id":"run_00000000-0000-0000-0000-000000000001", "revision_id":"spr_00000000-0000-0000-0000-000000000001", "recipe":{"id":"offline_fixture","version":"1"}, "metrics":{"accuracy":1.0,"correct":4,"total":4}, "evaluator_version":"offline-fixture-evaluator-v1", "manifest":{"status":"provisional","manifest_hash":"a"*64}, "evidence":[]}


def test_build_report_preserves_separate_confidence_intervals():
    value = snapshot()
    value["confidence_intervals"] = {"accuracy_95": {"lower": 0.5, "upper": 1.0}}
    report = build_report(value)
    assert report["confidence_intervals"] == value["confidence_intervals"]
    assert report["metrics"] == snapshot()["metrics"]
    report["confidence_intervals"]["accuracy_95"]["lower"] = 0
    assert value["confidence_intervals"]["accuracy_95"]["lower"] == 0.5


@pytest.mark.parametrize("ci", [
    None, [], {}, {"accuracy_95": {"lower": 0, "upper": 1, "secret": "canary"}},
    {"accuracy_95": {"lower": 0, "upper": 1}, "payload": "canary"},
    {"accuracy_95": {"lower": True, "upper": 1}},
    {"accuracy_95": {"lower": "0", "upper": 1}},
    {"accuracy_95": {"lower": float("nan"), "upper": 1}},
    {"accuracy_95": {"lower": 0, "upper": float("inf")}},
    {"accuracy_95": {"lower": -0.1, "upper": 1}},
    {"accuracy_95": {"lower": 0.8, "upper": 0.2}},
    {"accuracy_95": {"lower": 0, "upper": 1.1}},
    {"accuracy_95": {"lower": {"secret": "canary"}, "upper": 1}},
])
def test_build_report_rejects_malformed_confidence_intervals(ci):
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(snapshot() | {"confidence_intervals": ci})


def test_build_report_is_narrow_and_deterministic():
    value = snapshot(); value["secret"] = "canary"
    report = build_report(value)
    assert report == {"schema_version":"guided-report-v1", "run_id":"run_00000000-0000-0000-0000-000000000001", "revision_id":"spr_00000000-0000-0000-0000-000000000001", "recipe":{"id":"offline_fixture","version":"1"}, "metrics":{"accuracy":1.0,"correct":4,"total":4}, "metrics_source":"controlled_fixture_evaluation", "evidence_class":"controlled_smoke", "evaluator_version":"offline-fixture-evaluator-v1", "verdict":"not_comparable", "manifest":{"status":"provisional","manifest_hash":"a"*64}, "evidence":[], "limitations":LIMITATIONS}
    assert "secret" not in report
    assert report_hash(report) == report_hash(build_report(snapshot()))


@pytest.mark.parametrize("field", ["run_id", "revision_id", "recipe", "metrics", "evaluator_version", "manifest", "evidence"])
def test_build_report_rejects_missing_required_snapshot(field):
    value = snapshot(); value.pop(field)
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(value)


@pytest.mark.parametrize("field,value", [
    ("recipe", {"id": "offline_fixture", "version": "1", "token": "secret-canary"}),
    ("recipe", {"id": {"token": "secret-canary"}, "version": "1"}),
    ("recipe", {"id": "secret-canary", "version": "1"}),
    ("metrics", {"accuracy": {"token": "secret-canary"}}),
    ("metrics", {"accuracy": "secret-canary"}),
    ("metrics", {"accuracy": float("nan")}),
    ("manifest", {"status": "secret-canary", "manifest_hash": "a" * 64}),
    ("evidence", [{"id": {"token": "secret-canary"}}]),
])
def test_nested_inputs_never_pass_through(field, value):
    source = snapshot()
    if field == 'metrics':
        source[field].update(value)
    else:
        source[field] = value
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(source)


def test_build_report_rejects_unbounded_types():
    value = snapshot(); value["metrics"] = ["secret"]
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(value)


_EVIDENCE_ENTRY = {
    "run_id": "run_00000000-0000-0000-0000-000000000001",
    "sha256": "b" * 64,
    "bytes": 128,
    "media_type": "application/json",
}


def test_build_report_freezes_registered_evidence_entries():
    value = snapshot(); value["evidence"] = [dict(_EVIDENCE_ENTRY)]
    report = build_report(value)
    assert report["evidence"] == [dict(_EVIDENCE_ENTRY)]
    assert report_hash(report) == report_hash(build_report(snapshot() | {"evidence": [dict(_EVIDENCE_ENTRY)]}))
    # Mutating the frozen copy must not alias the snapshot input.
    report["evidence"][0]["bytes"] = 0
    assert value["evidence"][0]["bytes"] == 128


@pytest.mark.parametrize("mutate", [
    lambda e: e.update(extra_key="secret-canary"),
    lambda e: e.pop("media_type"),
    lambda e: e.update(run_id="run_99999999-9999-9999-9999-999999999999"),
    lambda e: e.update(sha256="b" * 63),
    lambda e: e.update(sha256="B" * 64),
    lambda e: e.update(bytes=True),
    lambda e: e.update(bytes=-1),
    lambda e: e.update(bytes=1.5),
    lambda e: e.update(bytes="128"),
    lambda e: e.update(media_type="text/html"),
    lambda e: e.update(media_type="noslash"),
    lambda e: e.update(media_type={"token": "secret-canary"}),
])
def test_build_report_rejects_malformed_evidence_entries(mutate):
    entry = dict(_EVIDENCE_ENTRY); mutate(entry)
    value = snapshot(); value["evidence"] = [entry]
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(value)


@pytest.mark.parametrize("value", [[{"id": {"token": "secret-canary"}}], "evidence", [{"sha256": "b" * 64, "bytes": 1, "media_type": "text/plain"}]])
def test_build_report_rejects_non_registration_evidence(value):
    source = snapshot(); source["evidence"] = value
    with pytest.raises(ValueError, match="REPORT_INPUT_INVALID"):
        build_report(source)
