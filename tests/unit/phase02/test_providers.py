from __future__ import annotations

import asyncio
import json
import urllib.error

import pytest
from pydantic import ValidationError

from p2r_orchestration.providers import (
    Capabilities, CompletionRequest, Message, OpenAICompatibleProvider,
    ProviderConfig, ProviderError, TransportResponse, urllib_transport,
)


KEY = "test-private-key"


def config(**changes):
    return ProviderConfig(**dict({
        "id": "local", "revision": "1", "model_id": "model-a",
        "display_name": "Test", "endpoint_alias": "test-endpoint",
        "base_url": "https://example.invalid/v1", "secret_env": "P2R_TEST_KEY",
        "capabilities": Capabilities(text="supported"),
    }, **changes))


def request(**changes):
    return CompletionRequest(**dict({
        "provider_config_id": "local", "model_id": "model-a", "phase": "plan",
        "call_id": "call-1", "messages": [Message(role="user", content="Hello")],
        "context_hash": "hash", "prompt_version": "1", "policy_version": "1",
    }, **changes))


def reply(**changes):
    return json.dumps(dict({"model": "model-a", "choices": [
        {"message": {"role": "assistant", "content": "Hello back"}, "finish_reason": "stop"}
    ]}, **changes)).encode()


class FakeTransport:
    def __init__(self, body=None, status=200, error=None):
        self.body = reply() if body is None else body
        self.status = status
        self.error = error
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return TransportResponse(status=self.status, body=self.body)


def collect(provider, req=None):
    async def run():
        return [event async for event in provider.complete(req or request())]
    return asyncio.run(run())


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("P2R_TEST_KEY", KEY)


def test_response_schema_version_is_optional_structured_metadata():
    assert request().response_schema_version is None
    assert request(response_schema_version="article-extraction.v1").response_schema_version == "article-extraction.v1"
    with pytest.raises(ValidationError):
        request(response_schema_version="")
    with pytest.raises(ValidationError):
        request(response_schema_version="x" * 65)


def test_success_and_usage():
    transport = FakeTransport(reply(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}))
    events = collect(OpenAICompatibleProvider(config(), transport=transport))
    assert [event.type for event in events] == ["usage", "completed"]
    assert events[0].usage.status == "reported"
    assert events[0].usage.total_tokens == 7
    assert events[1].text == "Hello back"
    assert events[1].call_id == "call-1"
    sent = transport.calls[0]
    assert sent["url"] == "https://example.invalid/v1/chat/completions"
    assert sent["headers"]["Authorization"] == f"Bearer {KEY}"
    body = json.loads(sent["body"])
    assert body["stream"] is False and body["model"] == "model-a"
    assert "policy_version" not in body


def test_missing_secret_and_identity_mismatch(monkeypatch):
    transport = FakeTransport()
    provider = OpenAICompatibleProvider(config(), transport=transport)
    monkeypatch.delenv("P2R_TEST_KEY")
    with pytest.raises(ProviderError, match="auth_invalid"):
        collect(provider)
    for change in ({"model_id": "other"}, {"provider_config_id": "other"}):
        with pytest.raises(ProviderError, match="config_mismatch"):
            collect(provider, request(**change))
    assert not transport.calls


@pytest.mark.parametrize("feature", ["text", "structured_output", "streaming", "tools", "image"])
def test_unknown_capability_never_silently_falls_back(feature):
    transport = FakeTransport()
    changes = {"text": {}, "structured_output": {"json_schema": {"type": "object"}},
               "streaming": {"stream": True}, "tools": {"tools": [{"type": "function"}]},
               "image": {"image": True}}[feature]
    provider = OpenAICompatibleProvider(config(capabilities=Capabilities()), transport=transport)
    with pytest.raises(ProviderError, match="capability_unverified"):
        collect(provider, request(**changes))
    assert not transport.calls


@pytest.mark.parametrize("changes", [{"stream": True}, {"tools": [{"type": "function"}]}, {"image": True}])
def test_unimplemented_features_rejected_even_when_configured(changes):
    provider = OpenAICompatibleProvider(config(capabilities=Capabilities(
        text="supported", streaming="supported", tools="supported", image="supported")), transport=FakeTransport())
    with pytest.raises(ProviderError, match="capability_unsupported"):
        collect(provider, request(**changes))


def test_structured_output_requires_explicit_support():
    transport = FakeTransport(reply(choices=[
        {"message": {"content": "{}"}, "finish_reason": "stop"}
    ]))
    provider = OpenAICompatibleProvider(config(capabilities=Capabilities(text="supported", structured_output="supported")), transport=transport)
    collect(provider, request(json_schema={"type": "object"}))
    assert json.loads(transport.calls[0]["body"])["response_format"]["type"] == "json_schema"


def test_developer_role_is_allowed():
    assert Message(role="developer", content="Be precise")


