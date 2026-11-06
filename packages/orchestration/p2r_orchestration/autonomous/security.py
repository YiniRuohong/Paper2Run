"""Bounded safe projections and process-local model credentials."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from urllib.parse import urlsplit

from p2r_contracts.autonomous import DEFAULT_CONTEXT_WINDOW, DEFAULT_MAX_TOKENS
from p2r_orchestration.errors import ServiceError


def fail(code: str, message: str, status: int = 422):
    error = ServiceError(message)
    error.code, error.status = code, status
    raise error


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class ProfileStore:
    """Actor-scoped; public profiles can be frozen, credentials cannot."""
    def __init__(self, *, environment_grants=()):
        self.lock = threading.RLock()
        self.profiles = {}
        self.secrets = set()
        # Trusted bootstrap only; never populated from an HTTP request or task
        # snapshot. Bind each reference to the full destination/model identity.
        self.environment_grants = frozenset(tuple(grant) for grant in environment_grants)

    def put(self, actor: str, profile: dict) -> dict:
        value = dict(profile)
        parsed = urlsplit(value["base_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            fail("VALIDATION_ERROR", "base_url must be an HTTP(S) endpoint without credentials/query")
        auth_mode = value.get("auth_mode", "api-key")
        if auth_mode not in ("api-key", "none"):
            fail("VALIDATION_ERROR", "unsupported auth mode")
        is_loopback = parsed.hostname.lower() in ("localhost", "127.0.0.1", "::1")
        if auth_mode == "none":
            if parsed.scheme != "http" or not is_loopback or value.get("api_key") or value.get("credential_ref"):
                fail("VALIDATION_ERROR", "anonymous auth requires an HTTP loopback endpoint without credentials")
        elif value.get("api_key") is not None and (not isinstance(value["api_key"], str) or not value["api_key"].strip()):
            fail("VALIDATION_ERROR", "api_key must be non-empty")
        reference = value.get("credential_ref")
        if reference and not re.fullmatch(r"env:[A-Za-z_][A-Za-z0-9_]*|credential-store:[A-Za-z0-9_.-]{1,128}", reference):
            fail("VALIDATION_ERROR", "unsupported credential reference")
        if value.get("max_tokens", DEFAULT_MAX_TOKENS) > value.get("context_window", DEFAULT_CONTEXT_WINDOW):
            fail("VALIDATION_ERROR", "max_tokens exceeds context_window")
        self._environment(actor, value)  # authorization precedes any host lookup
        reuse = value.pop("reuse_credentials_from", None)
        with self.lock:
            if reuse is not None:
                if not isinstance(reuse, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", reuse):
                    fail("VALIDATION_ERROR", "invalid credential source profile")
                if auth_mode != "api-key" or value.get("api_key") is not None or value.get("credential_ref"):
                    fail("VALIDATION_ERROR", "credential reuse cannot be combined with another auth source")
                source = self.profiles.get((actor, reuse))
                if not source or any(source.get(k) != value.get(k) for k in ("base_url", "api")):
                    fail("CREDENTIAL_REFERENCE_FORBIDDEN", "credential reuse requires your own profile at the same endpoint and API", 403)
                # Environment grants are bound to a full model identity; copying
                # their value would silently broaden that grant. Only explicit
                # process-local API keys may be reused by this user operation.
                if source.get("credential_ref") or source.get("auth_mode", "api-key") != "api-key" or not source.get("api_key"):
                    fail("CREDENTIAL_REQUIRED", "source must contain an explicit in-memory API key", 409)
                value["api_key"] = source["api_key"]
            self.profiles[(actor, value["id"])] = value
            if value.get("api_key"):
                self.secrets.add(value["api_key"])
            return self.public(value, actor)

    def public(self, value, actor):
        return {**{k: v for k, v in value.items() if k != "api_key"},
                "auth_mode": value.get("auth_mode", "api-key"),
                "secret_configured": bool(value.get("api_key") or self._environment(actor, value))}

    def _environment(self, actor, profile):
        reference = profile.get("credential_ref")
        if not reference or not reference.startswith("env:"):
            return None
        grant = (actor, profile["id"], profile["base_url"], profile["api"], profile["model"], reference)
        if grant not in self.environment_grants:
            fail("CREDENTIAL_REFERENCE_FORBIDDEN", "environment credential reference is not authorized for this actor and endpoint", 403)
        return os.environ.get(reference[4:])

    def list(self, actor):
        with self.lock:
            return [self.public(p, actor) for (owner, _), p in self.profiles.items() if owner == actor]

    def resolve(self, actor, profile_id, snapshot=None):
        with self.lock:
            value = self.profiles.get((actor, profile_id)) or snapshot
            if not value:
                fail("CREDENTIAL_REQUIRED", "configure the selected model profile", 409)
            # Frozen model identity cannot silently change when a profile is edited.
            frozen = snapshot or value
            secret = self._environment(actor, frozen)
            if not secret and value and all(value.get(k) == frozen.get(k) for k in ("id", "model", "base_url", "api")):
                secret = value.get("api_key")
            if frozen.get("auth_mode", "api-key") == "none":
                if frozen.get("api_key") or frozen.get("credential_ref"):
                    fail("VALIDATION_ERROR", "anonymous profile cannot carry credentials")
                return {"id": frozen["model"], "base_url": frozen["base_url"], "api": frozen["api"],
                        "auth_mode": "none", "api_key": "", "context_window": frozen.get("context_window", DEFAULT_CONTEXT_WINDOW),
                        "max_tokens": frozen.get("max_tokens", DEFAULT_MAX_TOKENS)}
            if not secret:
                fail("CREDENTIAL_REQUIRED", "reconfigure credentials for the frozen model profile", 409)
            self.secrets.add(secret)
            return {"id": frozen["model"], "base_url": frozen["base_url"], "api": frozen["api"],
                    "auth_mode": "api-key", "api_key": secret, "context_window": frozen.get("context_window", DEFAULT_CONTEXT_WINDOW),
                    "max_tokens": frozen.get("max_tokens", DEFAULT_MAX_TOKENS)}

    def snapshot(self, actor, profile_id, credential_ref=None):
        with self.lock:
            value = self.profiles.get((actor, profile_id))
            if not value:
                fail("CREDENTIAL_REQUIRED", "configure the selected model profile", 409)
            snapshot = dict(value)
            if credential_ref and credential_ref.startswith("env:"):
                snapshot["credential_ref"] = credential_ref
            self.resolve(actor, profile_id, snapshot)
            return self.public(snapshot, actor)

    def reject_credentials(self, value):
        raw = json.dumps(value, ensure_ascii=False)
        with self.lock:
            if any(secret in raw for secret in self.secrets):
                fail("SECRET_IN_INPUT", "runtime credentials cannot be persisted or passed to experiment tools")

    def clean(self, value, *, bounded=True):
        with self.lock:
            secrets = tuple(self.secrets)
        def scrub(item, depth=0):
            if depth > 16:
                return "[depth limit]"
            if isinstance(item, dict):
                return {str(k): ("[REDACTED]" if str(k) not in {"secret_configured", "authorization_confirmed"} and re.search(r"api.?key|^authorization$|password|secret|access.?token|private.?key", str(k), re.I)
                                else scrub(v, depth + 1)) for k, v in list(item.items())[:256]}
            if isinstance(item, (tuple, list)):
                return [scrub(v, depth + 1) for v in (item[:256] if bounded else item)]
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "[REDACTED]")
                item = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", item)
                return item[:16000] if bounded else item
            if item is None or isinstance(item, (int, float, bool)):
                return item
            return scrub(str(item), depth + 1)
        result = scrub(value)
        if bounded and len(json.dumps(result)) > 64000:
            return {"truncated": True, "preview": json.dumps(result)[:32000]}
        return result
