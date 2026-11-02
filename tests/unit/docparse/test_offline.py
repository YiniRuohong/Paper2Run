from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from p2r_docparse import offline, proc
from p2r_docparse.errors import DocParseError
from p2r_docparse.offline import install_env, mineru_kit, offline_env, parse_offline
from p2r_docparse.testing import minimal_pdf


def _fake_kit(root: Path, body: str) -> Path:
    # sh wrapper instead of a direct shebang: sys.executable may contain spaces,
    # which a shebang line cannot express.
    kit = root / "venv" / "bin" / "mineru-kit"
    kit.parent.mkdir(parents=True, exist_ok=True)
    script = kit.parent / "mineru_kit_impl.py"
    script.write_text(f"import sys, json, pathlib\n{body}\n")
    kit.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n')
    kit.chmod(kit.stat().st_mode | stat.S_IXUSR)
    return kit


# argv: [kit, "parse", in.pdf, "-o", out_dir, "--tier", tier]
_OUT = "out = pathlib.Path(sys.argv[sys.argv.index('-o') + 1]); out.mkdir(parents=True, exist_ok=True)\n"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX fake kit")


def test_env_whitelist(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid")
    env = offline_env(tmp_path)
    assert "MINERU_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "PIP_INDEX_URL" not in env
    assert "HTTPS_PROXY" not in env
    assert env["MINERU_TELEMETRY_DISABLED"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["MINERU_HOME"] == str(tmp_path / "mineru-home")
    assert env["PATH"].split(os.pathsep)[0] == str(mineru_kit(tmp_path).parent)


def test_install_env_allows_network_and_pip(monkeypatch, tmp_path):
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")
    monkeypatch.setenv("https_proxy", "http://proxy.invalid")
    monkeypatch.setenv("MINERU_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    env = install_env(tmp_path)
    assert env["PIP_INDEX_URL"] == "https://example.invalid/simple"
    assert env["https_proxy"] == "http://proxy.invalid"
    assert "HF_HUB_OFFLINE" not in env
    assert "TRANSFORMERS_OFFLINE" not in env
    assert "MINERU_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["MINERU_TELEMETRY_DISABLED"] == "1"


def test_windows_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(proc.sys, "platform", "win32")
    assert mineru_kit(tmp_path) == tmp_path / "venv" / "Scripts" / "mineru-kit.exe"
    assert proc.venv_python(tmp_path) == tmp_path / "venv" / "Scripts" / "python.exe"


def test_posix_paths(tmp_path):
    if sys.platform == "win32":
        pytest.skip("POSIX paths")
    assert mineru_kit(tmp_path) == tmp_path / "venv" / "bin" / "mineru-kit"
    assert proc.venv_python(tmp_path) == tmp_path / "venv" / "bin" / "python"


@posix_only
def test_argv_has_no_remote(tmp_path):
    argv_file = tmp_path / "argv.json"
    _fake_kit(tmp_path, _OUT + f"pathlib.Path({str(argv_file)!r}).write_text(json.dumps(sys.argv))\n"
              "(out / 'doc.md').write_text('hello')")
    parse_offline(minimal_pdf(["a", "b"]), root=tmp_path, tier="basic", version="4.1.3")
    argv = json.loads(argv_file.read_text())
    assert "--remote" not in argv
    assert argv[1] == "parse"
    assert argv[argv.index("--tier") + 1] == "basic"


@posix_only
def test_page_level_output(tmp_path):
    items = [
        {"type": "text", "text": "first page", "page_idx": 0},
        {"type": "table", "table_body": "<table/>", "page_idx": 1},
        {"type": "text", "text": "second page", "page_idx": 1},
        {"type": "image", "page_idx": 1},
    ]
    _fake_kit(tmp_path, _OUT + f"(out / 'content_list.json').write_text(json.dumps({items!r}))")
    result = parse_offline(minimal_pdf(["a", "b"]), root=tmp_path, tier="basic", version="4.1.3")
    assert "<!-- p2r:page 1 -->\nfirst page" in result.markdown
    assert "<!-- p2r:page 2 -->\n<table/>\n\nsecond page" in result.markdown
    assert result.backend == "offline_mineru"
    assert result.backend_version == "4.1.3"
    assert result.page_count == 2 and result.pages_parsed == 2
    assert result.warnings == []
    assert result.chunks == [{"first_page": 1, "last_page": 2, "status": "done"}]


@posix_only
def test_markdown_only_output(tmp_path):
    _fake_kit(tmp_path, _OUT + "(out / 'sub').mkdir(); (out / 'sub' / 'doc.md').write_text('# Title')")
    result = parse_offline(minimal_pdf(["a", "b"]), root=tmp_path, tier="basic", version="4.1.3")
    assert "<!-- p2r:pages 1-2 -->\n# Title" in result.markdown


@posix_only
def test_plain_md_fallback_warns(tmp_path):
    _fake_kit(tmp_path, _OUT + "(out / 'doc.md').write_text('# Title')")
    result = parse_offline(minimal_pdf(["a", "b"]), root=tmp_path, tier="basic", version="4.1.3")
    assert result.warnings == ["未能按页定位内容，引用页码只能精确到全文"]
    assert result.pages_parsed == 2
    assert result.chunks == [{"first_page": 1, "last_page": 2, "status": "done"}]
    assert result.markdown.startswith("<!-- p2r:pages 1-2 -->\n# Title")


@posix_only
def test_deeply_nested_content_list_is_unusable(tmp_path):
    _fake_kit(tmp_path, _OUT + "(out / 'content_list.json').write_text('[' * 100000 + ']' * 100000)\n"
              "(out / 'doc.md').write_text('fallback body')")
    result = parse_offline(minimal_pdf(["a", "b"]), root=tmp_path, tier="basic", version="4.1.3")
    assert "fallback body" in result.markdown
    assert result.warnings == ["未能按页定位内容，引用页码只能精确到全文"]


@posix_only
def test_no_output_fails(tmp_path):
    _fake_kit(tmp_path, _OUT)
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["a"]), root=tmp_path, tier="basic", version="4.1.3")
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
def test_nonzero_exit_hides_output(tmp_path):
    _fake_kit(tmp_path, "sys.stderr.write('SECRET-DETAIL'); sys.exit(3)")
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["a"]), root=tmp_path, tier="basic", version="4.1.3")
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"
    assert "SECRET" not in str(e.value) and e.value.__cause__ is None


