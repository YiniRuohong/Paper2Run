from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from p2r_artifacts.store import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStoreError,
    LocalArtifactStore,
)


def test_same_bytes_have_stable_address_and_metadata(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    first = store.put_bytes(b"hello")
    second = store.put_bytes(b"hello")
    assert first == second
    assert first.sha256 == hashlib.sha256(b"hello").hexdigest()
    assert first.bytes == 5
    assert first.path.exists()
    assert store.open_bytes(first.sha256) == b"hello"
    assert store.exists(first.sha256)


def test_artifact_is_immutable_and_detects_integrity_conflict(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    address = store.put_bytes(b"original")
    address.path.write_bytes(b"tampered")
    with pytest.raises(ArtifactIntegrityError, match="integrity"):
        store.put_bytes(b"original")
    with pytest.raises(ArtifactIntegrityError, match="integrity"):
        store.open_bytes(address.sha256)


def test_hash_address_rejects_traversal_and_unknown_files(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    for value in ("../secret", "/etc/passwd", "a\x00b", "not-a-hash", "A" * 64):
        with pytest.raises(ArtifactStoreError):
            store.exists(value)
        with pytest.raises(ArtifactStoreError):
            store.open_bytes(value)
    with pytest.raises(ArtifactNotFoundError):
        store.open_bytes("0" * 64)


def test_size_limit_is_enforced(tmp_path: Path):
    store = LocalArtifactStore(tmp_path / "artifacts", max_bytes=3)
    with pytest.raises(ValueError, match="size"):
        store.put_bytes(b"1234")
    assert list((tmp_path / "artifacts").rglob("*")) == []
