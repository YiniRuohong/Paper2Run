from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Mapping
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Capability = Literal["supported", "unsupported", "unknown"]


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-standard JSON constant")


class Capabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: Capability = "unknown"
    structured_output: Capability = "unknown"
    streaming: Capability = "unknown"
    tools: Capability = "unknown"
    image: Capability = "unknown"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=64)
    model_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=256)
    endpoint_alias: str = Field(min_length=1, max_length=128)
    base_url: str = Field(repr=False)
    secret_env: str = Field(min_length=1, max_length=128, repr=False)
    capabilities: Capabilities = Field(default_factory=Capabilities)
    allow_local_http: bool = False

    @field_validator("secret_env")
    @classmethod
    def valid_secret_env(cls, value: str) -> str:
        if not value.isidentifier():
            raise ValueError("secret_env must be an environment variable name")
        return value

    @field_validator("base_url")
    @classmethod
    def safe_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise ValueError("base_url must use https, or opted-in loopback http")
        if parsed.scheme == "http" and not is_loopback:
            raise ValueError("http is limited to loopback")
        if "@" in parsed.netloc or "?" in value or "#" in value:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        if not parsed.hostname or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError("invalid base_url")
        _ = parsed.port
        return value.rstrip("/")

    @model_validator(mode="after")
    def local_http_opt_in(self) -> "ProviderConfig":
        if urlsplit(self.base_url).scheme == "http" and not self.allow_local_http:
            raise ValueError("loopback http requires allow_local_http")
        return self

    def public_summary(self) -> dict[str, Any]:
        return {"id": self.id, "display_name": self.display_name,
                "endpoint_alias": self.endpoint_alias,
                "capabilities": self.capabilities.model_dump()}

    def __repr__(self) -> str:
        return f"ProviderConfig(id={self.id!r}, revision={self.revision!r}, model_id={self.model_id!r}, display_name={self.display_name!r}, endpoint_alias={self.endpoint_alias!r}, capabilities={self.capabilities!r})"


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    content: str = Field(min_length=1, max_length=1_000_000)


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_config_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    phase: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=256)
    messages: list[Message] = Field(min_length=1, max_length=1000)
    context_hash: str = Field(min_length=1, max_length=256)
    prompt_version: str = Field(min_length=1, max_length=64)
    policy_version: str = Field(min_length=1, max_length=64)
    response_schema_version: str | None = Field(default=None, min_length=1, max_length=64)
    timeout_seconds: float = Field(default=60, gt=0, le=300)
    max_output_tokens: int = Field(default=4096, gt=0, le=1_000_000)
    stream: bool = False
    tools: list[dict[str, Any]] | None = None
    image: bool = False
    json_schema: dict[str, Any] | None = None


class Usage(BaseModel):
    status: Literal["reported", "unknown"]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class NormalizedEvent(BaseModel):
    type: Literal["usage", "completed"]
    call_id: str
    text: str | None = None
    usage: Usage | None = None


class ProbeResult(BaseModel):
    capabilities: Capabilities
    verified: bool = False
    note: str = "configured capabilities only; not network-tested"


class UsageEstimate(BaseModel):
    authoritative: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: None = None
    note: str = "non-authoritative estimate; cost is not inferred"


class TransportResponse:
    def __init__(self, *, status: int, body: bytes):
        self.status, self.body = status, body


class AsyncTransport(Protocol):
    async def __call__(self, **kwargs: Any) -> TransportResponse: ...


class ProviderError(Exception):
    _MESSAGES = {
        "auth_invalid": "provider authentication failed",
        "permission_denied": "provider permission denied",
        "rate_limited": "provider rate limited",
        "timeout": "provider request timed out",
        "network_transient": "provider network error",
        "invalid_response": "provider returned an invalid response",
        "content_policy": "provider content policy rejected the response",
        "server_error": "provider server error",
        "config_mismatch": "provider request does not match configuration",
        "capability_unverified": "requested provider capability is unverified",
        "capability_unsupported": "requested provider capability is unsupported",
    }

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"{code}: {self._MESSAGES.get(code, 'provider error')}")


# Some gateways run a WAF that blanket-bans Python-urllib's default
# User-Agent (Cloudflare error 1010); send an identifying product UA instead.
DEFAULT_USER_AGENT = "paper2run/0.1"


async def urllib_transport(*, url: str, headers: Mapping[str, str], body: bytes,
                           timeout: float, max_bytes: int) -> TransportResponse:
    def send() -> TransportResponse:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args: Any, **kwargs: Any) -> None:
                return None
        sent_headers = {"User-Agent": DEFAULT_USER_AGENT, **dict(headers)}
        request = urllib.request.Request(url, data=body, headers=sent_headers, method="POST")
        opener = urllib.request.build_opener(NoRedirect())
        try:
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            return TransportResponse(status=status, body=b"")
        with response:
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ProviderError("invalid_response")
            return TransportResponse(status=response.status, body=data)
    error = None
    try:
        return await asyncio.to_thread(send)
    except TimeoutError:
        error = "timeout"
    except urllib.error.URLError as exc:
        error = "timeout" if isinstance(exc.reason, TimeoutError) else "network_transient"
    except OSError:
        error = "network_transient"
    raise ProviderError(error)


