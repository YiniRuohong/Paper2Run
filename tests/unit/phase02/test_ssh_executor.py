from __future__ import annotations

import pytest
from pydantic import ValidationError

from p2r_orchestration.ssh_executor import (
    FakeSSHVerifier,
    SSHRecipe,
    SSHTarget,
    SSHVerificationResult,
    SSHVerificationStatus,
    verify_target,
)


RECIPE = SSHRecipe(recipe_id="fixture", recipe_version="1")


def target(**overrides):
    payload = {
        "host": "10.0.0.5",
        "port": 22,
        "user": "runner",
        "workdir": "/srv/p2r",
        "auth": {"type": "agent", "agent_id": "agent-default"},
    }
    payload.update(overrides)
    return payload


def test_target_schema_requires_host_port_user_and_workdir():
    base = {"host": "10.0.0.5", "port": 22, "user": "runner", "workdir": "/srv/p2r"}
    # a complete, valid target is accepted
    SSHTarget.model_validate({**base, "auth": {"type": "agent", "agent_id": "a"}})
    # every structural field is required
    for missing in ("host", "port", "user", "workdir", "auth"):
        payload = dict(base)
        payload.pop(missing, None)
        candidate = dict(payload)
        if missing != "auth":
            candidate["auth"] = {"type": "agent", "agent_id": "a"}
        if missing == "port":
            # port must be present but may not be omitted silently; None is invalid
            candidate["port"] = None
        with pytest.raises(ValidationError):
            SSHTarget.model_validate(candidate)


def test_target_rejects_password_and_private_key_content():
    for payload in (
        target(auth={"type": "password", "password": "secret"}),
        target(auth={"type": "private_key", "private_key": "-----BEGIN"}),
        target(password="secret"),
        target(private_key="-----BEGIN"),
        target(ssh_private_key="-----BEGIN"),
    ):
        with pytest.raises(ValidationError):
            SSHTarget.model_validate(payload)


def test_target_accepts_only_controlled_key_reference_or_agent_id():
    ok = SSHTarget.model_validate(target(auth={"type": "key_reference", "key_reference": "keys/runner"}))
    assert ok.auth["type"] == "key_reference"
    ok = SSHTarget.model_validate(target(auth={"type": "agent", "agent_id": "agent-default"}))
    assert ok.auth["agent_id"] == "agent-default"
    # agent id / key reference are mandatory for their respective types
    with pytest.raises(ValidationError):
        SSHTarget.model_validate(target(auth={"type": "agent"}))
    with pytest.raises(ValidationError):
        SSHTarget.model_validate(target(auth={"type": "key_reference"}))


def test_target_rejects_arbitrary_command_or_argv():
    for payload in (target(command="rm -rf /"), target(argv=["bash", "-c", "x"]), target(shell=True)):
        with pytest.raises(ValidationError):
            SSHTarget.model_validate(payload)


def test_fake_verifier_verified_returns_safe_environment_summary():
    calls = []

    class Recorder(FakeSSHVerifier):
        def _probe(self, target, recipe):
            calls.append((target, recipe))
            return True

    result = Recorder().verify(SSHTarget.model_validate(target()), RECIPE)
    assert isinstance(result, SSHVerificationResult)
    assert result.status is SSHVerificationStatus.VERIFIED
    assert result.summary
    # the summary is a safe projection: no credentials of any kind
    flat = str(result.summary)
    assert "password" not in flat.lower()
    assert "BEGIN" not in flat
    assert len(calls) == 1
    assert isinstance(calls[0][0], SSHTarget)
    assert calls[0][1].recipe_id == "fixture"


def test_fake_verifier_failed_returns_status_without_summary_secret():
    class Recorder(FakeSSHVerifier):
        def _probe(self, target, recipe):
            return False

    result = Recorder().verify(SSHTarget.model_validate(target()), RECIPE)
    assert result.status is SSHVerificationStatus.FAILED
    assert result.summary.get("connected") is False


def test_verify_target_requires_registered_recipe():
    verifier = FakeSSHVerifier()
    with pytest.raises(Exception):
        verify_target(verifier, SSHTarget.model_validate(target()), None)


def test_verifier_protocol_is_injectable_and_takes_target_and_recipe_only():
    class OnlyShape:
        def verify(self, t, r):
            assert isinstance(t, SSHTarget)
            assert isinstance(r, SSHRecipe)
            return SSHVerificationResult(
                status=SSHVerificationStatus.VERIFIED,
                summary={"connected": True, "recipe_id": r.recipe_id},
            )

    result = verify_target(OnlyShape(), SSHTarget.model_validate(target()), RECIPE)
    assert result.status is SSHVerificationStatus.VERIFIED
    assert result.summary["recipe_id"] == "fixture"
