"""Runtime-only Provider session and secret store.

Secrets live only in this process: they are never serialized into DTOs,
databases, artifacts, or logs. `ProviderSession` is a frozen, non-secret
projection; the store holds the actual key in a private dict keyed by an
unpredictable session id.
"""
from __future__ import annotations

import secrets
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .providers import Capabilities, ProviderConfig


class ProviderSessionClosedError(LookupError):
    """Raised when a session id is unknown or has been cleared."""


class ProviderSession(BaseModel):
    """Safe, non-secret projection handed to callers.

    Deliberately has no api_key field: model_dump()/repr() can never leak it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=64, alias="provider_session_id")
    endpoint_alias: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=256)
    base_url: str = Field(repr=False)
    secret_configured: bool = True
    secret_last4: str = ""

    def public_summary(self) -> dict[str, Any]:
        return {
            "provider_session_id": self.id,
            "endpoint_alias": self.endpoint_alias,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "secret_configured": self.secret_configured,
            "secret_last4": self.secret_last4,
        }


def _mask_last4(api_key: str) -> str:
    """UI-only hint; never usable as a credential and never contains the key."""
    tail = api_key[-4:] if len(api_key) >= 4 else ""
    return "\u2022\u2022\u2022\u2022" + tail if tail else "\u2022\u2022\u2022\u2022"


class ProviderSecretStore:
    """Process-local store. `put`/`get`/`clear`/`public_summary` only."""

    def __init__(self) -> None:
        self._secrets: dict[str, str] = {}
        self._sessions: dict[str, ProviderSession] = {}
        # Non-secret validated config retained so config() replays the exact
        # capabilities/revision the caller declared. Never serialized.
        self._configs: dict[str, ProviderConfig] = {}

    def put(
        self,
        *,
        base_url: str,
        endpoint_alias: str,
        model_id: str,
        api_key: str,
        display_name: str | None = None,
        revision: str = "1",
        allow_local_http: bool = False,
        capabilities: Capabilities | None = None,
    ) -> ProviderSession:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key is required")
        if any(ord(c) < 32 or ord(c) == 127 for c in api_key):
            raise ValueError("api_key must not contain control characters")
        # Reuse ProviderConfig's URL safety rules verbatim.
        config = ProviderConfig(
            id=endpoint_alias,
            revision=revision,
            model_id=model_id,
            display_name=display_name or endpoint_alias,
            endpoint_alias=endpoint_alias,
            base_url=base_url,
            # secret_env is never read from the environment for sessions; it is
            # only a syntactic requirement of ProviderConfig.
            secret_env="P2R_UNUSED_SESSION_SECRET",
            capabilities=capabilities or Capabilities(),
            allow_local_http=allow_local_http,
        )
        session = ProviderSession(
            provider_session_id="ps_" + secrets.token_urlsafe(18),
            endpoint_alias=config.endpoint_alias,
            model_id=config.model_id,
            base_url=config.base_url,
            secret_configured=True,
            secret_last4=_mask_last4(api_key),
        )
        self._secrets[session.id] = api_key
        self._sessions[session.id] = session
        self._configs[session.id] = config
        return session

    def get(self, provider_session_id: str) -> str:
        if provider_session_id not in self._secrets:
            raise ProviderSessionClosedError("provider session is not available")
        return self._secrets[provider_session_id]

    def clear(self, provider_session_id: str) -> None:
        self._secrets.pop(provider_session_id, None)
        self._sessions.pop(provider_session_id, None)
        self._configs.pop(provider_session_id, None)

    def public_summary(self, provider_session_id: str) -> dict[str, Any]:
        session = self._sessions.get(provider_session_id)
        if session is None or provider_session_id not in self._secrets:
            raise ProviderSessionClosedError("provider session is not available")
        return session.public_summary()

    def config(self, provider_session_id: str) -> ProviderConfig:
        """Rebuild the validated ProviderConfig for a live session.

        get() confirms the secret is still present; the stored config replays
        the exact capabilities/revision declared at put() time.
        """
        self.get(provider_session_id)
        config = self._configs.get(provider_session_id)
        if config is None:
            raise ProviderSessionClosedError("provider session is not available")
        return config

    def latest_session_id(self, endpoint_alias: str, model_id: str) -> str | None:
        """Return the newest live session matching endpoint and model."""
        for session_id in reversed(self._sessions):
            session = self._sessions[session_id]
            if (session.endpoint_alias == endpoint_alias
                    and session.model_id == model_id
                    and session_id in self._secrets):
                return session_id
        return None

    def __contains__(self, provider_session_id: object) -> bool:
        return provider_session_id in self._secrets
