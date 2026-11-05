from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest

from p2r_orchestration.article_schema import ExtractionDocument
from p2r_orchestration.extraction_service import (
    RESPONSE_SCHEMA_VERSION,
    ExtractionService,
)
from p2r_orchestration.providers import (
    CompletionRequest,
    NormalizedEvent,
    ProviderError,
    Usage,
)

PAPER_ID = "paper-1"
CONTENT = "Deep learning for image classification with benchmarks.".encode("utf-8")
PAPER_SHA256 = hashlib.sha256(CONTENT).hexdigest()


def evidence(value, status="supported", refs=()):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def payload(**changes):
    body = {
        "schema_version": "article-extraction.v1",
        "title": evidence("Deep learning", "supported", ["ref-1"]),
        "task": evidence("classification", "supported", ["ref-1"]),
        "methods": [evidence("CNN baseline", "inferred", ["ref-1"])],
        "formulas": [],
        "datasets": [],
        "candidate_experiments": [],
        "warnings": ["conflicting claims between sections"],
        "limitations": [],
        "source_refs": [
            {"id": "ref-1", "paper_id": PAPER_ID, "quote": "classify images",
             "page": 1, "pdf_sha256": PAPER_SHA256},
        ],
    }
    body.update(changes)
    return body


class FakeProvider:
    """Async-iterator fake compatible with OpenAICompatibleProvider.complete."""

    def __init__(self, events, *, request=None):
        self.events = list(events)
        self.requests: list[CompletionRequest] = []
        self._request = request

    async def complete(self, request: CompletionRequest):
        self.requests.append(request)
        for event in self.events:
            yield event

    @property
    def last_request(self) -> CompletionRequest:
        return self.requests[-1]


def completed_events(text: str, *, include_usage: bool = True):
    events = []
    if include_usage:
        events.append(NormalizedEvent(
            type="usage", call_id="call-1",
            usage=Usage(status="unknown"),
        ))
    events.append(NormalizedEvent(type="completed", call_id="call-1", text=text))
    return events


def service(provider, *, max_context_bytes: int = 64 * 1024) -> ExtractionService:
    return ExtractionService(provider, max_context_bytes=max_context_bytes)


def run_extract(svc: ExtractionService, **overrides):
    kwargs = dict(
        paper_id=PAPER_ID,
        paper_sha256=PAPER_SHA256,
        content=CONTENT,
        provider_config_id="provider-1",
        model_id="model-a",
        call_id="call-1",
        prompt_version="p-1",
        policy_version="v-1",
    )
    kwargs.update(overrides)
    return asyncio.run(svc.extract(**kwargs))


def reject(fn):
    try:
        fn()
    except ProviderError as exc:
        assert exc.code == "invalid_response", exc.code
        return
    raise AssertionError("expected ProviderError('invalid_response')")


