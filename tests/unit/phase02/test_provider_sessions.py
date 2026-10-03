"""Runtime-only Provider secret store: lifecycle, projection, and non-persistence."""
from __future__ import annotations

import json

import pytest

from p2r_orchestration.provider_sessions import (
    ProviderSessionClosedError,
    ProviderSecretStore,
    ProviderSession,
)


KEY = "sk-test-private-key-1234567890"


def store():
    return ProviderSecretStore()


def test_put_get_returns_secret_then_clear_makes_it_unusable():
    s = store()
    session = s.put(base_url="https://example.invalid/v1", endpoint_alias="my-provider",
                    model_id="model-a", api_key=KEY, allow_local_http=False)
    assert isinstance(session, ProviderSession)
    # get() returns the raw secret for provider calls (server-internal only)
    assert s.get(session.id) == KEY
    # public projection never contains the secret
    summary = s.public_summary(session.id)
    assert summary["provider_session_id"] == session.id
    assert summary["endpoint_alias"] == "my-provider"
    assert summary["model_id"] == "model-a"
    assert summary["secret_configured"] is True
    assert KEY not in json.dumps(summary)
    # clear: subsequent get/public_summary fail, secret unrecoverable
    s.clear(session.id)
    with pytest.raises(ProviderSessionClosedError):
        s.get(session.id)
    with pytest.raises(ProviderSessionClosedError):
        s.public_summary(session.id)


def test_secret_not_in_repr_or_model_dump():
    s = store()
    session = s.put(base_url="https://example.invalid/v1", endpoint_alias="a",
                    model_id="m", api_key=KEY)
    assert KEY not in repr(session)
    assert KEY not in str(session)
    assert KEY not in json.dumps(session.model_dump(mode="json"))
    assert "api_key" not in session.model_dump()


def test_masked_hint_is_safe_and_not_an_auth_credential():
    s = store()
    session = s.put(base_url="https://example.invalid/v1", endpoint_alias="a",
                    model_id="m", api_key=KEY)
    summary = s.public_summary(session.id)
    # last-4 style mask for UI only; must not equal the secret
    assert summary["secret_last4"] != KEY
    assert KEY not in summary["secret_last4"]


def test_unsafe_endpoint_rejected_at_put():
    s = store()
    with pytest.raises(Exception):
        s.put(base_url="http://example.invalid/v1", endpoint_alias="a",
              model_id="m", api_key=KEY)
    with pytest.raises(Exception):
        s.put(base_url="https://user:pass@example.invalid/v1", endpoint_alias="a",
              model_id="m", api_key=KEY)
    with pytest.raises(Exception):
        s.put(base_url="https://example.invalid/v1?q=1", endpoint_alias="a",
              model_id="m", api_key=KEY)


def test_loopback_http_requires_explicit_opt_in():
    s = store()
    with pytest.raises(Exception):
        s.put(base_url="http://127.0.0.1:8000/v1", endpoint_alias="a",
              model_id="m", api_key=KEY)
    s.put(base_url="http://127.0.0.1:8000/v1", endpoint_alias="a",
          model_id="m", api_key=KEY, allow_local_http=True)


def test_get_after_close_of_unknown_id_raises():
    s = store()
    with pytest.raises(ProviderSessionClosedError):
        s.get("ps_missing")
    with pytest.raises(ProviderSessionClosedError):
        s.public_summary("ps_missing")


def test_two_stores_are_isolated():
    a, b = store(), store()
    session = a.put(base_url="https://example.invalid/v1", endpoint_alias="a",
                    model_id="m", api_key=KEY)
    with pytest.raises(ProviderSessionClosedError):
        b.get(session.id)
