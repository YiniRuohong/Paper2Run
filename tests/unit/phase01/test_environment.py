from __future__ import annotations

import hashlib
import json
from pathlib import Path

from p2r_orchestration.environment import environment_snapshot


def test_environment_snapshot_is_safe_json_and_redacted(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("API_TOKEN", "super-secret-token")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:password@example.invalid/db")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:password@example.invalid:8080")
    lock = tmp_path / "poetry.lock"
    lock.write_bytes(b"lock contents")
    snapshot = environment_snapshot(package_lock=lock, env_allowlist={"SAFE_MODE": "enabled"})
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "super-secret-token" not in encoded
    assert "password" not in encoded
    assert "postgres://" not in encoded
    assert snapshot["package_lock_sha256"] == hashlib.sha256(b"lock contents").hexdigest()
    assert snapshot["env"] == {"SAFE_MODE": "enabled"}
    assert isinstance(snapshot["runtime"], dict)
    assert isinstance(snapshot["python"], dict)
    assert isinstance(snapshot["platform"], dict)
    assert isinstance(snapshot["hardware"], dict)


def test_environment_snapshot_accepts_lock_bytes_and_allowlist_only():
    snapshot = environment_snapshot(package_lock=b"abc", env_allowlist={"CI": "true", "TOKEN": "do-not-include"})
    assert snapshot["package_lock_sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert snapshot["env"] == {"CI": "true"}
    assert "TOKEN" not in json.dumps(snapshot)


def test_allowlisted_values_with_embedded_credentials_are_not_exposed():
    snapshot = environment_snapshot(
        env_allowlist={
            "SAFE_URL": "https://user:password@example.invalid/x",
            "SAFE_AUTH": "Bearer very-secret-token",
            "SAFE_QUERY": "https://example.invalid/x?password=hidden&secret=also-hidden",
            "SAFE_MODE": "enabled",
        }
    )
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "password" not in encoded.lower()
    assert "very-secret-token" not in encoded
    assert "bearer" not in encoded.lower()
    assert "SAFE_URL" not in snapshot["env"]
    assert "SAFE_AUTH" not in snapshot["env"]
    assert "SAFE_QUERY" not in snapshot["env"]
    assert snapshot["env"] == {"SAFE_MODE": "enabled"}