@pytest.mark.parametrize("finish_reason", ["length", "unknown", "tool_calls"])
def test_non_stop_finish_reason_is_not_completed(finish_reason):
    body = reply(choices=[{"message": {"content": "partial"}, "finish_reason": finish_reason}])
    with pytest.raises(ProviderError, match="invalid_response"):
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport(body)))


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity", "plain text", '{"x":NaN}'])
def test_json_schema_output_must_be_strict_json(text):
    provider = OpenAICompatibleProvider(config(capabilities=Capabilities(text="supported", structured_output="supported")), transport=FakeTransport(reply(choices=[
        {"message": {"content": text}, "finish_reason": "stop"}
    ])))
    with pytest.raises(ProviderError, match="invalid_response"):
        collect(provider, request(json_schema={"type": "object"}))


def test_json_response_rejects_nonstandard_nan():
    body = b'{"model":"model-a","choices":[{"message":{"content":NaN},"finish_reason":"stop"}]}'
    with pytest.raises(ProviderError, match="invalid_response"):
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport(body)))


def test_cancelled_transport_propagates_cancellation():
    async def cancelled(**kwargs):
        raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        collect(OpenAICompatibleProvider(config(), transport=cancelled))


def test_secret_with_control_character_is_auth_invalid(monkeypatch):
    monkeypatch.setenv("P2R_TEST_KEY", "bad\nkey")
    with pytest.raises(ProviderError, match="auth_invalid"):
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport()))


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{}{}', reply(model=7),
    reply(choices=[]), reply(choices=[{"message": None}]), reply(usage=[]),
    reply(choices=[{"message": {"content": None}}]),
    reply(usage={"prompt_tokens": -1}), reply(usage={"total_tokens": True}),
    reply(choices=[{"message": {"content": KEY}, "finish_reason": "stop"}])])
def test_malformed_and_secret_echo_are_sanitized(body):
    with pytest.raises(ProviderError) as exc:
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport(body)))
    assert exc.value.code == "invalid_response"
    assert KEY not in str(exc.value)
    assert "example.invalid" not in repr(exc.value)
    assert exc.value.__cause__ is None


@pytest.mark.parametrize("status,code", [(401, "auth_invalid"), (403, "permission_denied"),
    (429, "rate_limited"), (408, "timeout"), (500, "server_error"), (302, "invalid_response"),
    (400, "invalid_response")])
def test_http_errors_no_retry_or_sensitive_body(status, code):
    transport = FakeTransport(f"{KEY} https://example.invalid".encode(), status=status)
    with pytest.raises(ProviderError) as exc:
        collect(OpenAICompatibleProvider(config(), transport=transport))
    assert exc.value.code == code
    assert KEY not in str(exc.value) and "https" not in str(exc.value)
    # Only a 400 is retried, once, without max_tokens (server output cap).
    assert len(transport.calls) == (2 if status == 400 else 1)


def structured_provider(transport):
    return OpenAICompatibleProvider(config(capabilities=Capabilities(text="supported", structured_output="supported")), transport=transport)


