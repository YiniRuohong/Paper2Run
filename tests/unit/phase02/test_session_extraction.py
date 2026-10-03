"""SessionExtractionService: runtime provider sessions → real provider chain.

The service backs papers.py's injectable seam: it resolves a live provider
session from the runtime-only secret store, overrides the session's declared
capabilities for the OpenAI-compatible text/JSON contract, resolves the key
through a store closure (never the environment), and delegates validation and
safe projection to ExtractionService over a real OpenAICompatibleProvider.

All transport interaction goes through a scripted fake — no network I/O.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from p2r_orchestration.provider_sessions import ProviderSecretStore
from p2r_orchestration.providers import (
    Capabilities,
    CompletionRequest,
    Message,
    OpenAICompatibleProvider,
    ProviderError,
    TransportResponse,
)
from p2r_orchestration.session_extraction import SessionExtractionService

PAPER_ID = "paper-session-1"
CONTENT = "We train a small CNN baseline on CIFAR-10 and report accuracy.".encode("utf-8")
PAPER_SHA256 = hashlib.sha256(CONTENT).hexdigest()
API_KEY = "sk-live-session-secret-abcdef123456"
ENDPOINT = "session-endpoint"
MODEL_ID = "model-a"
BASE_URL = "https://provider.example/v1"


def evidence(value, status="supported", refs=("ref-1",)):
    return {"value": value, "evidence_status": status, "source_refs": list(refs)}


def document(**changes):
    body = {
        "schema_version": "article-extraction.v1",
        "title": evidence("CNN baseline study"),
        "task": evidence("image classification"),
        "methods": [evidence("CNN baseline", "inferred")],
        "formulas": [],
        "datasets": [evidence("CIFAR-10")],
        "candidate_experiments": [{
            "candidate_id": "cand-1", "recipe_id": "fixture-recipe", "recipe_version": "1",
            "support_status": "supported", "parameters": {},
            "required_dependencies": [], "required_datasets": [],
            "resource_requirements": {}, "estimated_runtime": None,
            "source_refs": ["ref-1"],
        }],
        "warnings": [], "limitations": [],
        "source_refs": [{
            "id": "ref-1", "paper_id": PAPER_ID, "quote": "classify images",
            "page": 1, "pdf_sha256": PAPER_SHA256,
        }],
    }
    body.update(changes)
    return body


def openai_body(text: str) -> bytes:
    return json.dumps({
        "model": MODEL_ID,
        "choices": [{"message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
    }).encode()


class ScriptedTransport:
    """AsyncTransport fake: one scripted OpenAI-compatible body, records calls."""

    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status, self.calls = body, status, []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return TransportResponse(status=self.status, body=self.body)


def make_store(*, capabilities: Capabilities | None = None,
               api_key: str = API_KEY) -> ProviderSecretStore:
    store = ProviderSecretStore()
    store.put(base_url=BASE_URL, endpoint_alias=ENDPOINT, model_id=MODEL_ID,
              api_key=api_key, capabilities=capabilities)
    return store


def run_extract(service: SessionExtractionService, **overrides):
    kwargs = dict(
        paper_id=PAPER_ID,
        paper_sha256=PAPER_SHA256,
        content=CONTENT,
        provider_config_id=ENDPOINT,
        model_id=MODEL_ID,
        call_id="call-1",
        prompt_version="p-1",
        policy_version="v-1",
    )
    kwargs.update(overrides)
    return asyncio.run(service.extract(**kwargs))


async def _drain(iterator):
    async for _ in iterator:
        pass


# ------------------------------------------------------------------ happy path


def test_success_returns_safe_projection_through_real_provider_chain():
    store = make_store()
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    result = run_extract(SessionExtractionService(store, transport=transport))

    # ExtractionService's safe projection, unchanged by the session layer.
    assert set(result) == {
        "paper_id", "content_sha256", "context_hash", "provider_config_id",
        "model_id", "prompt_version", "policy_version", "extraction",
    }
    assert result["paper_id"] == PAPER_ID
    assert result["provider_config_id"] == ENDPOINT
    assert result["model_id"] == MODEL_ID
    assert result["content_sha256"] == PAPER_SHA256
    assert result["extraction"]["candidate_experiments"][0]["candidate_id"] == "cand-1"

    # The call really reached the OpenAI-compatible transport.
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == f"{BASE_URL}/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {API_KEY}"
    request_payload = json.loads(call["body"])
    assert request_payload["model"] == MODEL_ID
    content = request_payload["messages"][1]["content"]
    # The user message carries the provenance block the external model must
    # cite, followed by the article text itself.
    assert content.startswith("PROVENANCE (every source_ref must cite these exact values):")
    assert f"paper_id: {PAPER_ID}" in content
    assert f"pdf_sha256: {PAPER_SHA256}" in content
    assert content.endswith(CONTENT.decode("utf-8"))
    # Structured output was requested (json_schema), and succeeded.
    assert "json_schema" in request_payload.get("response_format", {})


def test_secret_comes_from_store_not_environment(monkeypatch):
    # Even a colliding environment variable must not win over the store key.
    monkeypatch.setenv("P2R_UNUSED_SESSION_SECRET", "env-key-must-not-be-used")
    store = make_store()
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    run_extract(SessionExtractionService(store, transport=transport))
    assert transport.calls[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"


# --------------------------------------------------------- capability handling


def test_structured_output_no_longer_capability_unverified():
    store = make_store()  # no capabilities declared → all "unknown"
    session_id = store.latest_session_id(ENDPOINT, MODEL_ID)
    assert session_id is not None
    config = store.config(session_id)
    assert config.capabilities.structured_output == "unknown"
    assert config.capabilities.text == "unknown"

    # Contrast: the raw stored config would refuse a structured request…
    raw_provider = OpenAICompatibleProvider(
        config,
        transport=ScriptedTransport(openai_body(json.dumps(document()))),
        secret_resolver=lambda: API_KEY,
    )
    raw_request = CompletionRequest(
        provider_config_id=ENDPOINT, model_id=MODEL_ID, phase="article_extraction",
        call_id="call-raw", messages=[Message(role="user", content="context")],
        context_hash="a" * 64, prompt_version="p", policy_version="v",
        json_schema={"name": "article_extraction", "schema": {}},
    )
    with pytest.raises(ProviderError) as raw_error:
        asyncio.run(_drain(raw_provider.complete(raw_request)))
    assert raw_error.value.code == "capability_unverified"

    # …while the session service overrides capabilities and completes.
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    result = run_extract(SessionExtractionService(store, transport=transport))
    assert result["extraction"]["source_refs"][0]["paper_id"] == PAPER_ID
    assert len(transport.calls) == 1


# ------------------------------------------------------------------- failures


def test_http_401_maps_to_auth_invalid():
    store = make_store()
    transport = ScriptedTransport(b"", status=401)
    service = SessionExtractionService(store, transport=transport)
    with pytest.raises(ProviderError) as error:
        run_extract(service)
    assert error.value.code == "auth_invalid"


def test_missing_session_maps_to_auth_invalid():
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    service = SessionExtractionService(ProviderSecretStore(), transport=transport)
    with pytest.raises(ProviderError) as error:
        run_extract(service)
    assert error.value.code == "auth_invalid"
    # No session resolved → the transport was never contacted.
    assert transport.calls == []


def test_cleared_secret_maps_to_auth_invalid():
    store = make_store()
    session_id = store.latest_session_id(ENDPOINT, MODEL_ID)
    store.clear(session_id)
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    with pytest.raises(ProviderError) as error:
        run_extract(SessionExtractionService(store, transport=transport))
    assert error.value.code == "auth_invalid"


# ---------------------------------------------------------------- secret safety


def test_error_messages_and_public_summaries_never_contain_api_key():
    store = make_store()
    session = store.latest_session_id(ENDPOINT, MODEL_ID)

    # Failure surfaces: the mapped error message carries a fixed code only.
    with pytest.raises(ProviderError) as error:
        run_extract(SessionExtractionService(store, transport=ScriptedTransport(b"", status=401)))
    assert API_KEY not in str(error.value)
    assert API_KEY not in repr(error.value)

    # Success surfaces: projection, ProviderConfig.public_summary and the
    # session's own public projection stay key-free.
    transport = ScriptedTransport(openai_body(json.dumps(document())))
    result = run_extract(SessionExtractionService(store, transport=transport))
    assert API_KEY not in json.dumps(result)
    assert API_KEY not in json.dumps(store.config(session).public_summary())
    assert API_KEY not in json.dumps(store.public_summary(session))


# ------------------------------------------------------- default runtime wiring


def test_create_app_default_runtime_exposes_extraction_service(tmp_path):
    from p2r_api.main import create_app

    app = create_app(db_url=f"sqlite:///{tmp_path / 'unit-runtime.sqlite3'}")
    runtime = app.state.runtime
    service = runtime.get("extraction_service")
    assert isinstance(service, SessionExtractionService)
    assert hasattr(service, "extract")
    # Same runtime-only store instance backs both the seam and the session API.
    assert runtime["provider_secret_store"] is not None
