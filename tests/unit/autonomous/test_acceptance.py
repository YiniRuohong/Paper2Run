"""Offline checks of acceptance logic only; these do not certify a real model run."""
from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

_spec = importlib.util.spec_from_file_location("autonomous_acceptance", Path(__file__).resolve().parents[3] / "tools/autonomous_acceptance.py")
a = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(a)


def archive(files, *, mutate_manifest=None):
    entries = dict(files)
    manifest = {name: {"sha256": a.digest(raw), "bytes": len(raw)} for name, raw in entries.items()}
    if mutate_manifest:
        mutate_manifest(manifest)
    entries["manifest.json"] = a.canonical(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name, raw in entries.items():
            z.writestr(name, raw)
    return output.getvalue()


def test_defaults_and_actual_origin():
    args = a.parse_args([])
    assert args.spawn and args.base_url == "http://127.0.0.1:8000"
    assert args.model == "zcode/GLM-5.3-Flash"
    assert args.model_base_url == "http://127.0.0.1:3425"
    assert args.output == a.ROOT / ".p2r-e2e-autonomous/fullflow"
    assert not args.pdf
    assert not args.allow_unauthenticated_loopback
    assert a.parse_args(["--port", "8123"]).base_url.endswith(":8123")
    assert not a.parse_args(["--base-url=http://127.0.0.1:8124"]).spawn


def test_model_api_and_specialist_cli():
    defaults = a.parse_args([])
    assert defaults.model_api == "anthropic-messages" and defaults.specialist_model == []
    args = a.parse_args(["--model-api", "openai-completions", "--specialist-model", "model-a",
                         "--specialist-model", "model-a", "--specialist-model", "model-b"])
    assert args.model_api == "openai-completions"
    assert args.specialist_model == ["model-a", "model-a", "model-b"]
    assert len(a.parse_args(["--specialist-model", "x"] * 16).specialist_model) == 16
    assert a.parse_args(["--specialist-model", "x" * 256]).specialist_model == ["x" * 256]


@pytest.mark.parametrize("flags", [["--model-api", "unsupported"], ["--specialist-model", ""],
    ["--specialist-model", "   "], ["--specialist-model", "x" * 257], ["--specialist-model", "x"] * 17])
def test_invalid_specialist_cli(flags):
    with pytest.raises(SystemExit):
        a.parse_args(flags)


@pytest.mark.parametrize("anonymous", [False, True])
def test_configure_model_profiles_reuses_credentials(anonymous):
    from p2r_api.routes.autonomous import Profile
    from p2r_contracts.autonomous import Specialist
    flags = ["--model-api", "openai-completions", "--specialist-model", "model-a",
             "--specialist-model", "model-b"]
    if anonymous:
        flags.append("--allow-unauthenticated-loopback")
    runner = object.__new__(a.Runner)
    runner.args, runner.secret = a.parse_args(flags), "unit-only-credential"
    calls = []
    runner.api = lambda method, path, **kwargs: calls.append((method, path, kwargs["json"]))
    specialists = runner.configure_model_profiles("acceptance-unit")
    assert len(calls) == 3
    main = calls[0][2]
    assert main == a.build_profile_payload("acceptance-unit", runner.args.model_base_url,
        runner.args.model, "openai-completions", runner.secret, anonymous=anonymous)
    for index, ((method, path, payload), specialist) in enumerate(zip(calls[1:], specialists), 1):
        profile_id = f"acceptance-unit-expert-{index}"
        assert method == "POST" and path == "/autonomous/profiles"
        assert payload["id"] == profile_id
        assert payload["model"] == runner.args.specialist_model[index - 1]
        assert payload["api"] == main["api"] and payload["base_url"] == main["base_url"]
        assert "api_key" not in payload
        assert payload["auth_mode"] == ("none" if anonymous else "api-key")
        assert payload.get("reuse_credentials_from") == (None if anonymous else main["id"])
        assert specialist["id"] == f"expert-{index}"
        assert "model requested for research" in specialist["description"]
        assert specialist["task_types"] == ["paper_analysis", "method_design", "coding", "data_analysis", "review"]
        assert specialist["model"] == {"provider_profile": profile_id, "credential_ref": "profile:" + profile_id}
        assert Profile.model_validate(payload).id == profile_id
        assert Specialist.model_validate(specialist).id == specialist["id"]


def test_configure_no_specialists_preserves_main_profile_and_objective():
    runner = object.__new__(a.Runner)
    runner.args, runner.secret = a.parse_args([]), "unit-only-credential"
    calls = []
    runner.api = lambda method, path, **kwargs: calls.append(kwargs["json"])
    assert runner.configure_model_profiles("acceptance-unit") == []
    assert calls == [a.build_profile_payload("acceptance-unit", runner.args.model_base_url,
        runner.args.model, "anthropic-messages", runner.secret)]
    _, objective = a.protocol_and_objective(runner.args, {"pdf_sha256": "a" * 64, "ingestion": "unit"})
    assert "research_delegate" not in objective


def test_specialists_objective_requests_choice_not_fixed_assignment():
    args = a.parse_args(["--specialist-model", "model-a"])
    protocol, objective = a.protocol_and_objective(args, {"pdf_sha256": "a" * 64, "ingestion": "unit"})
    assert protocol["objective"] == objective
    assert "research_delegate" in objective and "at least one" in objective
    assert "Explain" in objective and "unverified advisory" in objective
    assert "background" in objective and "submit_candidate" in objective
    assert "successful entrypoint run comes after the latest code registration" in objective
    assert "Then invoke" not in objective


def delegation_fixture(models=("model-a",)):
    configured, events = [], []
    for index, model in enumerate(models, 1):
        specialist = {"id": f"expert-{index}", "model": model, "task_types": ["review"]}
        configured.append(specialist)
        common = {"specialist_id": specialist["id"], "task_type": "review", "model": model,
                  "reason": "Check the frozen method", "evidence_status": "unverified_advisory"}
        events.extend([
            {"type": "sdk_event", "payload": {**common, "type": "research_delegation_started"}},
            {"type": "sdk_event", "payload": {**common, "type": "research_delegation_completed", "text": "Review advice"}},
        ])
    return configured, events


def test_delegations_optional_for_legacy_acceptance():
    assert a.verify_delegations([], []) == {"required": False, "successful_delegations": 0}


@pytest.mark.parametrize("models,primary,expected", [
    (("model-a",), "model-a", "same_model_specialists"),
    (("model-a", "model-a"), "model-a", "same_model_specialists"),
    (("model-b",), "model-a", "distinct_models_observed"),
    (("model-a", "model-b"), "model-a", "distinct_models_observed"),
])
def test_delegations_report_observed_model_identity(models, primary, expected):
    configured, events = delegation_fixture(models)
    result = a.verify_delegations(events, configured, primary_model=primary)
    assert result["successful_delegations"] == len(models)
    assert result["model_diversity"] == expected
    assert result["evidence_status"] == "unverified_advisory"
    assert result["observed_specialist_models"] == sorted(set(models))


@pytest.mark.parametrize("mutation", ["missing", "text_claim", "wrong_outer_type", "orphan",
    "reverse", "wrong_specialist", "wrong_task", "wrong_model", "changed_model", "blank_reason",
    "blank_text", "error", "verified_claim", "duplicate_completion"])
def test_delegations_reject_missing_forged_or_inconsistent_evidence(mutation):
    configured, events = delegation_fixture()
    start, end = events[0]["payload"], events[1]["payload"]
    if mutation == "missing":
        events = []
    elif mutation == "text_claim":
        events = [{"type": "sdk_event", "payload": {"type": "message_end", "text": json.dumps(events)}}]
    elif mutation == "wrong_outer_type":
        for event in events:
            event["type"] = "tool_completed"
    elif mutation == "orphan":
        events.pop(0)
    elif mutation == "reverse":
        events.reverse()
    elif mutation == "wrong_specialist":
        start["specialist_id"] = end["specialist_id"] = "unknown"
    elif mutation == "wrong_task":
        start["task_type"] = end["task_type"] = "coding"
    elif mutation == "wrong_model":
        start["model"] = end["model"] = "unconfigured"
    elif mutation == "changed_model":
        end["model"] = "model-b"
    elif mutation == "blank_reason":
        start["reason"] = " "
    elif mutation == "blank_text":
        end["text"] = " "
    elif mutation == "error":
        end["is_error"] = True
    elif mutation == "verified_claim":
        end["evidence_status"] = "verified"
    else:
        events.append(copy.deepcopy(events[-1]))
    with pytest.raises(a.AcceptanceError):
        a.verify_delegations(events, configured)


def test_delegations_allow_repeat_calls_but_do_not_count_failed_responses():
    configured, events = delegation_fixture()
    repeated = events + copy.deepcopy(events)
    assert a.verify_delegations(repeated, configured)["successful_delegations"] == 2
    events[1]["payload"]["is_error"] = True
    assert a.verify_delegations(repeated, configured)["successful_delegations"] == 1


def test_delegations_require_matching_task_type_on_completion():
    configured, events = delegation_fixture()
    configured[0]["task_types"].append("coding")
    events[1]["payload"]["task_type"] = "coding"
    with pytest.raises(a.AcceptanceError, match="matching prior start"):
        a.verify_delegations(events, configured)


def test_unused_specialists_do_not_establish_model_diversity():
    configured, events = delegation_fixture(("model-a", "model-b"))
    result = a.verify_delegations(events[:2], configured, primary_model="model-a")
    assert result["successful_delegations"] == 1
    assert result["model_diversity"] == "same_model_specialists"


def test_explicit_loopback_anonymous_provider_mode():
    args = a.parse_args(["--allow-unauthenticated-loopback"])
    assert args.allow_unauthenticated_loopback
    for url in ("http://127.0.0.1:3425", "http://localhost:3425", "http://[::1]:3425"):
        assert a.parse_args(["--allow-unauthenticated-loopback", "--model-base-url", url])


@pytest.mark.parametrize("url", ["https://provider.example", "http://127.0.0.2:3425"])
def test_anonymous_provider_mode_rejects_remote_provider(url):
    with pytest.raises(SystemExit):
        a.parse_args(["--allow-unauthenticated-loopback", "--model-base-url", url])


def test_empty_secret_scan_is_noop(tmp_path):
    (tmp_path / "output.txt").write_text("ordinary output", encoding="utf-8")
    assert not a.contains_secret(b"ordinary output", "")
    assert a.scan_local_secrets(tmp_path, "") == {
        "files_checked": 1, "bytes_checked": len(b"ordinary output"), "symlinks_not_followed": 0}


@pytest.mark.parametrize("flags", [["--timeout", "nan"], ["--request-timeout", "0"],
    ["--poll-interval", "inf"], ["--port", "0"], ["--max-model-calls", "0"],
    ["--base-url", "http://example.com"], ["--base-url", "http://127.0.0.1/api/v1"],
    ["--base-url", "http://user:secret@localhost"], ["--model-base-url", "https://host?api_key=secret"]])
def test_invalid_cli_fails(flags):
    with pytest.raises(SystemExit):
        a.parse_args(flags)


@pytest.mark.parametrize("proxy", ["http://proxy-user:proxy-secret@127.0.0.1:7890",
    "http://proxy-user@127.0.0.1:7890", "http://@127.0.0.1:7890", "socks5://localhost:7890",
    "http://localhost:invalid", "http://localhost:0", "http://localhost:65536",
    "http://[invalid", "http://localhost/path", "http://localhost?key=proxy-secret",
    "http://localhost#proxy-secret", "http://local\nhost:7890", ""])
def test_public_proxy_rejects_invalid_url_without_echo(proxy, capsys):
    with pytest.raises(SystemExit) as exc:
        a.parse_args(["--public-download-proxy", proxy])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert "proxy-secret" not in error and "proxy-user" not in error


@pytest.mark.parametrize("proxy", [None, "http://127.0.0.1:7890", "https://localhost:7890"])
def test_public_proxy_isolated_from_api_and_credentials(proxy, tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(a, "ROOT", tmp_path)
    monkeypatch.setenv("HTTP_PROXY", "http://unwanted-environment-proxy:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://unwanted-environment-proxy:9999")
    flags = ["--base-url", "http://127.0.0.1:8000", "--output", str(tmp_path / "output")]
    if proxy is not None:
        flags += ["--public-download-proxy", proxy]
    args = a.parse_args(flags)
    original_client, constructors, requests = httpx.Client, [], []
    def response(request):
        requests.append(request)
        return httpx.Response(200, content=b"public source" if request.url.host == "arxiv.org" else b'{"data":{}}')
    def client_factory(**kwargs):
        constructors.append(dict(kwargs))
        kwargs.pop("proxy", None)  # No sockets in this test; inspect routing intent above.
        return original_client(**kwargs, transport=httpx.MockTransport(response))
    monkeypatch.setattr(httpx, "Client", client_factory)
    runner = a.Runner(args, "unit-private-credential")
    try:
        runner.client.cookies.set("api-session", "private-cookie")
        runner.client.headers["Authorization"] = "Bearer api-only"
        assert runner.download(a.PAPER_URL) == b"public source"
        runner.api("GET", "/test")
        assert len(constructors) == 2
        assert constructors[0].get("proxy") is None and constructors[0]["trust_env"] is False
        assert constructors[1]["proxy"] == proxy and constructors[1]["trust_env"] is False
        assert constructors[1]["follow_redirects"] is True
        assert not ({"headers", "cookies", "auth"} & constructors[1].keys())
        public, api = requests
        assert "authorization" not in public.headers and "cookie" not in public.headers
        assert "origin" not in public.headers and "unit-private-credential" not in str(public.headers)
        assert public.content == b""
        assert api.headers["authorization"] == "Bearer api-only"
        assert api.url.host == "127.0.0.1"
        assert args.model_base_url == "http://127.0.0.1:3425"
    finally:
        runner.client.close()


def test_acceptance_rejects_false_zip_size_before_materializing():
    import struct
    import zlib
    payload = b"x" * (1024 * 1024)
    blob = bytearray(archive({"a": payload}, mutate_manifest=lambda m: m.update(
        a={"bytes": 1, "sha256": a.digest(b"x")})))
    central = blob.index(b"PK\x01\x02")
    struct.pack_into("<I", blob, central + 16, zlib.crc32(b"x"))
    struct.pack_into("<I", blob, central + 24, 1)
    with pytest.raises(a.AcceptanceError):
        a.read_verified_zip(blob)


def test_missing_key_refuses_before_any_runner_or_network(monkeypatch, capsys):
    monkeypatch.delenv("P2R_TEST_API_KEY", raising=False)
    monkeypatch.setattr(a, "Runner", lambda *args: pytest.fail("runner must not start"))
    assert a.main([]) == 2
    assert "REFUSED" in capsys.readouterr().err


@pytest.mark.parametrize("pdf_pages", [None, 48])
def test_product_upload_hash_verified_for_text_and_pdf(pdf_pages):
    paper = {"paper_version_id": "pv-unit", "sha256": "a" * 64, "page_count": 48}
    a.verify_uploaded_paper(paper, "a" * 64, pdf_pages=pdf_pages)
    with pytest.raises(a.AcceptanceError, match="SHA256"):
        a.verify_uploaded_paper(paper, "b" * 64, pdf_pages=pdf_pages)


@pytest.mark.parametrize("change", [{"paper_version_id": None}, {"page_count": 47}, {"page_count": "48"}])
def test_invalid_product_pdf_metadata_rejected(change):
    paper = {"paper_version_id": "pv-unit", "sha256": "a" * 64, "page_count": 48, **change}
    with pytest.raises(a.AcceptanceError):
        a.verify_uploaded_paper(paper, "a" * 64, pdf_pages=48)


def complete_pdf_status():
    return {"status": "ready", "backend": "online_agent", "page_count": 48, "pages_parsed": 48,
        "error_code": None, "preview": "Real parsed paper text", "chunks": [
            {"first_page": 1, "last_page": 20, "status": "done"},
            {"first_page": 21, "last_page": 40, "status": "done"},
            {"first_page": 41, "last_page": 48, "status": "done"}]}


def test_complete_pdf_parse_checks_all_page_chunks():
    a.verify_complete_pdf_parse(complete_pdf_status(), 48)


@pytest.mark.parametrize("mutation", ["partial", "count", "missing_chunk", "empty_chunks", "failed_chunk",
    "overlap", "out_of_range", "invalid_range", "backend", "state", "error", "preview", "bool_count"])
def test_partial_or_inconsistent_pdf_ready_rejected(mutation):
    status = complete_pdf_status()
    if mutation == "partial":
        status["pages_parsed"] = 40
    elif mutation == "count":
        status["page_count"] = 47
    elif mutation == "missing_chunk":
        status["chunks"].pop(1)
    elif mutation == "empty_chunks":
        status["chunks"] = []
    elif mutation == "failed_chunk":
        status["chunks"][1]["status"] = "failed"
    elif mutation == "overlap":
        status["chunks"][1]["first_page"] = 20
    elif mutation == "out_of_range":
        status["chunks"][-1]["last_page"] = 49
    elif mutation == "invalid_range":
        status["chunks"][0]["first_page"] = "1"
    elif mutation == "backend":
        status["backend"] = "offline_mineru"
    elif mutation == "state":
        status["status"] = "parsing"
    elif mutation == "error":
        status["error_code"] = "partial_error"
    elif mutation == "preview":
        status["preview"] = " "
    else:
        status["pages_parsed"] = True
    with pytest.raises(a.AcceptanceError):
        a.verify_complete_pdf_parse(status, 48)


@pytest.fixture
def skill_evidence():
    # Read actual bundled packages, without registry construction or writes.
    from p2r_orchestration.research_capabilities import _package
    root = Path(__file__).resolve().parents[3] / "resources/research-skills"
    snapshots = [{**_package(root / name), "source": "bundled", "enabled": True} for name in sorted(a.BUNDLED_SKILLS)]
    capabilities = [{k: v for k, v in item.items() if k not in ("content", "files")} for item in snapshots]
    files = {"skills.json": a.canonical(snapshots),
             "config.json": a.canonical({"harness": {"adapter": "pi", "skills": [s["id"] for s in snapshots]}})}
    return capabilities, files


def test_all_eight_real_bundled_skills_verified(skill_evidence):
    capabilities, files = skill_evidence
    selected = a.select_bundled_skills(list(reversed(capabilities)))
    assert len(selected) == 8
    assert {s["id"] for s in selected} == a.BUNDLED_SKILLS
    verified = a.verify_skill_snapshots(files, selected)
    assert [s["id"] for s in verified] == sorted(a.BUNDLED_SKILLS)
    assert all(s["version"] and len(s["sha256"]) == 64 for s in verified)


@pytest.mark.parametrize("mutation", ["missing", "disabled", "local", "duplicate", "bad_hash"])
def test_invalid_skill_selection_rejected(skill_evidence, mutation):
    capabilities, _ = skill_evidence
    if mutation == "missing":
        capabilities.pop()
    elif mutation == "disabled":
        capabilities[0]["enabled"] = False
    elif mutation == "local":
        capabilities[0]["source"] = "local"
    elif mutation == "duplicate":
        capabilities.append(capabilities[0])
    else:
        capabilities[0]["sha256"] = "wrong"
    with pytest.raises(a.AcceptanceError):
        a.select_bundled_skills(capabilities)


@pytest.mark.parametrize("mutation", ["missing", "version", "content", "package", "hash", "manifest", "config"])
def test_skill_export_tampering_rejected(skill_evidence, mutation):
    capabilities, files = skill_evidence
    snapshots = a.strict_json(files["skills.json"])
    if mutation == "missing":
        snapshots.pop()
    elif mutation == "version":
        snapshots[0]["version"] = "9.0.0"
    elif mutation == "content":
        snapshots[0]["content"] += "tampered"
    elif mutation == "package":
        snapshots[0]["files"]["extra.txt"] = base64.b64encode(b"tampered").decode()
    elif mutation == "hash":
        snapshots[0]["sha256"] = "0" * 64
    elif mutation == "manifest":
        manifest = a.strict_json(base64.b64decode(snapshots[0]["files"]["manifest.json"]))
        manifest["version"] = "9.0.0"
        snapshots[0]["files"]["manifest.json"] = base64.b64encode(a.canonical(manifest)).decode()
    else:
        files["config.json"] = a.canonical({"harness": {"skills": []}})
    files["skills.json"] = a.canonical(snapshots)
    with pytest.raises(a.AcceptanceError):
        a.verify_skill_snapshots(files, capabilities)


def test_protocol_is_current_contract_and_exploratory():
    from p2r_contracts.autonomous import freeze_protocol
    protocol, objective = a.protocol_and_objective(a.parse_args([]), {"pdf_sha256": "a" * 64, "ingestion": "external extraction"})
    assert freeze_protocol(protocol)["protocol"] == protocol
    assert protocol["evaluation"]["reference_value"] is None
    assert protocol["mode"] == "exploratory"
    assert protocol["evaluation"]["adapter"] == "regression"
    assert "load_diabetes" in objective and "synthetic" in objective
    assert "def " not in objective  # Only goals and format requirements, not experiment implementation.
    split = a.split_ids()
    assert len(split["test"]) == 89 and len(split["train"]) == 353
    assert not set(split["train"]) & set(split["test"])
    assert sorted(split["train"] + split["test"]) == list(range(442))
    assert split == a.split_ids()


def test_zip_integrity_and_key_detection():
    raw = archive({"task.json": b"{}", "artifacts/a/train.py": b"unit test bytes"})
    files = a.read_verified_zip(raw, "private-test-credential")
    assert files["task.json"] == b"{}"
    broken = archive({"task.json": b"{}"}, mutate_manifest=lambda m: m["task.json"].update(sha256="0" * 64))
    with pytest.raises(a.AcceptanceError, match="ZIP integrity"):
        a.read_verified_zip(broken)
    with pytest.raises(a.AcceptanceError, match="credential"):
        a.read_verified_zip(archive({"session.json": b'"private-test-credential"'}), "private-test-credential")


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../outside", "a\\outside", "a//b", "./a", "a\nfile"])
def test_zip_path_rejected(name):
    with pytest.raises(a.AcceptanceError, match="ZIP integrity"):
        a.read_verified_zip(archive({name: b"x"}))


def test_zip_duplicate_and_extra_member_rejected():
    stream = io.BytesIO(archive({"ok": b"one"}))
    with zipfile.ZipFile(stream, "a") as z:
        z.writestr("unlisted", b"two")
    with pytest.raises(a.AcceptanceError, match="ZIP integrity"):
        a.read_verified_zip(stream.getvalue())
    stream = io.BytesIO(archive({"ok": b"one"}))
    with zipfile.ZipFile(stream, "a") as z, pytest.warns(UserWarning):
        z.writestr("ok", b"two")
    with pytest.raises(a.AcceptanceError, match="ZIP integrity"):
        a.read_verified_zip(stream.getvalue())


def test_zip_size_and_symlink_rejected(monkeypatch):
    raw = archive({"large": b"x" * 2000})
    monkeypatch.setattr(a, "MAX_ZIP", 1000)
    with pytest.raises(a.AcceptanceError, match="size"):
        a.read_verified_zip(raw)
    monkeypatch.setattr(a, "MAX_ZIP", 128 * 1024 * 1024)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as z:
        link = zipfile.ZipInfo("symlink")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        z.writestr(link, "target")
    with pytest.raises(a.AcceptanceError, match="ZIP integrity"):
        a.read_verified_zip(stream.getvalue())


def test_redaction_and_strict_json():
    secret = 'private-"key\\value'
    assert a.clean({"value": secret}, secret) == {"value": "[REDACTED]"}
    assert a.contains_secret(json.dumps({"value": secret}).encode(), secret)
    for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
        with pytest.raises(a.AcceptanceError):
            a.strict_json(raw)


@pytest.mark.parametrize("data", [[], [{"id": "1", "prediction": True}],
    [{"id": "1", "prediction": float("inf")}], [{"id": 1, "prediction": 2}],
    [{"id": "1", "prediction": 2}, {"id": "1", "prediction": 3}]])
def test_invalid_metric_records(data):
    with pytest.raises(a.AcceptanceError):
        a.records(data, "prediction")


@pytest.fixture(scope="module")
def diabetes_reference():
    # Real bundled public raw data; no download, oracle mock or submitted code.
    a.reference_dependencies()  # Explicit prerequisite error, never importorskip.
    from sklearn.datasets import load_diabetes
    from sklearn.linear_model import Lars
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    x, y = load_diabetes(scaled=False, return_X_y=True)
    split = a.split_ids()
    pipeline = make_pipeline(StandardScaler(), Lars(n_nonzero_coefs=10))
    pipeline.fit(x[split["train"]], y[split["train"]])
    predictions = pipeline.predict(x[split["test"]])
    return x.tolist(), y.tolist(), predictions.tolist()


@pytest.fixture
def unit_evidence(diabetes_reference):
    # Artificial export/events only; numerical evidence uses the real reference method.
    source = {"pdf_sha256": "a" * 64, "uploaded_sha256": "b" * 64, "ingestion": "unit-only"}
    protocol, _ = a.protocol_and_objective(a.parse_args([]), source)
    frozen = {"protocol": protocol, "sha256": a.digest(a.canonical(protocol))}
    prefix = "artifacts/" + frozen["sha256"] + "/"
    data, labels, predictions = copy.deepcopy(diabetes_reference)
    artifacts = {
        "train.py": b"# unit-only code fixture", "data.py": b"# second unit-only code fixture",
        "dataset.json": a.canonical({"data": data, "target": labels}),
        "split.json": a.canonical(a.split_ids()),
        "requirements.txt": b"numpy==1\nscikit-learn==1", "environment.txt": b"Python 3\nnumpy==1\nscikit-learn==1",
        "run.log": b"unit-only log",
        "predictions.json": a.canonical([{"id": str(i), "prediction": prediction}
                                         for i, prediction in zip(a.split_ids()["test"], predictions)]),
        "labels.json": a.canonical([{"id": str(i), "label": labels[i]} for i in a.split_ids()["test"]]),
    }
    artifacts["data-provenance.json"] = a.canonical({"loader": "sklearn.datasets.load_diabetes", "scaled": False,
        "source_urls": [a.DATA_URL, a.LABEL_URL], "sklearn_version": "unit",
        "dataset_sha256": a.digest(artifacts["dataset.json"]), "split_sha256": a.digest(artifacts["split.json"])})
    provenance = [{"name": n, "kind": "code" if n.endswith(".py") else "other", "sha256": a.digest(raw),
                   "protocol_sha256": frozen["sha256"]} for n, raw in artifacts.items()]
    report = {"status": "not_comparable", "verdict": "not_comparable", "missing_artifacts": [],
        "missing_evidence_kinds": [], "evaluation": {"verdict": "not_comparable", "comparison_eligible": False,
        "metrics": {}, "evaluator_version": "paper2run.builtin.v2",
        "confidence_intervals": {},  # Filled from genuine predictions below.
        "artifact_hashes": {n: a.digest(artifacts[n]) for n in ("predictions.json", "labels.json")}}}
    report["sha256"] = a.digest(a.canonical(report))
    events = [{"type": "target_prepare", "payload": {"ready": True, "container": "p2r-unit"}}]
    for request, name, command, job in [("1", "execute", "pip install numpy scikit-learn", "install"),
                                         ("2", "background", "python train.py", "training")]:
        events.extend([{"type": "tool_started", "payload": {"id": request, "name": name, "arguments": {"command": command}}},
            {"type": "tool_completed", "payload": {"id": request, "name": name, "is_error": False,
             "result": {"job_id": job, "state": "exited" if name == "execute" else "running", "exit_code": 0 if name == "execute" else None}}}])
    events.append({"type": "tool_completed", "payload": {"name": "status", "is_error": False,
        "result": {"job_id": "training", "state": "exited", "exit_code": 0}}})
    for name in ("submit_candidate", "register_artifact"):
        events.append({"type": "tool_completed", "payload": {"name": name, "is_error": False, "result": {}}})
    files = {prefix + n: raw for n, raw in artifacts.items()}
    files.update({n: a.canonical(v) for n, v in {"protocol.json": frozen, "task.json": {"state": "completed"},
        "report.json": report, "provenance.json": provenance, "logs/events.json": events,
        "paper-context.json": {"paper_available": True, "text": "unit paper", "source_sha256": source["uploaded_sha256"]}}.items()})
    synchronize_prediction_evidence(files)
    return files, protocol, source, data, labels


def test_unicode_report_hash_matches_service(unit_evidence):
    files = unit_evidence[0]
    report = a.strict_json(files["report.json"])
    report.pop("sha256")
    report["model_narrative"] = "仅探索，不与论文指标比较。"
    report["sha256"] = a.digest(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    files["report.json"] = a.canonical(report)
    assert a.verify_evidence(*unit_evidence)["verdict"] == "not_comparable"


def test_local_secret_audit_cross_chunk(tmp_path):
    secret = "unit-test-private-credential"
    (tmp_path / "safe").write_bytes(b"public evidence")
    assert a.scan_local_secrets(tmp_path, secret)["files_checked"] == 1
    (tmp_path / "unsafe").write_bytes(b"x" * (65536 - 5) + secret.encode())
    with pytest.raises(a.AcceptanceError, match="credential detected"):
        a.scan_local_secrets(tmp_path, secret)


def test_http_origin_idempotency_and_secret_rejection(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(a, "ROOT", tmp_path)
    args = a.parse_args(["--base-url", "http://127.0.0.1:8123", "--output", str(tmp_path / "output")])
    runner = a.Runner(args, "unit-private-credential")
    seen = []
    def handle(request):
        seen.append(request)
        if request.url.path.endswith("leak"):
            return httpx.Response(200, json={"data": "unit-private-credential"})
        return httpx.Response(200, json={"data": {"accepted": True}})
    runner.client.close()
    runner.client = httpx.Client(base_url=args.base_url, headers={"Origin": args.base_url}, transport=httpx.MockTransport(handle))
    assert runner.api("POST", "/test", json={})["accepted"]
    assert seen[0].headers["origin"] == "http://127.0.0.1:8123"
    assert seen[0].headers["idempotency-key"]
    with pytest.raises(a.AcceptanceError, match="credential"):
        runner.api("GET", "/leak")
    assert runner.leak
    runner.finish()
    assert "unit-private-credential" not in (runner.output / "result.json").read_text()
    assert not json.loads((runner.output / "result.json").read_text())["ok"]


def test_failure_report_retains_bounded_events(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(a, "ROOT", tmp_path)
    monkeypatch.setenv("P2R_TEST_API_KEY", "unit-private-credential")
    def fail(self):
        for i in range(1200):
            self.events.append({"sequence": i, "type": "unit"})
        raise a.AcceptanceError("unit failure containing unit-private-credential")
    monkeypatch.setattr(a.Runner, "run", fail)
    assert a.main(["--base-url", "http://127.0.0.1:8123", "--output", str(tmp_path / "output")]) == 1
    result_file = next((tmp_path / "output").glob("*/result.json"))
    result = json.loads(result_file.read_text())
    assert not result["ok"] and "[REDACTED]" in result["error"]
    assert "unit-private-credential" not in capsys.readouterr().err
    events = json.loads((result_file.parent / "events.json").read_text())
    assert len(events["events"]) == 1000


def test_event_pagination_and_cursor_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "ROOT", tmp_path)
    args = a.parse_args(["--base-url", "http://127.0.0.1:8123", "--output", str(tmp_path / "output")])
    runner = a.Runner(args, "unit-private-credential")
    runner.task_id = "unit"
    pages = iter([{"events": [{"sequence": i, "type": "unit"} for i in range(1, 201)], "next_cursor": 200},
                  {"events": [{"sequence": 201, "type": "unit"}], "next_cursor": 201}])
    monkeypatch.setattr(runner, "api", lambda *args, **kwargs: next(pages))
    runner.collect_events()
    assert runner.cursor == 201 and len(runner.events) == 201
    monkeypatch.setattr(runner, "api", lambda *args, **kwargs: {"events": [{"sequence": 200}], "next_cursor": 200})
    with pytest.raises(a.AcceptanceError, match="cursor"):
        runner.collect_events()
    runner.task_id = None
    runner.finish()


@pytest.mark.parametrize("interval", [None, {"lower": 0.0, "upper": 4.0},
                                      {"lower": 2.0, "upper": 2.0, "extra": 1}])
def test_acceptance_rejects_missing_or_incorrect_ci(unit_evidence, interval):
    files = unit_evidence[0]
    report = a.strict_json(files["report.json"])
    report.pop("sha256")
    report["evaluation"]["confidence_intervals"] = {} if interval is None else {"rmse_95": interval}
    report["sha256"] = a.digest(a.canonical(report))
    files["report.json"] = a.canonical(report)
    with pytest.raises(a.AcceptanceError, match="confidence interval"):
        a.verify_evidence(*unit_evidence)


def synchronize_prediction_evidence(files):
    """Keep all submission-controlled hashes/metrics consistent after an attack."""
    import math
    import random
    prefix = next(k.removesuffix("predictions.json") for k in files if k.endswith("/predictions.json"))
    predicted = a.records(a.strict_json(files[prefix + "predictions.json"]), "prediction")
    labels = a.records(a.strict_json(files[prefix + "labels.json"]), "label")
    errors = [predicted[k] - labels[k] for k in sorted(labels)]
    size, rng = len(errors), random.Random(20261104)
    bootstrap = sorted(math.hypot(*(errors[rng.randrange(size)] for _ in range(size))) / math.sqrt(size)
                       for _ in range(2000))
    report = a.strict_json(files["report.json"])
    report.pop("sha256", None)
    report["evaluation"].update(
        metrics={"rmse": math.hypot(*errors) / math.sqrt(size), "total": size},
        confidence_intervals={"rmse_95": {"lower": bootstrap[int(.025 * 1999)], "upper": bootstrap[int(.975 * 1999)]}},
        artifact_hashes={n: a.digest(files[prefix + n]) for n in ("predictions.json", "labels.json")})
    report["sha256"] = a.digest(json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
    files["report.json"] = a.canonical(report)
    provenance = a.strict_json(files["provenance.json"])
    for item in provenance:
        item["sha256"] = a.digest(files[prefix + item["name"]])
    files["provenance.json"] = a.canonical(provenance)


def test_noop_training_with_consistent_zero_predictions_rejected(unit_evidence):
    files = unit_evidence[0]
    prefix = next(k.removesuffix("train.py") for k in files if k.endswith("/train.py"))
    files[prefix + "train.py"] = b"print('training complete')\n"
    files[prefix + "predictions.json"] = a.canonical([
        {"id": str(i), "prediction": 0.0} for i in a.split_ids()["test"]])
    synchronize_prediction_evidence(files)
    with pytest.raises(a.AcceptanceError, match="independent reference method"):
        a.verify_evidence(*unit_evidence)


def test_independent_recomputation(unit_evidence):
    import numpy
    import sklearn
    result = a.verify_evidence(*unit_evidence)
    evaluation = a.strict_json(unit_evidence[0]["report.json"])["evaluation"]
    assert result["rmse"] == pytest.approx(evaluation["metrics"]["rmse"])
    assert result["confidence_intervals"] == evaluation["confidence_intervals"]
    assert result["test_rows"] == 89 and result["verdict"] == "not_comparable"
    check = result["reference_method_check"]
    assert check["check_type"] == "independent_reference_prediction_equivalence"
    assert check["numpy_version"] == numpy.__version__
    assert check["sklearn_version"] == sklearn.__version__
    assert check["samples_checked"] == 89 and check["code_identity_proven"] is False
    assert check["rtol"] == 1e-7 and check["atol"] == 1e-8
    assert "predictions" not in check


@pytest.mark.parametrize("mutation", ["same_rmse", "wrong_lars", "one_row"])
def test_consistent_wrong_method_predictions_rejected(unit_evidence, mutation):
    files, _, _, data, labels = unit_evidence
    key = next(k for k in files if k.endswith("/predictions.json"))
    rows = a.strict_json(files[key])
    if mutation == "same_rmse":
        # Negating every residual preserves RMSE and every bootstrap draw.
        for row in rows:
            row["prediction"] = 2 * labels[int(row["id"])] - row["prediction"]
    elif mutation == "wrong_lars":
        import numpy as np
        from sklearn.linear_model import Lars
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
        x, y, split = np.asarray(data), np.asarray(labels), a.split_ids()
        wrong = make_pipeline(StandardScaler(), Lars(n_nonzero_coefs=1))
        wrong.fit(x[split["train"]], y[split["train"]])
        for row, prediction in zip(rows, wrong.predict(x[split["test"]])):
            row["prediction"] = float(prediction)
    else:
        rows[0]["prediction"] += .001
    files[key] = a.canonical(rows)
    synchronize_prediction_evidence(files)
    with pytest.raises(a.AcceptanceError, match="independent reference method"):
        a.verify_evidence(*unit_evidence)


def test_reference_tolerance_and_id_alignment(unit_evidence):
    files, protocol, source, reference_data, reference_labels = unit_evidence
    dataset_key = next(k for k in files if k.endswith("/dataset.json"))
    dataset = a.strict_json(files[dataset_key])
    dataset["data"][146][8] = 4.803999999999999
    files[dataset_key] = a.canonical(dataset)
    provenance_key = "provenance.json"
    provenance = a.strict_json(files[provenance_key])
    for item in provenance:
        if item["name"] == "dataset.json":
            item["sha256"] = a.digest(files[dataset_key])
    files[provenance_key] = a.canonical(provenance)
    origin_key = next(k for k in files if k.endswith("/data-provenance.json"))
    origin = a.strict_json(files[origin_key])
    origin["dataset_sha256"] = a.digest(files[dataset_key])
    files[origin_key] = a.canonical(origin)
    for item in provenance:
        if item["name"] == "data-provenance.json":
            item["sha256"] = a.digest(files[origin_key])
    files[provenance_key] = a.canonical(provenance)
    key = next(k for k in files if k.endswith("/predictions.json"))
    rows = a.strict_json(files[key])
    for row in rows:
        row["prediction"] += .25 * (1e-8 + 1e-7 * abs(row["prediction"]))
    files[key] = a.canonical(list(reversed(rows)))
    synchronize_prediction_evidence(files)
    assert a.verify_evidence(*unit_evidence)["reference_method_check"]["samples_checked"] == 89


def test_checker_never_executes_submission_or_proves_code_identity(unit_evidence, monkeypatch):
    files = unit_evidence[0]
    key = next(k for k in files if k.endswith("/train.py"))
    files[key] = b"raise RuntimeError('submitted code must never run on host')\n"
    synchronize_prediction_evidence(files)
    monkeypatch.setattr(a.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("host execution forbidden"))
    assert a.verify_evidence(*unit_evidence)["reference_method_check"]["code_identity_proven"] is False


@pytest.mark.parametrize("missing", ["numpy", "sklearn"])
def test_missing_reference_dependencies_fail_explicitly(missing, monkeypatch):
    import builtins
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == missing or name.startswith(missing + "."):
            raise ModuleNotFoundError("blocked prerequisite for test: " + missing)
        if missing == "sklearn" and name == "numpy":
            # Dependency-error test only: no fit/predict operation may be reached.
            return object()
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(a.AcceptanceError, match="prerequisite missing.*pip install numpy scikit-learn") as exc:
        a.verify_reference_method({}, [], [])
    assert str(exc.value.__cause__) == "blocked prerequisite for test: " + missing


def test_missing_reference_prerequisite_fails_before_runner_start(monkeypatch):
    import builtins
    original = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name == "numpy":
            raise ModuleNotFoundError("blocked prerequisite for test")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", blocked)
    monkeypatch.setattr(a.Runner, "start", lambda self: pytest.fail("must fail before API/network"))
    with pytest.raises(a.AcceptanceError, match="prerequisite missing"):
        a.Runner.__new__(a.Runner).run()


@pytest.mark.parametrize("mutation,match", [("labels", "independent public targets"), ("data", "public diabetes"),
    ("metric", "recomputed RMSE"), ("state", "did not complete"), ("tools", "required actual tools"),
    ("command", "successful real training"), ("protocol", "protocol changed")])
def test_false_success_is_rejected(unit_evidence, mutation, match):
    files, protocol, source, data, labels = copy.deepcopy(unit_evidence)
    if mutation == "labels":
        labels[protocol["parameters"]["split"]["test"][0]] += 1
        # Make dataset comparison pass, while labels.json remains unchanged.
        key = next(k for k in files if k.endswith("/dataset.json"))
        dataset = a.strict_json(files[key]); dataset["target"] = labels
        files[key] = a.canonical(dataset)
        provenance = a.strict_json(files["provenance.json"])
        for item in provenance:
            if item["name"] == "dataset.json":
                item["sha256"] = a.digest(files[key])
        pkey = next(k for k in files if k.endswith("/data-provenance.json"))
        origin = a.strict_json(files[pkey]); origin["dataset_sha256"] = a.digest(files[key])
        files[pkey] = a.canonical(origin)
        for item in provenance:
            if item["name"] == "data-provenance.json":
                item["sha256"] = a.digest(files[pkey])
        files["provenance.json"] = a.canonical(provenance)
    elif mutation == "data":
        data[0][0] += 1
    elif mutation == "metric":
        report = a.strict_json(files["report.json"])
        report["evaluation"]["metrics"]["rmse"] = 99
        report.pop("sha256")
        report["sha256"] = a.digest(a.canonical(report))
        files["report.json"] = a.canonical(report)
    elif mutation == "state":
        files["task.json"] = a.canonical({"state": "incomplete"})
    elif mutation in ("tools", "command"):
        events = a.strict_json(files["logs/events.json"])
        if mutation == "tools":
            events = [e for e in events if e["payload"].get("name") != "submit_candidate"]
        else:
            for e in events:
                if e["type"] == "tool_completed" and e["payload"].get("name") == "status":
                    e["payload"]["result"]["exit_code"] = 1
        files["logs/events.json"] = a.canonical(events)
    else:
        protocol["parameters"]["seed"] = 43
    with pytest.raises(a.AcceptanceError, match=match):
        a.verify_evidence(files, protocol, source, data, labels)
