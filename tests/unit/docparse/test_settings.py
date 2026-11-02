from __future__ import annotations

import pytest

from p2r_docparse.settings import DocParseSettings


def test_settings_roundtrip(tmp_path):
    settings = DocParseSettings(tmp_path / "docparse")
    assert settings.mode() is None
    settings.set_mode("online")
    assert DocParseSettings(tmp_path / "docparse").mode() == "online"
    settings.set_mode("offline")
    assert DocParseSettings(tmp_path / "docparse").mode() == "offline"


def test_invalid_mode_rejected(tmp_path):
    settings = DocParseSettings(tmp_path)
    with pytest.raises(ValueError):
        settings.set_mode("hybrid")  # type: ignore[arg-type]
    assert settings.mode() is None


def test_corrupt_file_reads_as_unset(tmp_path):
    (tmp_path / "settings.json").write_text("{not json", encoding="utf-8")
    assert DocParseSettings(tmp_path).mode() is None
    (tmp_path / "settings.json").write_text('{"mode": "weird"}', encoding="utf-8")
    assert DocParseSettings(tmp_path).mode() is None


def test_write_leaves_no_temp_files(tmp_path):
    DocParseSettings(tmp_path).set_mode("online")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["settings.json"]
