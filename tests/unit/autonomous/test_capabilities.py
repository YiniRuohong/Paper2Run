import json
from pathlib import Path

import pytest

from p2r_orchestration.research_capabilities import CapabilityRegistry

BUNDLED = Path(__file__).resolve().parents[3] / "resources" / "research-skills"


def package(root, version="1.0.0", content="# test\nRead evidence."):
    root.mkdir(exist_ok=True)
    (root / "SKILL.md").write_text(content)
    (root / "manifest.json").write_text(json.dumps({"id": "custom", "version": version, "name": "Custom", "description": "Test skill", "entrypoint": "SKILL.md", "capabilities": ["audit"], "compatibility": ["paper2run.skills.v1"]}))
    return root


def test_bundled_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("P2R_SKILLS_DIR", str(BUNDLED))
    registry = CapabilityRegistry(imported_root=tmp_path / "imports")
    assert len(registry.list()) == 8
    assert len(registry.snapshot(["paper-analysis"])[0]["sha256"]) == 64


def test_import_update_snapshot_disable(tmp_path):
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")
    source = package(tmp_path / "source")
    imported = registry.import_local(source)
    snapshot = registry.snapshot(["custom"])
    assert snapshot[0]["content"] == "# test\nRead evidence."
    assert "SKILL.md" in snapshot[0]["files"]
    assert registry.import_local(source)["sha256"] == imported["sha256"]
    package(source, content="changed")
    with pytest.raises(ValueError, match="same skill version"):
        registry.import_local(source)
    package(source, "1.1.0", "new")
    registry.import_local(source)
    package(source, "1.10.0", "newest")
    registry.import_local(source)
    assert registry.snapshot(["custom"])[0]["content"] == "newest"
    assert snapshot[0]["content"] == "# test\nRead evidence."
    registry.set_enabled("custom", False)
    with pytest.raises(ValueError, match="disabled"):
        registry.snapshot(["custom"])
    assert next(item for item in CapabilityRegistry(BUNDLED, tmp_path / "imports").list() if item["id"] == "custom")["enabled"] is False
    registry.set_enabled("custom", True)
    assert registry.snapshot(["custom"])
    with pytest.raises(ValueError, match="duplicate"):
        registry.snapshot(["custom", "custom"])


@pytest.mark.parametrize("field,value", [("entrypoint", "../script.py"), ("compatibility", ["future"]), ("version", "latest"), ("content_sha256", "a" * 64), ("id", "../escape"), ("command", "id")])
def test_bad_manifest(tmp_path, field, value):
    source = package(tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest[field] = value
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        CapabilityRegistry(BUNDLED, tmp_path / "imports").import_local(source)


def test_symlinks_and_size(tmp_path):
    source = package(tmp_path / "source")
    (source / "link").symlink_to(source / "SKILL.md")
    with pytest.raises(ValueError, match="symlinks"):
        CapabilityRegistry(BUNDLED, tmp_path / "imports").import_local(source)


def test_size_and_installed_tamper(tmp_path):
    source = package(tmp_path / "source")
    (source / "large").write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")
    with pytest.raises(ValueError, match="size"):
        registry.import_local(source)
    clean = package(tmp_path / "clean")
    registry.import_local(clean)
    (tmp_path / "imports" / "custom--1.0.0" / "SKILL.md").write_text("tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        registry.snapshot(["custom"])


def test_interrupted_import_not_visible(tmp_path, monkeypatch):
    source = package(tmp_path / "source")
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")
    original_write = Path.write_bytes

    def failing_write(path, raw):
        if any(part.startswith(".staging-") for part in path.parts):
            raise OSError("injected write interruption")
        return original_write(path, raw)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "write_bytes", failing_write)
        with pytest.raises(OSError, match="interruption"):
            registry.import_local(source)
    assert len(registry.list()) == 8
    assert not list((tmp_path / "imports").glob(".staging-*"))
    assert registry.import_local(source)["id"] == "custom"


def test_interrupted_publish_and_abandoned_staging(tmp_path, monkeypatch):
    import p2r_orchestration.research_capabilities as capabilities
    source = package(tmp_path / "source")
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")

    def fail(*args):
        raise OSError("injected rename interruption")

    with monkeypatch.context() as patch:
        patch.setattr(capabilities.os, "rename", fail)
        with pytest.raises(OSError, match="interruption"):
            registry.import_local(source)
    assert not (tmp_path / "imports" / "custom--1.0.0").exists()
    assert not list((tmp_path / "imports").glob(".staging-*"))
    # Simulate a process killed before TemporaryDirectory's cleanup executes.
    abandoned = tmp_path / "imports" / ".staging-abandoned"
    abandoned.mkdir()
    (abandoned / "partial").write_text("not a valid package")
    assert len(registry.list()) == 8
    assert registry.import_local(source)["id"] == "custom"


def test_interrupted_state_replace_preserves_previous(tmp_path, monkeypatch):
    import p2r_orchestration.research_capabilities as capabilities
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")
    registry.set_enabled("paper-analysis", False)
    before = (tmp_path / "imports" / "enabled.json").read_bytes()

    def fail(*args):
        raise OSError("injected publish interruption")

    with monkeypatch.context() as patch:
        patch.setattr(capabilities.os, "replace", fail)
        with pytest.raises(OSError, match="interruption"):
            registry.set_enabled("result-audit", False)
    assert (tmp_path / "imports" / "enabled.json").read_bytes() == before
    assert not list((tmp_path / "imports").glob(".state-*"))
    assert len(registry.list()) == 8


def _disable_in_process(bundled, imports, skill_id):
    CapabilityRegistry(Path(bundled), Path(imports)).set_enabled(skill_id, False)


def test_concurrent_instances_and_processes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import multiprocessing

    imports = tmp_path / "imports"
    source = package(tmp_path / "source")
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: CapabilityRegistry(BUNDLED, imports).import_local(source), range(4)))
    assert len({item["sha256"] for item in results}) == 1
    ids = ["paper-analysis", "result-audit", "data-preparation", "research-report"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda key: CapabilityRegistry(BUNDLED, imports).set_enabled(key, False), ids[:2]))
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_disable_in_process, args=(str(BUNDLED), str(imports), key)) for key in ids[2:]]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0
    state = json.loads((imports / "enabled.json").read_text())
    assert all(state[key] is False for key in ids)
    assert len(CapabilityRegistry(BUNDLED, imports).list()) == 9


def test_import_does_not_execute(tmp_path):
    source = package(tmp_path / "source")
    (source / "setup.py").write_text("raise RuntimeError('must never run')")
    registry = CapabilityRegistry(BUNDLED, tmp_path / "imports")
    registry.import_local(source)
    assert "setup.py" in registry.snapshot(["custom"])[0]["files"]
