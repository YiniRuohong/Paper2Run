"""mineru-kit `-f zip` output (real 4.1.3 layout) -> per-page outcomes."""
from __future__ import annotations

import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

from p2r_docparse import offline
from p2r_docparse.errors import DocParseError
from p2r_docparse.offline import parse_offline
from p2r_docparse.proc import mineru_kit
from p2r_docparse.testing import minimal_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "structured_content_darwin_arm64_4.1.3.json"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake kit")

_TINY = json.dumps({"pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": "hi"}]}]},
                   separators=(",", ":"))


def _stage_zip(tmp_path: Path, members: dict) -> Path:
    staged = tmp_path / "staged.zip"
    with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return staged


def _kit_writing(root: Path, staged: Path, argv_file: Path | None = None) -> None:
    kit = mineru_kit(root)
    kit.parent.mkdir(parents=True, exist_ok=True)
    script = kit.parent / "mineru_kit_impl.py"
    lines = [
        "import sys, json, shutil, pathlib",
        "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1]); out.mkdir(parents=True, exist_ok=True)",
    ]
    if argv_file is not None:
        lines.append(f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv))")
    # the real mineru-kit writes <output>/<input stem>.zip
    lines.append(f"shutil.copy({str(staged)!r}, out / (pathlib.Path(sys.argv[2]).stem + '.zip'))")
    script.write_text("\n".join(lines) + "\n")
    kit.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    kit.chmod(kit.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: Path, members: dict, pages: int = 2, argv_file: Path | None = None):
    root = tmp_path / "root"
    _kit_writing(root, _stage_zip(tmp_path, members), argv_file)
    return parse_offline(minimal_pdf(["p"] * pages), root=root, tier="standard", version="4.1.3")


def _structured(pages: list) -> str:
    return json.dumps({"pages": pages, "metadata": {}, "is_full_document": True})


@posix_only
def test_argv_requests_zip_format(tmp_path):
    argv_file = tmp_path / "argv.json"
    _run(tmp_path, {"structured_content.json": _TINY}, argv_file=argv_file)
    argv = json.loads(argv_file.read_text())
    assert argv[argv.index("-f") + 1] == "zip"
    assert argv[argv.index("--tier") + 1] == "standard"
    assert "--remote" not in argv


@posix_only
def test_real_structured_content_fixture(tmp_path):
    members = {
        "structured_content.json": FIXTURE.read_text(encoding="utf-8"),
        "markdown.md": "WRONG-SOURCE",
        "middle_json.json": "{}",
        "images/page_2_image_1.jpg": bytes([255, 216]) + b"fake",
    }
    result = _run(tmp_path, members, pages=6)
    md = result.markdown
    assert md.startswith("<!-- p2r:page 1 -->\n# AN IMAGE IS WORTH 16X16 WORDS")
    for page in (1, 3, 5, 6):
        assert f"<!-- p2r:page {page} -->" in md
    for page in (2, 4):
        assert f"<!-- p2r:page {page} -->" not in md
    assert "## 3 METHOD" in md
    assert "Figure 1: Model overview." in md
    assert "| ViT-Base | 12 | 768 | 3072 | 12 | 86M |" in md
    assert "Table 1: Details of Vision Transformer model variants." in md
    assert "## 4.2 COMPARISON TO STATE OF THE ART" in md
    assert "Published as a conference paper" not in md
    assert "arXiv:2010.11929v2" not in md
    assert "images/" not in md and "![" not in md
    assert "WRONG-SOURCE" not in md
    assert result.backend == "offline_mineru"
    assert result.page_count == 6 and result.pages_parsed == 6


@posix_only
def test_titles_equations_tables_and_furniture(tmp_path):
    pages = [
        {"page_idx": 0, "blocks": [
            {"type": "doc_title", "level": 1, "content": "Title"},
            {"type": "paragraph_title", "level": 3, "content": "1.1 Sub"},
            {"type": "equation", "content": "E = mc^2", "image_source": "images/e.jpg"},
            {"type": "footer", "content": "FOOT"},
            {"type": "table", "content": "| a | b |\n| --- | --- |\n| 1 | 2 |",
             "captions": [{"content": "Table 9: caption"}], "footnotes": [{"content": "note"}],
             "image_source": "images/t.jpg"},
            {"type": "chart", "content": "", "captions": [], "footnotes": [],
             "image_source": "images/c.jpg"},
        ]},
        {"page_idx": 1, "blocks": [
            {"type": "header", "content": "H"},
            {"type": "page_number", "content": "2"},
            {"type": "aside_text", "content": "SIDE"},
        ]},
        {"page_idx": 99, "blocks": [{"type": "text", "content": "outside"}]},
    ]
    result = _run(tmp_path, {"structured_content.json": _structured(pages)}, pages=2)
    assert result.markdown == (
        "<!-- p2r:page 1 -->\n# Title\n\n### 1.1 Sub\n\n$$\nE = mc^2\n$$\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\nTable 9: caption\n\nnote"
    )
    assert result.page_count == 2 and result.pages_parsed == 2


@posix_only
def test_markdown_fallback_without_structured_content(tmp_path):
    md = "# T\n\n![](images/page_2_image_1.jpg)\n\nbody text"
    result = _run(tmp_path, {"markdown.md": md, "middle_json.json": "{}"}, pages=3)
    assert result.markdown.startswith("<!-- p2r:pages 1-3 -->\n# T")
    assert "body text" in result.markdown
    assert "![" not in result.markdown and "images/" not in result.markdown


@posix_only
def test_zip_without_usable_members_fails(tmp_path):
    with pytest.raises(DocParseError) as e:
        _run(tmp_path, {"middle_json.json": "{}", "images/a.jpg": b"x"})
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
@pytest.mark.parametrize("name", [
    "../evil.txt", "/abs/evil.txt", "images/../../evil.txt", "C:\\evil.txt", "a\\..\\evil.txt",
])
def test_unsafe_member_name_rejected(tmp_path, name):
    members = {"structured_content.json": _TINY, name: "x"}
    with pytest.raises(DocParseError) as e:
        _run(tmp_path, members)
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
def test_oversized_member_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "_MAX_MEMBER_BYTES", 100)
    members = {"structured_content.json": _TINY, "images/big.jpg": b"x" * 500}
    with pytest.raises(DocParseError) as e:
        _run(tmp_path, members)
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
def test_oversized_structured_content_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(offline, "_MAX_MEMBER_BYTES", 100)
    big = _structured([{"page_idx": 0, "blocks": [{"type": "text", "content": "y" * 500}]}])
    with pytest.raises(DocParseError) as e:
        _run(tmp_path, {"structured_content.json": big})
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
def test_corrupt_zip_fails(tmp_path):
    root = tmp_path / "root"
    staged = tmp_path / "staged.zip"
    staged.write_bytes(b"this is not a zip file")
    _kit_writing(root, staged)
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["p"]), root=root, tier="standard", version="4.1.3")
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"
