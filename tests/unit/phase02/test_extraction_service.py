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


def test_schema_mismatch_is_rejected():
    provider = FakeProvider(completed_events(json.dumps(payload(
        title=evidence("Deep learning", "unsupported-claim"),
    ))))
    reject(lambda: run_extract(service(provider)))


def test_unknown_field_is_rejected_by_schema():
    provider = FakeProvider(completed_events(json.dumps(payload(extra_field="x"))))
    reject(lambda: run_extract(service(provider)))


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


def test_missing_source_ref_pdf_hash_is_rejected():
    provider = FakeProvider(completed_events(json.dumps(payload(
        source_refs=[{"id": "ref-1", "paper_id": PAPER_ID,
                      "quote": "classify images", "page": 1}],
    ))))
    reject(lambda: run_extract(service(provider)))


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