class Provider(Protocol):
    async def probe_capabilities(self) -> ProbeResult: ...
    def complete(self, request: CompletionRequest) -> AsyncIterator[NormalizedEvent]: ...
    async def estimate_usage(self, request: CompletionRequest) -> UsageEstimate: ...


class OpenAICompatibleProvider:
    def __init__(self, config: ProviderConfig, *, transport: AsyncTransport | None = None,
                 max_response_bytes: int = 4 * 1024 * 1024,
                 secret_resolver: Callable[[], str] | None = None):
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 16 * 1024 * 1024:
            raise ValueError("max_response_bytes must be between 1 and 16 MiB")
        self.config = config
        self._transport = transport or urllib_transport
        self._max_response_bytes = max_response_bytes
        self._secret_resolver = secret_resolver

    async def probe_capabilities(self) -> ProbeResult:
        return ProbeResult(capabilities=self.config.capabilities)

    async def estimate_usage(self, request: CompletionRequest) -> UsageEstimate:
        return UsageEstimate()

    async def complete(self, request: CompletionRequest) -> AsyncIterator[NormalizedEvent]:
        """Validate JSON syntax for structured output; schema validation belongs to callers."""
        self._validate_request(request)
        key = self._secret_resolver() if self._secret_resolver is not None else os.environ.get(self.config.secret_env)
        if not key or any(ord(character) < 32 or ord(character) == 127 for character in key):
            raise ProviderError("auth_invalid")
        payload: dict[str, Any] = {
            "model": self.config.model_id,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": request.json_schema}
        try:
            response = await asyncio.wait_for(self._transport(
                url=f"{self.config.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                body=json.dumps(payload).encode(), timeout=request.timeout_seconds,
                max_bytes=self._max_response_bytes), timeout=request.timeout_seconds)
        except asyncio.TimeoutError:
            raise ProviderError("timeout") from None
        except ProviderError:
            raise
        except (TimeoutError,):
            raise ProviderError("timeout") from None
        except OSError:
            raise ProviderError("network_transient") from None
        if response.status in (401,):
            raise ProviderError("auth_invalid")
        if response.status == 403:
            raise ProviderError("permission_denied")
        if response.status == 429:
            raise ProviderError("rate_limited")
        if response.status == 408:
            raise ProviderError("timeout")
        if 500 <= response.status:
            raise ProviderError("server_error")
        if response.status != 200 or len(response.body) > self._max_response_bytes:
            raise ProviderError("invalid_response")
        try:
            data = json.loads(response.body, parse_constant=_reject_json_constant)
            if not isinstance(data, dict) or data.get("model") not in (None, self.config.model_id):
                raise ValueError
            choices = data.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ValueError
            choice = choices[0]
            if choice.get("finish_reason") == "content_filter":
                raise ProviderError("content_policy")
            if choice.get("finish_reason") != "stop":
                raise ValueError
            message = choice.get("message")
            if not isinstance(message, dict):
                raise ValueError
            if message.get("refusal"):
                raise ProviderError("content_policy")
            if message.get("tool_calls") or message.get("function_call"):
                raise ValueError
            text = message.get("content")
            if not isinstance(text, str) or key in text:
                raise ValueError
            if request.json_schema is not None:
                json.loads(text, parse_constant=_reject_json_constant)
            usage_data = data.get("usage")
            usage = self._usage(usage_data)
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            raise ProviderError("invalid_response") from None
        yield NormalizedEvent(type="usage", call_id=request.call_id, usage=usage)
        yield NormalizedEvent(type="completed", call_id=request.call_id, text=text, usage=usage)

    def _validate_request(self, request: CompletionRequest) -> None:
        if request.provider_config_id != self.config.id or request.model_id != self.config.model_id:
            raise ProviderError("config_mismatch")
        for feature, requested, configured in (
            ("streaming", request.stream, self.config.capabilities.streaming),
            ("tools", request.tools is not None, self.config.capabilities.tools),
            ("image", request.image, self.config.capabilities.image),
            ("structured_output", request.json_schema is not None, self.config.capabilities.structured_output),
        ):
            if requested:
                if configured == "unknown":
                    raise ProviderError("capability_unverified")
                if configured != "supported" or feature in {"streaming", "tools", "image"}:
                    raise ProviderError("capability_unsupported")
        if self.config.capabilities.text == "unknown":
            raise ProviderError("capability_unverified")
        if self.config.capabilities.text != "supported":
            raise ProviderError("capability_unsupported")

    @staticmethod
    def _usage(data: Any) -> Usage:
        if data is None:
            return Usage(status="unknown")
        if not isinstance(data, dict):
            raise ValueError
        values = {name: data.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in values.values() if value is not None):
            raise ValueError
        if all(value is None for value in values.values()):
            return Usage(status="unknown")
        return Usage(status="reported", **values)
