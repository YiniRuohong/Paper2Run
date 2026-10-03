import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[3] / "packages" / "codegen"))

from p2r_codegen import (  # noqa: E402
    CodegenError,
    Patch,
    PatchOperation,
    SecretLeakError,
    apply_patch,
    audit_payload,
    build_candidate,
    patch_hash,
    propose_repair,
    RepairBudget,
    next_repair_budget,
    repair_gate,
)


ALLOW = {"src/main.py", "src/util.py", "README.md"}


def candidate(**files):
    return build_candidate(files, allowlist=ALLOW, max_file_size=100, max_total_size=300)


def test_candidate_enforces_allowlist_paths_and_sizes():
    with pytest.raises(CodegenError):
        candidate(**{"src/other.py": "x"})
    with pytest.raises(CodegenError):
        candidate(**{"../src/main.py": "x"})
    with pytest.raises(CodegenError):
        build_candidate({"src/main.py": "x" * 101}, allowlist=ALLOW, max_file_size=100, max_total_size=300)
    with pytest.raises(CodegenError):
        build_candidate({"src/main.py": "x" * 100, "src/util.py": "y" * 100, "README.md": "z" * 101}, allowlist=ALLOW, max_file_size=200, max_total_size=250)


def test_manifest_and_hash_are_canonical_and_declared_hash_is_anchored():
    a = candidate(**{"src/main.py": "print(1)", "src/util.py": "x"})
    b = candidate(**{"src/util.py": "x", "src/main.py": "print(1)"})
    assert a.manifest == b.manifest
    assert a.base_hash == b.base_hash
    with pytest.raises(CodegenError):
        build_candidate({"src/main.py": "print(1)"}, allowlist=ALLOW, declared_hash="bad")


def test_structured_patch_supports_add_replace_delete_and_base_anchor():
    base = candidate(**{"src/main.py": "old", "src/util.py": "keep"})
    patch = Patch(
        base_hash=base.base_hash,
        operations=(
            PatchOperation("replace", "src/main.py", content="new", expected_hash=base.manifest["src/main.py"]["sha256"]),
            PatchOperation("delete", "src/util.py", expected_hash=base.manifest["src/util.py"]["sha256"]),
            PatchOperation("add", "README.md", content="docs"),
        ),
    )
    result = apply_patch(base, patch)
    assert result.files == {"src/main.py": "new", "README.md": "docs"}
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base_hash="wrong", operations=()))


def test_repair_ordinal_repeat_error_budget_and_needs_revision_stop():
    base = candidate(**{"src/main.py": "x"})
    assert repair_gate(ordinal=1, error_code="E1", base_hash=base.base_hash, spent=0, budget=2).status == "allowed"
    assert repair_gate(ordinal=5, error_code="E1", base_hash=base.base_hash, spent=0, budget=2).status == "allowed"
    for kwargs in (
        {"ordinal": 0, "error_code": "E1", "base_hash": base.base_hash, "spent": 0, "budget": 2},
        {"ordinal": 6, "error_code": "E1", "base_hash": base.base_hash, "spent": 0, "budget": 2},
        {"ordinal": 1, "error_code": "E1", "base_hash": base.base_hash, "spent": 2, "budget": 2},
        {"ordinal": 1, "error_code": "E1", "base_hash": base.base_hash, "spent": 0, "budget": 2, "needs_revision": True},
    ):
        assert repair_gate(**kwargs).status in {"stopped", "needs_revision"}
    assert repair_gate(ordinal=1, error_code="E1", base_hash=base.base_hash, spent=0, budget=2, previous_errors={("E1", base.base_hash)}).status == "stopped"


def test_audit_payload_discloses_source_and_rejects_secrets():
    payload = audit_payload(source="repair", base_hash="a" * 64, patch_hash="b" * 64, validation={"ok": True})
    assert payload["source"] == "repair"
    assert payload["base_hash"] == "a" * 64
    with pytest.raises(SecretLeakError):
        audit_payload(source="model", error_code="sk-abcdefghijklmnop", validation={"ok": False})
    with pytest.raises(SecretLeakError):
        audit_payload(source="template", validation={"ok": False, "static_issues": ["bearer abcdefghijklmnop"]})


@pytest.mark.parametrize("path", ["src//x", "./x", "src/main.py/", "src/\x00x", "src/\nX", "C:/src/main.py"])
def test_rejects_noncanonical_paths_even_when_allowlisted(path):
    with pytest.raises(CodegenError):
        build_candidate({path: "x"}, allowlist=ALLOW)


def test_rejects_file_directory_prefix_conflicts():
    with pytest.raises(CodegenError):
        build_candidate({"src": "file", "src/main.py": "x"}, allowlist={"src", "src/main.py"})


def test_apply_patch_revalidates_mutable_candidate_and_cannot_relax_limits():
    base = candidate(**{"src/main.py": "x"})
    base.files["src/main.py"] = "tampered"
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, ()))
    base = candidate(**{"src/main.py": "x"})
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, ()), allowlist={"src/main.py", "README.md"}, max_file_size=999, max_total_size=999)


def test_patch_requires_expected_hash_rejects_duplicates_empty_and_hashes_canonically():
    base = candidate(**{"src/main.py": "x"})
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, ()))
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, (PatchOperation("replace", "src/main.py", content="y"),)))
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, (PatchOperation("delete", "src/main.py"),)))
    op = PatchOperation("replace", "src/main.py", content="y", expected_hash=base.manifest["src/main.py"]["sha256"])
    with pytest.raises(CodegenError):
        apply_patch(base, Patch(base.base_hash, (op, op)))
    assert patch_hash(Patch(base.base_hash, (op,))) == patch_hash(Patch(base.base_hash, (op,)))


@pytest.mark.parametrize("field", ["ordinal", "spent", "budget"])
def test_repair_gate_rejects_bool_float_and_negative(field):
    kwargs = {"ordinal": 1, "error_code": "E1", "base_hash": "a" * 64, "spent": 0, "budget": 2}
    for value in [True, 1.0, -1]:
        kwargs[field] = value
        with pytest.raises(CodegenError):
            repair_gate(**kwargs)


def test_repair_gate_requires_requested_cost_and_budget_remaining():
    kwargs = {"ordinal": 1, "error_code": "E1", "base_hash": "a" * 64, "spent": 1, "budget": 2}
    assert repair_gate(**kwargs, requested_cost=1).status == "allowed"
    assert repair_gate(**kwargs, requested_cost=2).status == "stopped"
    with pytest.raises(CodegenError):
        repair_gate(**kwargs, requested_cost=0)


def test_audit_payload_is_closed_and_hashes_are_strict():
    with pytest.raises(CodegenError):
        audit_payload(source="repair", base_hash="a" * 64, patch_hash="b" * 64, validation={"ok": True, "static_issues": "free text"})
    with pytest.raises(CodegenError):
        audit_payload(source="repair", base_hash="bad", patch_hash="b" * 64, validation={"ok": True})
    with pytest.raises((TypeError, CodegenError)):
        audit_payload(source="model", made_up=True)
    with pytest.raises(CodegenError):
        audit_payload(source="repair", base_hash="a" * 64, validation={"ok": True})
