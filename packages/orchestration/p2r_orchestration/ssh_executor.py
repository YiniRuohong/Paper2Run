"""Structured SSH targets and injectable verification protocol.

Deliberate safety boundary for this phase:

- Only a structured target (host/port/user/workdir) plus a *registered* recipe
  may be verified. No arbitrary command, argv, or shell string is accepted.
- Credentials are only accepted as a controlled local key reference or an SSH
  agent identifier. Passwords and private key material are rejected outright
  and are never persisted, logged, or echoed back.
- Verification returns a safe environment summary and a status; it never
  creates a Run. The real transport is left to a later controlled adapter —
  this module ships the protocol plus a fake implementation for tests.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_FORBIDDEN_AUTH_FIELDS = ("password", "private_key", "passphrase", "secret", "token")
_FORBIDDEN_TARGET_FIELDS = ("command", "argv", "shell", "script")


class SSHVerificationStatus(str, Enum):
    VERIFIED = "verified"
    FAILED = "failed"


class SSHRecipe(BaseModel):
    """A registered recipe the verifier is allowed to consider."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe_id: str = Field(min_length=1, max_length=255)
    recipe_version: str = Field(min_length=1, max_length=64)


class SSHTarget(BaseModel):
    """Structured SSH target. Credentials are references, never material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535, default=22)
    user: str = Field(min_length=1, max_length=128)
    workdir: str = Field(min_length=1, max_length=1024)
    auth: dict[str, Any]

    @model_validator(mode="before")
    @classmethod
    def reject_credentials_and_commands(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        for key in _FORBIDDEN_TARGET_FIELDS:
            if key in values:
                raise ValueError(f"field '{key}' is not allowed: arbitrary commands are rejected")
        for key in _FORBIDDEN_AUTH_FIELDS:
            if key in values:
                raise ValueError(f"field '{key}' is not allowed: credential material is rejected")
        auth = values.get("auth")
        if isinstance(auth, dict):
            for key in _FORBIDDEN_AUTH_FIELDS:
                if key in auth:
                    raise ValueError(f"auth field '{key}' is not allowed: credential material is rejected")
        return values

    @field_validator("auth")
    @classmethod
    def validate_auth(cls, auth: dict[str, Any]) -> dict[str, Any]:
        kind = auth.get("type")
        if kind not in {"agent", "key_reference"}:
            raise ValueError("auth.type must be 'agent' or 'key_reference'")
        if kind == "agent" and not auth.get("agent_id"):
            raise ValueError("agent auth requires agent_id")
        if kind == "key_reference" and not auth.get("key_reference"):
            raise ValueError("key_reference auth requires key_reference")
        return auth

    def safe_projection(self) -> dict[str, Any]:
        """Public projection: never includes any credential material."""
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "workdir": self.workdir,
            "auth_type": self.auth.get("type"),
        }


class SSHVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: SSHVerificationStatus
    summary: dict[str, Any] = Field(default_factory=dict)


class SSHVerifier(Protocol):
    def verify(self, target: SSHTarget, recipe: SSHRecipe) -> SSHVerificationResult: ...


class FakeSSHVerifier:
    """Deterministic fake transport for tests and the default runtime.

    It performs no network I/O. Override ``_probe`` to script success/failure.
    """

    def verify(self, target: SSHTarget, recipe: SSHRecipe) -> SSHVerificationResult:
        connected = bool(self._probe(target, recipe))
        return SSHVerificationResult(
            status=SSHVerificationStatus.VERIFIED if connected else SSHVerificationStatus.FAILED,
            summary={
                "connected": connected,
                "host": target.host,
                "port": target.port,
                "user": target.user,
                "workdir": target.workdir,
                "auth_type": target.auth.get("type"),
                "recipe_id": recipe.recipe_id,
                "recipe_version": recipe.recipe_version,
            },
        )

    def _probe(self, target: SSHTarget, recipe: SSHRecipe) -> bool:
        return True


def verify_target(verifier: SSHVerifier, target: SSHTarget, recipe: SSHRecipe | None) -> SSHVerificationResult:
    """Invoke an injected verifier with exactly (target, registered recipe)."""
    if recipe is None:
        raise ValueError("a registered recipe is required for SSH verification")
    return verifier.verify(target, recipe)