def test_structured_output_degrades_on_400_and_never_weakens_parsing():
    class Ladder(FakeTransport):
        async def __call__(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) < 4:
                return TransportResponse(status=400, body=b"")
            return TransportResponse(status=200, body=reply(choices=[
                {"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}]))
    transport = Ladder()
    events = collect(structured_provider(transport), request(json_schema={"name": "x", "schema": {"type": "object"}}))
    sent = [json.loads(call["body"]) for call in transport.calls]
    assert [body.get("response_format", {}).get("type") for body in sent] == ["json_schema", "json_object", None, None]
    assert ["max_tokens" in body for body in sent] == [True, True, True, False]
    assert events[-1].text == '{"ok": true}'


@pytest.mark.parametrize("content,expected", [
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('Here is the result:\n{"a": [1, 2]}\nHope it helps.', '{"a": [1, 2]}'),
    ('  {"a": 1}  ', '{"a": 1}'),
])
def test_structured_output_extracts_json_from_fences_and_prose(content, expected):
    transport = FakeTransport(reply(choices=[{"message": {"content": content}, "finish_reason": "stop"}]))
    events = collect(structured_provider(transport), request(json_schema={"type": "object"}))
    assert json.loads(events[-1].text) == json.loads(expected)


def test_nested_fragment_of_rejected_document_is_not_promoted():
    content = 'note: {"x": NaN, "inner": {"a": 1}}'
    transport = FakeTransport(reply(choices=[{"message": {"content": content}, "finish_reason": "stop"}]))
    with pytest.raises(ProviderError, match="invalid_response"):
        collect(structured_provider(transport), request(json_schema={"type": "object"}))


@pytest.mark.parametrize("changes", [
    {"model": "vendor/model-a-2026-01-01"},
    {"choices": [{"message": {"content": "Hello back"}}]},
    {"choices": [{"message": {"content": "Hello back"}, "finish_reason": "end_turn"}]},
    {"choices": [{"message": {"content": [{"type": "reasoning", "text": "hmm"}, {"type": "text", "text": "Hello back"}]}, "finish_reason": "stop"}]},
])
def test_gateway_variations_are_accepted(changes):
    events = collect(OpenAICompatibleProvider(config(), transport=FakeTransport(reply(**changes))))
    assert events[-1].text == "Hello back"


@pytest.mark.parametrize("error,code", [(TimeoutError(KEY), "timeout"), (OSError(KEY), "network_transient")])
def test_transport_errors_sanitized(error, code):
    transport = FakeTransport(error=error)
    with pytest.raises(ProviderError) as exc:
        collect(OpenAICompatibleProvider(config(), transport=transport))
    assert exc.value.code == code and KEY not in str(exc.value)
    assert len(transport.calls) == 1


def test_timeout_bounds_async_transport():
    async def slow(**kwargs):
        await asyncio.sleep(1)
    with pytest.raises(ProviderError, match="timeout"):
        collect(OpenAICompatibleProvider(config(), transport=slow), request(timeout_seconds=0.01))


def test_oversize_and_unknown_usage():
    with pytest.raises(ProviderError, match="invalid_response"):
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport(b"x" * 101), max_response_bytes=100))
    usage = collect(OpenAICompatibleProvider(config(), transport=FakeTransport()))[0].usage
    assert usage.status == "unknown" and usage.total_tokens is None


def test_config_safety_and_immutability():
    cfg = config()
    assert "https" not in repr(cfg) and "P2R_TEST_KEY" not in repr(cfg)
    assert "https" not in str(cfg) and "P2R_TEST_KEY" not in str(cfg)
    assert set(cfg.public_summary()) == {"id", "display_name", "endpoint_alias", "capabilities"}
    assert "secret_env" not in json.dumps(cfg.public_summary())
    with pytest.raises(ValidationError):
        cfg.model_id = "changed"
    with pytest.raises(ValidationError):
        cfg.capabilities.text = "unsupported"


@pytest.mark.parametrize("url", ["http://example.invalid", "https://user:pass@example.invalid", "https://example.invalid?q=x", "https://example.invalid#x", "file:///tmp/test", "https://example.invalid?", "https://example.invalid/#"])
def test_unsafe_urls_rejected(url):
    with pytest.raises(ValidationError):
        config(base_url=url)


def test_loopback_requires_explicit_opt_in():
    with pytest.raises(ValidationError):
        config(base_url="http://127.0.0.1:8000/v1")
    assert config(base_url="http://127.0.0.1:8000/v1", allow_local_http=True)
    with pytest.raises(ValidationError):
        config(base_url="http://example.invalid", allow_local_http=True)


@pytest.mark.parametrize("changes", [{"timeout_seconds": 0}, {"timeout_seconds": 301}, {"max_output_tokens": 0}, {"max_output_tokens": 1000001}, {"messages": []}])
def test_request_bounds(changes):
    with pytest.raises(ValidationError):
        request(**changes)


def test_probe_and_estimate_do_not_call_network():
    transport = FakeTransport()
    provider = OpenAICompatibleProvider(config(), transport=transport)
    probe = asyncio.run(provider.probe_capabilities())
    assert probe.verified is False and probe.capabilities.image == "unknown"
    estimate = asyncio.run(provider.estimate_usage(request()))
    assert estimate.authoritative is False and estimate.cost is None
    assert not transport.calls


def test_content_policy():
    with pytest.raises(ProviderError, match="content_policy"):
        collect(OpenAICompatibleProvider(config(), transport=FakeTransport(reply(choices=[
            {"message": {"content": None}, "finish_reason": "content_filter"}]))))


def test_default_transport_no_redirect_and_bounded_read(monkeypatch):
    import p2r_orchestration.providers as module
    calls = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            assert size == 101
            return b"x" * size
    class Opener:
        def open(self, req, timeout):
            calls.append(req)
            assert req.method == "POST"
            return Response()
    def build(*handlers):
        redirect = next(h for h in handlers if isinstance(h, module.urllib.request.HTTPRedirectHandler))
        assert redirect.redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid") is None
        return Opener()
    monkeypatch.setattr(module.urllib.request, "build_opener", build)
    with pytest.raises(ProviderError, match="invalid_response"):
        asyncio.run(urllib_transport(url="https://example.invalid/v1/chat/completions", headers={}, body=b"{}", timeout=1, max_bytes=100))
    assert len(calls) == 1


def test_default_transport_http_error_does_not_read_body(monkeypatch):
    import p2r_orchestration.providers as module
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.HTTPError("https://example.invalid", 401, KEY, {}, None)
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: Opener())
    response = asyncio.run(urllib_transport(url="https://example.invalid", headers={}, body=b"{}", timeout=1, max_bytes=100))
    assert response.status == 401 and response.body == b""
