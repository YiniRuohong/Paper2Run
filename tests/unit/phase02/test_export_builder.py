import hashlib
import json
import zipfile

import pytest

from p2r_orchestration.export_builder import build_bundle, build_zip, canonical_json, render_markdown


def report():
    return {"run_id":"run_1", "revision_id":"spr_1", "metrics":{"accuracy":1}, "metrics_source":"fixture", "limitations":["one","two","three"], "manifest":{"status":"provisional"}}


def test_canonical_json_roundtrip_and_markdown_escape():
    value = {"z": 1, "a": "中文"}
    assert json.loads(canonical_json(value)) == value
    md = render_markdown({**report(), "metrics":{"a|b":"line\nvalue"}}).decode()
    assert "metrics source" in md.lower() and "one" in md and "a\\|b" in md
    assert "<table" not in md and "<br" not in md


def test_zip_is_deterministic_and_metadata_fixed():
    a = build_zip([("b.txt", b"b"), ("a.txt", b"a")])
    b = build_zip([("a.txt", b"a"), ("b.txt", b"b")])
    assert a == b
    with zipfile.ZipFile(__import__("io").BytesIO(a)) as z:
        assert z.namelist() == ["a.txt", "b.txt"]
        assert all(i.date_time == (1980,1,1,0,0,0) and i.external_attr == 0o100644 << 16 for i in z.infolist())


@pytest.mark.parametrize("name", ["/abs", "../x", "a/../x", "a\\x", "a:b", "", "a/./x"])
def test_zip_rejects_unsafe_paths(name):
    with pytest.raises(ValueError): build_zip([(name, b"x")])


def test_zip_rejects_duplicate_and_non_bytes():
    with pytest.raises(ValueError): build_zip([("a", b"x"), ("a", b"y")])
    with pytest.raises(ValueError): build_zip([("a", "x")])


def test_bundle_checks_evidence_hash_and_checksums():
    evidence = b"registered"
    digest = hashlib.sha256(evidence).hexdigest()
    blob = build_bundle(report(), evidence=[(f"evidence/{digest}.txt", evidence)])
    with zipfile.ZipFile(__import__("io").BytesIO(blob)) as z:
        names = z.namelist()
        assert names == sorted(names)
        checksums = json.loads(z.read("checksums.json"))
        assert "checksums.json" not in checksums
        assert checksums[f"evidence/{digest}.txt"] == digest
    with pytest.raises(ValueError): build_bundle(report(), evidence=[(f"evidence/{digest}.txt", b"bad")])
    with pytest.raises(ValueError): build_bundle({**report(), "limitations": []})