def test_valid_response_returns_safe_projection_and_metadata():
    provider = FakeProvider(completed_events(json.dumps(payload())))
    result = run_extract(service(provider))

    assert result["paper_id"] == PAPER_ID
    assert result["content_sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert result["provider_config_id"] == "provider-1"
    assert result["model_id"] == "model-a"
    assert result["prompt_version"] == "p-1"
    assert result["policy_version"] == "v-1"
    assert result["extraction"]["schema_version"] == "article-extraction.v1"
    assert result["extraction"]["title"]["value"] == "Deep learning"
    # Safe projection only: no raw response, headers, usage, or secrets.
    assert set(result) == {
        "paper_id", "content_sha256", "context_hash", "provider_config_id",
        "model_id", "prompt_version", "policy_version", "extraction",
    }

    request = provider.last_request
    context = CONTENT.decode("utf-8")
    user_message = [message.content for message in request.messages][1]
    # Provenance block (citable identifiers for the external model), then text.
    assert user_message.startswith("PROVENANCE (every source_ref must cite these exact values):")
    assert f"paper_id: {PAPER_ID}" in user_message
    assert f"pdf_sha256: {PAPER_SHA256}" in user_message
    assert user_message.endswith(context)
    assert request.context_hash == hashlib.sha256(context.encode("utf-8")).hexdigest()
    assert result["context_hash"] == request.context_hash
    assert request.phase == "article_extraction"
    assert request.call_id == "call-1"
    assert request.prompt_version == "p-1"
    assert request.policy_version == "v-1"
    assert request.response_schema_version == RESPONSE_SCHEMA_VERSION
    assert request.json_schema is not None
    assert isinstance(request.json_schema, dict) and request.json_schema
    # The declared schema is the real article contract, not an empty shell.
    ExtractionDocument.model_validate(payload())


def test_context_is_truncated_and_hashed_from_sent_bytes():
    content = ("word " * 4000).encode("utf-8")
    provider = FakeProvider(completed_events(json.dumps(payload())))
    result = run_extract(service(provider, max_context_bytes=1024), content=content)

    sent = provider.last_request.messages[1].content
    # The article portion of the user message is truncated to the byte budget;
    # context_hash is computed from exactly those truncated bytes.
    truncated = content.decode("utf-8")[:1024]
    assert len(truncated.encode("utf-8")) == 1024
    assert sent.endswith(truncated)
    assert result["context_hash"] == hashlib.sha256(truncated.encode("utf-8")).hexdigest()
    # content_sha256 still reflects the full original bytes.
    assert result["content_sha256"] == hashlib.sha256(content).hexdigest()


def test_undecodable_content_is_rejected():
    provider = FakeProvider(completed_events(json.dumps(payload())))
    reject(lambda: run_extract(service(provider), content=b"\xff\xfe not utf-8"))


def test_invalid_json_is_rejected():
    provider = FakeProvider(completed_events("not json at all"))
    reject(lambda: run_extract(service(provider)))


def test_non_standard_json_constant_is_rejected():
    provider = FakeProvider(completed_events('{"title": NaN}'))
    reject(lambda: run_extract(service(provider)))


def _normalization_warning(result):
    return [w for w in result["extraction"]["warnings"] if w.startswith("Model output was normalized leniently")]


def test_invalid_evidence_status_is_downgraded_never_upgraded():
    provider = FakeProvider(completed_events(json.dumps(payload(
        title=evidence("Deep learning", "unsupported-claim"),
    ))))
    result = run_extract(service(provider))
    assert result["extraction"]["title"]["evidence_status"] == "inferred"
    assert "title evidence_status" in _normalization_warning(result)[0]


def test_unknown_field_is_dropped_with_a_warning():
    provider = FakeProvider(completed_events(json.dumps(payload(extra_field="x"))))
    result = run_extract(service(provider))
    assert "extra_field" not in result["extraction"]
    assert "extra_field" in _normalization_warning(result)[0]


def test_bare_values_and_drifted_shapes_are_normalized():
    body = payload(task={"task_type": "simulation", "reported_results": ["err < 0.003"]},
                   methods="Monte Carlo sampling", warnings="single warning")
    body.pop("datasets")
    provider = FakeProvider(completed_events(json.dumps(body)))
    result = run_extract(service(provider))
    extraction = result["extraction"]
    assert extraction["task"] == {"value": {"task_type": "simulation", "reported_results": ["err < 0.003"]},
                                  "evidence_status": "inferred", "source_refs": []}
    assert extraction["methods"] == [{"value": "Monte Carlo sampling", "evidence_status": "inferred", "source_refs": []}]
    assert extraction["datasets"] == []
    assert extraction["warnings"][0] == "single warning"
    assert len(_normalization_warning(result)) == 1


def test_common_field_aliases_are_read_as_contract_fields():
    body = payload(
        candidate_experiments=[{"id": "c-1", "recipe_id": "sandbox_python", "recipe_version": 1,
                                "execution_support": "supported"}],
        source_refs=[{"id": "ref-1", "paper_id": PAPER_ID, "text": "classify images",
                      "pdf_sha256": PAPER_SHA256}],
    )
    result = run_extract(service(FakeProvider(completed_events(json.dumps(body)))))
    spec = result["extraction"]["candidate_experiments"][0]
    assert (spec["candidate_id"], spec["support_status"], spec["recipe_version"]) == ("c-1", "supported", "1")
    assert result["extraction"]["source_refs"][0]["quote"] == "classify images"
    assert "execution_support read as support_status" in _normalization_warning(result)[0]


def test_clean_output_gets_no_normalization_warning():
    provider = FakeProvider(completed_events(json.dumps(payload())))
    assert _normalization_warning(run_extract(service(provider))) == []


def test_source_ref_paper_id_mismatch_is_rejected():
    provider = FakeProvider(completed_events(json.dumps(payload(
        source_refs=[{"id": "ref-1", "paper_id": "another-paper",
                      "quote": "classify images", "page": 1,
                      "pdf_sha256": PAPER_SHA256}],
    ))))
    reject(lambda: run_extract(service(provider)))


def test_source_ref_pdf_hash_mismatch_is_rejected():
    provider = FakeProvider(completed_events(json.dumps(payload(
        source_refs=[{"id": "ref-1", "paper_id": PAPER_ID,
                      "quote": "classify images", "page": 1,
                      "pdf_sha256": "0" * 64}],
    ))))
    reject(lambda: run_extract(service(provider)))


def test_missing_source_ref_pdf_hash_is_filled_from_current_paper():
    provider = FakeProvider(completed_events(json.dumps(payload(
        source_refs=[{"id": "ref-1", "paper_id": PAPER_ID,
                      "quote": "classify images", "page": 1}],
    ))))
    result = run_extract(service(provider))
    assert result["extraction"]["source_refs"][0]["pdf_sha256"] == PAPER_SHA256
    assert "pdf_sha256 filled" in _normalization_warning(result)[0]


def test_source_ref_without_quote_is_dropped_not_invented():
    provider = FakeProvider(completed_events(json.dumps(payload(
        source_refs=[{"id": "ref-1", "paper_id": PAPER_ID, "pdf_sha256": PAPER_SHA256}],
    ))))
    result = run_extract(service(provider))
    assert result["extraction"]["source_refs"] == []
    assert "has no quote" in _normalization_warning(result)[0]


def test_missing_completed_event_is_rejected():
    provider = FakeProvider([NormalizedEvent(
        type="usage", call_id="call-1", usage=Usage(status="unknown"),
    )])
    reject(lambda: run_extract(service(provider)))


def test_empty_event_stream_is_rejected():
    reject(lambda: run_extract(service(FakeProvider([]))))


def test_completed_without_text_is_rejected():
    provider = FakeProvider([NormalizedEvent(type="completed", call_id="call-1")])
    reject(lambda: run_extract(service(provider)))


def test_evidence_status_keeps_conflicting_spelling():
    doc = ExtractionDocument.model_validate(payload(
        warnings=["a conflicts with b"],
        limitations=[],
    ))
    assert doc.model_dump()["schema_version"] == "article-extraction.v1"
    enum = ExtractionDocument.model_json_schema()[
        "$defs"]["EvidenceField"]["properties"]["evidence_status"]["enum"]
    assert enum == ["supported", "inferred", "missing", "conflicting"]
    # "conflicted" is not part of the contract and must not validate.
    with pytest.raises(Exception):
        ExtractionDocument.model_validate(payload(
            title=evidence("Deep learning", "conflicted"),
        ))


PAGE_SENTENCE = (
    "Lines like <!-- p2r:page 7 --> or <!-- p2r:pages 21-40 --> mark the PDF page(s) "
    "of the text that follows; set source_ref.page to the first page of the marked span."
)


def test_system_prompt_explains_page_markers():
    provider = FakeProvider(completed_events(json.dumps(payload())))
    run_extract(service(provider))
    assert provider.last_request.messages[0].content.endswith(" " + PAGE_SENTENCE)


def test_trim_warning_in_projection():
    head = "<!-- p2r:pages 1-20 -->\n\n" + "first part. " * 40 + "\n\n"
    tail = "<!-- p2r:pages 21-40 -->\n\n" + "second part. " * 400
    content = (head + tail).encode("utf-8")
    limit = len(head) + 100
    provider = FakeProvider(completed_events(json.dumps(payload())))
    result = run_extract(service(provider, max_context_bytes=limit), content=content)

    warnings = result["extraction"]["warnings"]
    # The model's own warnings stay first; the trim warning is appended.
    assert warnings == ["conflicting claims between sections", "仅第 1–20 页发送给模型（超出上下文上限）"]
    sent_text = (head + tail)[:limit]
    assert provider.last_request.messages[1].content.endswith(sent_text)
    assert result["context_hash"] == hashlib.sha256(sent_text.encode("utf-8")).hexdigest()
    assert result["content_sha256"] == hashlib.sha256(content).hexdigest()


def test_no_trim_warning_when_text_fits():
    content = "<!-- p2r:page 1 -->\n\nShort paper.\n".encode("utf-8")
    provider = FakeProvider(completed_events(json.dumps(payload())))
    result = run_extract(service(provider), content=content)
    assert result["extraction"]["warnings"] == ["conflicting claims between sections"]
    assert result["context_hash"] == hashlib.sha256(content).hexdigest()


def test_references_section_is_not_sent():
    body = "<!-- p2r:page 1 -->\n\n# Method\n\nWe train a CNN.\n\n"
    content = (body + "# References\n\n[1] Some cited work.\n").encode("utf-8")
    provider = FakeProvider(completed_events(json.dumps(payload())))
    result = run_extract(service(provider), content=content)
    sent = provider.last_request.messages[1].content
    assert sent.endswith(body) and "Some cited work" not in sent
    assert result["context_hash"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
