"""Windows no-follow reads, exercised on POSIX by forcing the Windows branch.

Windows has neither O_NOFOLLOW nor dir_fd. The Windows branch must refuse any
symlink or reparse point (junction, mount point) on the whole path, refuse a
file swapped between the check and the open, and refuse a final path that
resolves outside the trusted root. The POSIX branch is unchanged and keeps
its own tests in test_evaluation.py.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import p2r_evaluation.autonomous as evaluation
from p2r_contracts.autonomous import freeze_protocol
from p2r_evaluation import safe_read
from p2r_orchestration.autonomous import files
from p2r_orchestration.errors import ServiceError

from .test_evaluation import artifacts, protocol


def reparse_at(monkeypatch, *targets):
    """Report FILE_ATTRIBUTE_REPARSE_POINT for the given paths (a junction on Windows)."""
    marked = {os.fspath(t) for t in targets}
    real_lstat = os.lstat

    def lstat(value, *args, **kwargs):
        info = real_lstat(value, *args, **kwargs)
        if os.fspath(value) in marked:
            return SimpleNamespace(st_mode=info.st_mode, st_ino=info.st_ino, st_dev=info.st_dev,
                                   st_size=info.st_size, st_mtime=info.st_mtime, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(os, "lstat", lstat)


def swap_before_open(monkeypatch, path, tmp_path, content):
    """Replace ``path`` with a different file right before it is opened."""
    real_open = os.open

    def swapping_open(value, flags, *args, **kwargs):
        if os.fspath(value) == os.fspath(path):
            replacement = tmp_path / "replacement.tmp"
            replacement.write_text(content)
            os.replace(replacement, path)
        return real_open(value, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping_open)


def posix_final_path_of_fd(fd):
    """POSIX stand-in for GetFinalPathNameByHandleW: the path the kernel holds for this open fd."""
    import sys
    if sys.platform == "darwin":
        import fcntl
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).split(b"\0", 1)[0].decode()
    return os.readlink(f"/proc/self/fd/{fd}")


@pytest.fixture(autouse=True)
def final_path_seams(monkeypatch):
    # The real seams call kernel32 through ctypes; on POSIX they are replaced by
    # handle-bound equivalents so the forced Windows branch runs end to end.
    monkeypatch.setattr(safe_read, "_final_path_of_fd", posix_final_path_of_fd)
    monkeypatch.setattr(safe_read, "_final_path_of_dir", lambda path: os.path.realpath(path))


@pytest.fixture
def windows_evaluation(monkeypatch):
    monkeypatch.setattr(evaluation, "_read_mode", lambda: "windows")


def frozen():
    return freeze_protocol(protocol())


# ---- p2r_evaluation: the Windows branch gives the same answers -------------

def test_windows_branch_matches_posix_result(tmp_path, monkeypatch):
    artifacts(tmp_path)
    posix = evaluation.evaluate_artifacts(frozen(), tmp_path)
    monkeypatch.setattr(evaluation, "_read_mode", lambda: "windows")
    assert evaluation.evaluate_artifacts(frozen(), tmp_path) == posix


def test_windows_missing_artifact_is_a_reason_not_a_crash(tmp_path, windows_evaluation):
    (tmp_path / "predictions.json").write_text("[]")
    result = evaluation.evaluate_artifacts(frozen(), tmp_path)
    assert "missing evaluation artifact: labels.json" in result["reasons"]


def test_windows_refuses_symlinked_artifact(tmp_path, windows_evaluation):
    artifacts(tmp_path)
    real = tmp_path / "real-labels.json"
    os.replace(tmp_path / "labels.json", real)
    (tmp_path / "labels.json").symlink_to(real)
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)


def test_windows_refuses_symlinked_root_component(tmp_path, windows_evaluation):
    real = tmp_path / "real"
    real.mkdir()
    artifacts(real)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.evaluate_artifacts(frozen(), tmp_path / "link")


def test_windows_refuses_reparse_point_component(tmp_path, monkeypatch, windows_evaluation):
    root = tmp_path / "artifacts"
    root.mkdir()
    artifacts(root)
    reparse_at(monkeypatch, root)
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.evaluate_artifacts(frozen(), root)


def test_windows_refuses_reparse_point_file(tmp_path, monkeypatch, windows_evaluation):
    artifacts(tmp_path)
    reparse_at(monkeypatch, tmp_path / "predictions.json")
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)


def test_windows_refuses_file_swapped_after_check(tmp_path, monkeypatch, windows_evaluation):
    artifacts(tmp_path)
    swap_before_open(monkeypatch, tmp_path / "labels.json", tmp_path,
                     json.dumps([{"id": "0", "label": 0}, {"id": "1", "label": 0}]))
    with pytest.raises(ValueError, match="changed"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)


def test_windows_refuses_handle_final_path_outside_root(tmp_path, monkeypatch, windows_evaluation):
    # The identity check passes (same file was opened), but the open handle's
    # final path is outside the root: containment is bound to the handle.
    artifacts(tmp_path)
    outside = str(tmp_path.parent / "elsewhere" / "labels.json")
    seen = []

    def final(fd):
        path = posix_final_path_of_fd(fd)
        seen.append(path)
        return outside if path.endswith("labels.json") else path
    monkeypatch.setattr(safe_read, "_final_path_of_fd", final)
    # A name-based realpath check alone would pass here.
    assert os.path.realpath(tmp_path / "labels.json").startswith(os.path.realpath(tmp_path))
    with pytest.raises(ValueError, match="outside"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)
    assert any(path.endswith("labels.json") for path in seen)


def test_windows_final_path_failure_fails_closed(tmp_path, monkeypatch, windows_evaluation):
    artifacts(tmp_path)

    def broken(fd):
        raise OSError(6, "GetFinalPathNameByHandleW failed")
    monkeypatch.setattr(safe_read, "_final_path_of_fd", broken)
    with pytest.raises(ValueError, match="unsafe"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)


def test_windows_root_final_path_is_resolved_once_per_open(tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text("{}")
    calls = []
    monkeypatch.setattr(safe_read, "_final_path_of_dir", lambda path: calls.append(path) or os.path.realpath(path))
    fd = safe_read.open_below_root_windows(tmp_path, ["a.json"])
    os.close(fd)
    assert calls == [tmp_path]


@pytest.mark.parametrize("raw,expected", [
    ("\\\\?\\C:\\Users\\me\\p2r\\a.json", "C:\\Users\\me\\p2r\\a.json"),
    ("\\\\?\\UNC\\server\\share\\a.json", "\\\\server\\share\\a.json"),
    ("C:\\plain\\a.json", "C:\\plain\\a.json"),
])
def test_extended_length_prefix_is_removed(raw, expected):
    assert safe_read.strip_extended_prefix(raw) == expected


def test_windows_refuses_relative_or_traversing_root(windows_evaluation):
    with pytest.raises(ValueError, match="unsafe artifact root"):
        evaluation.evaluate_artifacts(frozen(), evaluation.Path("relative"))


def test_platform_without_either_mechanism_fails_closed(tmp_path, monkeypatch):
    artifacts(tmp_path)
    monkeypatch.setattr(evaluation, "_read_mode", lambda: None)
    with pytest.raises(ValueError, match="safe artifact reads require"):
        evaluation.evaluate_artifacts(frozen(), tmp_path)


def test_read_mode_prefers_posix_where_available():
    # Never trade the dir_fd/O_NOFOLLOW path for the Windows checks on POSIX.
    if hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
        assert evaluation._read_mode() == "posix"


def test_open_below_root_rejects_unsafe_names(tmp_path):
    for parts in ([], [""], [".."], ["a/b"], ["a\\b"], ["."]):
        with pytest.raises(ValueError):
            safe_read.open_below_root_windows(tmp_path, parts)


# ---- skills registry: a junction is refused like a symlink --------------------

def test_registry_refuses_junctioned_roots(tmp_path, monkeypatch):
    from p2r_orchestration.research_capabilities import CapabilityRegistry
    bundled, imported = tmp_path / "bundled", tmp_path / "imported"
    bundled.mkdir()
    imported.mkdir()
    (bundled / "skill").mkdir()
    registry = CapabilityRegistry(bundled, imported)
    reparse_at(monkeypatch, imported)
    with pytest.raises(ValueError, match="unsafe imported skills root"):
        registry.list()
    monkeypatch.undo()
    reparse_at(monkeypatch, bundled / "skill")
    with pytest.raises(ValueError, match="symlinks are forbidden"):
        registry.list()


# ---- p2r_orchestration files: workbench raw reads on Windows ----------------

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "_windows_reads", lambda: True)
    root = tmp_path / "workspace" / "task"
    (root / "out").mkdir(parents=True)
    (root / "out" / "result.txt").write_text("observed")
    return {"task_root": tmp_path / "control", "artifacts": [], "workspace": root}


def test_windows_workspace_read(workspace):
    raw, media, name = files.read_file(workspace, "workspace", "workspace/out/result.txt")
    assert (raw, media, name) == (b"observed", "text/plain", "result.txt")


def test_windows_workspace_junction_is_not_found(workspace, monkeypatch):
    reparse_at(monkeypatch, workspace["workspace"] / "out")
    with pytest.raises(ServiceError) as error:
        files.read_file(workspace, "workspace", "workspace/out/result.txt")
    assert error.value.status == 404


def test_windows_workspace_swapped_file_is_not_found(workspace, monkeypatch, tmp_path):
    swap_before_open(monkeypatch, workspace["workspace"] / "out" / "result.txt", tmp_path, "swapped")
    with pytest.raises(ServiceError) as error:
        files.read_file(workspace, "workspace", "workspace/out/result.txt")
    assert error.value.status == 404


def test_windows_listing_skips_reparse_points(workspace, monkeypatch):
    (workspace["workspace"] / "junction").mkdir()
    (workspace["workspace"] / "junction" / "hidden.txt").write_text("x")
    (workspace["workspace"] / "pointer.txt").write_text("x")
    reparse_at(monkeypatch, workspace["workspace"] / "junction", workspace["workspace"] / "pointer.txt")
    paths = [item["path"] for item in files.list_files(workspace)["items"]]
    assert paths == ["workspace/out/result.txt"]


def test_windows_artifact_reparse_point_is_integrity_error(tmp_path, monkeypatch):
    import hashlib
    monkeypatch.setattr(files, "_windows_reads", lambda: True)
    sha = "a" * 64
    item = {"name": "result.json", "sha256": hashlib.sha256(b"{}").hexdigest(), "protocol_sha256": sha, "kind": "result"}
    snap = {"task_root": tmp_path, "artifacts": [item], "workspace": None}
    disk = tmp_path / "artifacts" / sha / "result.json"
    disk.parent.mkdir(parents=True)
    disk.write_bytes(b"{}")
    path = "artifacts/" + sha[:12] + "/result.json"
    assert files.read_file(snap, "artifact", path)[0] == b"{}"
    reparse_at(monkeypatch, disk)
    with pytest.raises(ServiceError) as error:
        files.read_file(snap, "artifact", path)
    assert error.value.code == "EVIDENCE_INTEGRITY"