@posix_only
def test_missing_kit_fails(tmp_path):
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["a"]), root=tmp_path, tier="basic", version="4.1.3")
    assert e.value.code == "DOCPARSE_OFFLINE_FAILED"


@posix_only
def test_timeout_kills_tree(tmp_path):
    pid_file = tmp_path / "child.pid"
    _fake_kit(tmp_path, "import subprocess, time\n"
              "c = subprocess.Popen(['sleep', '60'])\n"
              f"pathlib.Path({str(pid_file)!r}).write_text(str(c.pid))\n"
              "time.sleep(60)")
    started = time.monotonic()
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["a"]), root=tmp_path, tier="basic", version="4.1.3", timeout=1)
    assert e.value.code == "DOCPARSE_TIMEOUT"
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@posix_only
def test_cancel_kills_and_raises(tmp_path):
    _fake_kit(tmp_path, "import time\ntime.sleep(60)")
    with pytest.raises(DocParseError) as e:
        parse_offline(minimal_pdf(["a"]), root=tmp_path, tier="basic", version="4.1.3",
                      timeout=30, cancelled=lambda: True)
    assert e.value.code == "DOCPARSE_CANCELLED"


@posix_only
def test_run_tree_keeps_only_tail(tmp_path):
    argv = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000 + 'END')"]
    done = proc.run_tree(argv, env=dict(os.environ), cwd=tmp_path, timeout=20)
    assert done.returncode == 0 and not done.timed_out
    assert len(done.stdout_tail.encode()) <= 4096 and done.stdout_tail.endswith("END")


def test_module_exports():
    assert offline.parse_offline is parse_offline


def test_base_env_forces_utf8(tmp_path):
    assert offline_env(tmp_path)["PYTHONUTF8"] == "1"
    assert install_env(tmp_path)["PYTHONUTF8"] == "1"


@posix_only
def test_kill_tree_survives_taskkill_oserror(tmp_path, monkeypatch):
    """I14: a failing taskkill must still kill and reap the child."""
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    monkeypatch.setattr(proc.sys, "platform", "win32")

    def no_taskkill(*a, **kw):
        raise FileNotFoundError("taskkill")

    monkeypatch.setattr(proc.subprocess, "run", no_taskkill)
    proc._kill_tree(child)
    assert child.poll() is not None


@posix_only
def test_parse_offline_uses_absolute_kit_path(tmp_path, monkeypatch):
    """I10: relative roots resolve against the parent, not the child's cwd."""
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run(argv, *, env, cwd, timeout, cancelled):
        seen["argv"], seen["env"] = argv, env
        return proc.CompletedRun(1, "", "", False)

    with pytest.raises(DocParseError):
        parse_offline(minimal_pdf(["a"]), root=Path("rel"), tier="basic", version="4.1.3", run=fake_run)
    assert Path(seen["argv"][0]).is_absolute()
    assert Path(seen["env"]["MINERU_HOME"]).is_absolute()
