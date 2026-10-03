"""Provider-backed structured extraction for a single article.

The service is deliberately transport-agnostic: it takes any object exposing
``complete(CompletionRequest) -> AsyncIterator[NormalizedEvent]`` (the
``OpenAICompatibleProvider`` protocol) and returns a *safe projection* — no
raw provider response, headers, usage secret, or transport payload ever
leaves this module.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

from pydantic import ValidationError

from .article_schema import ExtractionDocument
from .providers import CompletionRequest, Message, NormalizedEvent, ProviderError

EXTRACTION_PHASE = "article_extraction"
RESPONSE_SCHEMA_VERSION = "article-extraction.v1"

_SYSTEM_PROMPT = (
    "You are a structured extraction engine. Read the article context and "
    "return one JSON object that validates against the provided JSON schema. "
    "Every claim must be traceable to a source_ref that names the current "
    "paper_id and pdf_sha256 exactly as given in the PROVENANCE block of the "
    "user message. Use evidence_status 'conflicting' when sources disagree. "
    "Never invent source references. Candidate experiments must be executable "
    "by this platform: propose recipe_id/recipe_version only from the "
    "registered recipe registry — offline_fixture version 1 (controlled "
    "teaching smoke, no parameters) or real_cpu_baseline version 1 (real "
    "local-CPU logistic-regression binary classification on the bundled "
    "tabular dataset real_cpu_baseline_v1; parameters seed/max_epochs/"
    "learning_rate/batch_size) or sandbox_python version 1 (a "
    "platform-generated and human-reviewed self-contained standard-library "
    "Python script, executed in a local sandbox; parameters seed/"
    "max_seconds; prefer proposing it with execution_support 'supported' "
    "whenever the article's experiment can be approximated by a "
    "self-contained script). Set execution_support 'supported' only when "
    "the article's experiment can actually be reproduced by one of these "
    "registered recipes; use 'review_required' or 'unsupported' otherwise. "
    "Never invent unregistered recipe ids. The JSON object must contain "
    "every required top-level key — schema_version, title, task, methods, "
    "formulas, datasets, candidate_experiments, warnings, limitations, "
    "source_refs — even if a list is empty."
)


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


def _strict_wire_schema(node: Any) -> Any:
    """Rewrite the pydantic schema for strict provider structured output.

    ``Any`` fields serialize as bare ``{"default", "title"}`` schemas,
    ``dict[str, Any]`` values as ``additionalProperties: true``, and optional
    fields drop out of ``required`` — strict providers reject all three, and
    some gateways mis-validate ``$ref``/``$defs`` nesting. The wire schema
    keeps the same shape but makes every choice explicit, marks every
    property required, and inlines all refs into a self-contained document;
    the pydantic model on our side remains the authoritative validator.
    """
    rewritten = _rewrite_schema(node)
    if not isinstance(rewritten, dict):
        return rewritten
    defs = rewritten.pop("$defs", {})
    return _inline_refs(rewritten, defs if isinstance(defs, dict) else {})


def _rewrite_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [_rewrite_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    stripped: dict[str, Any] = {}
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            # Property names are data, not schema metadata: never strip keys
            # inside a properties map (the contract has a field literally
            # named "title", which metadata stripping would delete).
            stripped[key] = {name: _rewrite_schema(sub) for name, sub in value.items()}
        elif key in ("default", "title"):
            continue
        else:
            stripped[key] = _rewrite_schema(value)
    if "additionalProperties" in stripped and stripped["additionalProperties"] is True:
        # Some strict gateways reject anyOf inside additionalProperties (and
        # report it as a misleading required-key error), so free-form map
        # values are declared as plain strings on the wire. Validation stays
        # dict[str, Any]; these maps are informational on our side.
        stripped["additionalProperties"] = {"type": "string"}
    if not stripped:
        return _scalar_alternatives()
    properties = stripped.get("properties")
    if isinstance(properties, dict) and properties:
        for pname, pschema in properties.items():
            shaped = _wire_map_shape(pname, pschema)
            if shaped is not None:
                properties[pname] = shaped
        stripped["required"] = sorted(properties)
        stripped["additionalProperties"] = False
    if stripped.get("type") == "array" and "items" not in stripped:
        # Tuples serialize as prefixItems + maxItems with no ``items`` key;
        # some strict gateways require ``items`` on every array schema. It is
        # unreachable behind maxItems, so any scalar type satisfies them.
        stripped["items"] = {"type": "number"}
    return stripped


def _is_open_map(schema: Any) -> bool:
    return (
        isinstance(schema, dict)
        and schema.get("type") == "object"
        and "properties" not in schema
        and isinstance(schema.get("additionalProperties"), dict)
    )


def _wire_map_shape(name: str, schema: Any) -> dict[str, Any] | None:
    """Closed-object stand-ins for free-form maps on the strict wire.

    Strict gateways reject open maps (``additionalProperties`` with a
    subschema) inside properties, so the wire declares a closed key set for
    the two maps our contract carries. Validation on our side stays
    ``dict[str, Any]``; nothing on the wire is a claim about storage.
    """
    if not _is_open_map(schema):
        return None
    if name == "parameters":
        return {
            "type": "object",
            "properties": {
                "seed": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "max_epochs": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                "learning_rate": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                "batch_size": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            },
            "required": ["batch_size", "learning_rate", "max_epochs", "seed"],
            "additionalProperties": False,
        }
    if name == "resource_requirements":
        return {
            "type": "object",
            "properties": {
                "cpu": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "memory": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "gpu": {"anyOf": [{"type": "string"}, {"type": "null"}]},
            },
            "required": ["cpu", "gpu", "memory"],
            "additionalProperties": False,
        }
    return None


def _inline_refs(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if set(node) == {"$ref"} and isinstance(node["$ref"], str):
        target = defs.get(node["$ref"].rsplit("/", 1)[-1])
        if isinstance(target, dict):
            return _inline_refs(target, defs)
    return {
        key: _inline_refs(value, defs)
        for key, value in node.items()
        if key != "$defs"
    }


def _scalar_alternatives() -> dict[str, Any]:
    return {"anyOf": [
        {"type": "string"}, {"type": "integer"}, {"type": "number"},
        {"type": "boolean"}, {"type": "null"},
    ]}


class ExtractionService:
    """Turn article bytes into a validated, provenance-checked extraction."""

    def __init__(self, provider: Any, *, max_context_bytes: int = 128 * 1024) -> None:
        if type(max_context_bytes) is not int or max_context_bytes < 1:
            raise ValueError("max_context_bytes must be a positive integer")
        self._provider = provider
        self._max_context_bytes = max_context_bytes

    async def extract(
        self,
        *,
        paper_id: str,
        paper_sha256: str,
        content: bytes,
        provider_config_id: str,
        model_id: str,
        call_id: str,
        prompt_version: str,
        policy_version: str,
    ) -> dict[str, Any]:
        # 1. Decode and bound the context we are willing to send.
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ProviderError("invalid_response") from None
        context = text[: self._max_context_bytes]
        context_bytes = context.encode("utf-8")
        context_hash = hashlib.sha256(context_bytes).hexdigest()
        content_sha256 = hashlib.sha256(content).hexdigest()
        # The provider is an external model: without this block it cannot know
        # the identifiers that provenance validation later demands in every
        # source_ref, so a compliant response would be impossible.
        user_message = (
            "PROVENANCE (every source_ref must cite these exact values):\n"
            f"paper_id: {paper_id}\n"
            f"pdf_sha256: {paper_sha256.lower()}\n"
            "---- article text follows ----\n"
            f"{context}"
        )
        wire_schema = _strict_wire_schema(ExtractionDocument.model_json_schema())

        request = CompletionRequest(
            provider_config_id=provider_config_id,
            model_id=model_id,
            phase=EXTRACTION_PHASE,
            call_id=call_id,
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT),
                Message(role="user", content=user_message),
            ],
            context_hash=context_hash,
            prompt_version=prompt_version,
            policy_version=policy_version,
            response_schema_version=RESPONSE_SCHEMA_VERSION,
            # Full-document structured extraction on a slow gateway can take
            # well over the 60s default; 120s stays inside the 300s cap.
            timeout_seconds=120.0,
            json_schema={
                "name": "article_extraction",
                "strict": True,
                "schema": wire_schema,
            },
        )

        # 2. Consume the provider event stream, keeping only completed.text.
        text_payload: str | None = None
        async for event in self._provider.complete(request):
            if not isinstance(event, NormalizedEvent):
                raise ProviderError("invalid_response")
            if event.type == "completed":
                if event.text is None or not isinstance(event.text, str):
                    raise ProviderError("invalid_response")
                text_payload = event.text
        if text_payload is None:
            raise ProviderError("invalid_response")

        # 3. Parse strictly (no NaN/Infinity) and validate against the schema.
        try:
            payload = json.loads(text_payload, parse_constant=_reject_json_constant)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise ProviderError("invalid_response") from None
        if not isinstance(payload, dict):
            raise ProviderError("invalid_response")
        try:
            document = ExtractionDocument.model_validate(payload)
        except ValidationError:
            raise ProviderError("invalid_response") from None

        # 4. Provenance: every source_ref must point at *this* paper and this
        #    exact content hash (pdf_sha256 is the paper artifact digest).
        expected_sha = paper_sha256.lower()
        for ref in document.source_refs:
            if ref.paper_id != paper_id:
                raise ProviderError("invalid_response")
            if ref.pdf_sha256 is None or ref.pdf_sha256.lower() != expected_sha:
                raise ProviderError("invalid_response")

        # 5. Safe projection — metadata plus the validated extraction only.
        return {
            "paper_id": paper_id,
            "content_sha256": content_sha256,
            "context_hash": context_hash,
            "provider_config_id": provider_config_id,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "policy_version": policy_version,
            "extraction": document.model_dump(),
        }
