"""docparse_root() keeps the MinerU runtime on an ASCII path on Windows."""
import sys

from p2r_api.dependencies import docparse_root


def _windows(monkeypatch, tmp_path, demo_dir):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("P2R_DOCPARSE_DIR", raising=False)
    monkeypatch.setenv("P2R_DEMO_DIR", str(demo_dir))
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path / "pd"))


def test_non_ascii_data_dir_moves_docparse_to_programdata(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, tmp_path / "天猫AI" / "data")
    assert docparse_root() == (tmp_path / "pd" / "Paper2Run" / "docparse").resolve()


def test_ascii_data_dir_is_unchanged(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, tmp_path / "data")
    assert docparse_root() == (tmp_path / "data" / "docparse").resolve()


def test_explicit_docparse_dir_wins(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, tmp_path / "天猫AI" / "data")
    monkeypatch.setenv("P2R_DOCPARSE_DIR", str(tmp_path / "天猫" / "dp"))
    assert docparse_root() == (tmp_path / "天猫" / "dp").resolve()


def test_other_platforms_keep_the_data_dir(monkeypatch, tmp_path):
    _windows(monkeypatch, tmp_path, tmp_path / "天猫AI" / "data")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert docparse_root() == (tmp_path / "天猫AI" / "data" / "docparse").resolve()
