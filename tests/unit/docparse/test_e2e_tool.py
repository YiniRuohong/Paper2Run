from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

from p2r_docparse.errors import DocParseError
from p2r_docparse.install import InstallState
from p2r_docparse.markers import ParseResult
from p2r_docparse.testing import minimal_pdf

TOOL = Path(__file__).resolve().parents[3] / 'tools' / 'docparse_e2e.py'

BASE_KEYS = {'mode', 'ok', 'page_count', 'pages_parsed', 'chunks', 'warnings', 'markdown_sha256',
             'markdown_chars', 'elapsed_seconds', 'backend', 'backend_version', 'error_code'}
INSTALL_KEYS = BASE_KEYS | {'state', 'tier', 'version', 'bytes_on_disk', 'step_log'}


@pytest.fixture(scope='module')
def tool():
    spec = importlib.util.spec_from_file_location('docparse_e2e', TOOL)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules['docparse_e2e'] = mod
    spec.loader.exec_module(mod)
    return mod


def _parsed(markdown='<!-- p2r:pages 1-2 -->', backend='online_agent', version='agent-v1'):
    return ParseResult(markdown=markdown, backend=backend, backend_version=version, page_count=2,
                       pages_parsed=2, chunks=[{'first_page': 1, 'last_page': 2, 'status': 'done'}],
                       warnings=[])


def _pdf(tmp_path, name='paper.pdf'):
    path = tmp_path / name
    path.write_bytes(minimal_pdf(['a', 'b']))
    return path


def _invoke(tool, tmp_path, argv, **backends):
    out = io.StringIO()
    rc = tool.main([*argv, '--results-dir', str(tmp_path / 'results')], out=out, backends=backends)
    text = out.getvalue()
    assert text.endswith(chr(10)) and len(text.splitlines()) == 1  # exactly one JSON line
    return rc, json.loads(text)


# ---- argument parsing -------------------------------------------------------
def test_parse_args_online(tool):
    args = tool.parse_args(['--online', 'p.pdf'])
    assert args.mode == 'online' and args.pdf == Path('p.pdf')


def test_parse_args_offline_with_timeout(tool):
    args = tool.parse_args(['--offline', 'p.pdf', '--root', 'r', '--timeout', '5'])
    assert args.mode == 'offline' and args.pdf == Path('p.pdf')
    assert args.root == Path('r') and args.timeout == 5.0


def test_parse_args_offline_install(tool):
    args = tool.parse_args(['--offline-install', 'r'])
    assert args.mode == 'offline-install' and args.root == Path('r')


@pytest.mark.parametrize('argv', [
    [],
    ['--offline', 'p.pdf'],
    ['--online', 'p.pdf', '--offline-install', 'r'],
    ['--online', 'p.pdf', '--root', 'r'],
    ['--offline', 'p.pdf', '--root', 'r', '--timeout', '0'],
])
def test_parse_args_rejects_bad_combinations(tool, argv):
    with pytest.raises(SystemExit) as e:
        tool.parse_args(argv)
    assert e.value.code == 2


# ---- online -----------------------------------------------------------------
def test_online_success_contract(tool, tmp_path):
    pdf = _pdf(tmp_path)
    seen = []

    def backend(data):
        seen.append(data)
        return _parsed()

    rc, doc = _invoke(tool, tmp_path, ['--online', str(pdf)], online=backend)
    md = '<!-- p2r:pages 1-2 -->'
    assert rc == 0 and set(doc) == BASE_KEYS
    assert doc['mode'] == 'online' and doc['ok'] is True and doc['error_code'] is None
    assert doc['page_count'] == 2 and doc['pages_parsed'] == 2 and doc['warnings'] == []
    assert doc['chunks'] == [{'first_page': 1, 'last_page': 2, 'status': 'done'}]
    assert doc['markdown_sha256'] == hashlib.sha256(md.encode('utf-8')).hexdigest()
    assert doc['markdown_chars'] == len(md)
    assert doc['backend'] == 'online_agent' and doc['backend_version'] == 'agent-v1'
    assert isinstance(doc['elapsed_seconds'], float) and doc['elapsed_seconds'] >= 0
    assert seen == [pdf.read_bytes()]
    assert (tmp_path / 'results' / 'online-paper.md').read_text(encoding='utf-8') == md


@pytest.mark.parametrize('name', ['DOCPARSE_RATE_LIMITED', 'DOCPARSE_REMOTE_FAILED',
                                  'DOCPARSE_PDF_INVALID'])
def test_online_docparse_error_keeps_only_the_code(tool, tmp_path, name):
    def backend(data):
        raise DocParseError(name)

    rc, doc = _invoke(tool, tmp_path, ['--online', str(_pdf(tmp_path))], online=backend)
    assert rc != 0 and set(doc) == BASE_KEYS
    assert doc['ok'] is False and doc['error_code'] == name
    assert doc['page_count'] is None and doc['markdown_sha256'] is None
    assert isinstance(doc['elapsed_seconds'], float)
    assert not (tmp_path / 'results' / 'online-paper.md').exists()


def test_unexpected_exception_is_not_echoed(tool, tmp_path):
    def backend(data):
        raise RuntimeError('SECRET-DETAIL at /private/path')

    rc, doc = _invoke(tool, tmp_path, ['--online', str(_pdf(tmp_path))], online=backend)
    assert rc != 0 and doc['ok'] is False and doc['error_code'] == 'E2E_UNEXPECTED'
    assert 'SECRET' not in json.dumps(doc) and '/private' not in json.dumps(doc)


def test_missing_pdf_reports_input_error(tool, tmp_path):
    called = []
    rc, doc = _invoke(tool, tmp_path, ['--online', str(tmp_path / 'nope.pdf')],
                      online=lambda data: called.append(data))
    assert rc != 0 and doc['ok'] is False and doc['error_code'] == 'E2E_INPUT_MISSING'
    assert called == []


# ---- offline ----------------------------------------------------------------
def test_offline_passes_root_and_timeout(tool, tmp_path):
    pdf = _pdf(tmp_path)
    root = tmp_path / 'root'
    seen = {}

    def backend(data, root_, timeout):
        seen.update(root=root_, timeout=timeout)
        return _parsed(backend='offline_mineru', version='4.1.3')

    rc, doc = _invoke(tool, tmp_path, ['--offline', str(pdf), '--root', str(root), '--timeout', '5'],
                      offline=backend)
    assert rc == 0 and set(doc) == BASE_KEYS
    assert doc['mode'] == 'offline' and doc['ok'] is True
    assert doc['backend'] == 'offline_mineru' and doc['backend_version'] == '4.1.3'
    assert seen == {'root': root, 'timeout': 5.0}
    assert (tmp_path / 'results' / 'offline-paper.md').is_file()


def test_offline_timeout_code(tool, tmp_path):
    def backend(data, root, timeout):
        raise DocParseError('DOCPARSE_TIMEOUT')

    rc, doc = _invoke(tool, tmp_path,
                      ['--offline', str(_pdf(tmp_path)), '--root', str(tmp_path / 'r'), '--timeout', '5'],
                      offline=backend)
    assert rc != 0 and doc['ok'] is False and doc['error_code'] == 'DOCPARSE_TIMEOUT'
    assert doc['mode'] == 'offline'


# ---- offline install --------------------------------------------------------
def test_install_success_contract(tool, tmp_path):
    steps = [{'elapsed_seconds': 1.5, 'step': 'venv', 'progress': 0.17}]
    seen = []

    def backend(root):
        seen.append(root)
        return InstallState('ready', tier='standard', version='4.1.3', bytes_on_disk=123), steps

    rc, doc = _invoke(tool, tmp_path, ['--offline-install', str(tmp_path / 'root')], install=backend)
    assert rc == 0 and set(doc) == INSTALL_KEYS
    assert doc['mode'] == 'offline-install' and doc['ok'] is True and doc['error_code'] is None
    assert doc['state'] == 'ready' and doc['tier'] == 'standard' and doc['version'] == '4.1.3'
    assert doc['bytes_on_disk'] == 123 and doc['step_log'] == steps
    assert doc['page_count'] is None and doc['markdown_sha256'] is None
    assert seen == [tmp_path / 'root']


def test_install_failure_hides_state_error(tool, tmp_path):
    def backend(root):
        return InstallState('failed', error='SECRET-PIP-OUTPUT'), []

    rc, doc = _invoke(tool, tmp_path, ['--offline-install', str(tmp_path / 'root')], install=backend)
    assert rc != 0 and doc['ok'] is False and doc['state'] == 'failed'
    assert doc['error_code'] == 'E2E_INSTALL_FAILED'
    assert 'SECRET' not in json.dumps(doc)


def test_install_unsupported_platform(tool, tmp_path):
    def backend(root):
        return InstallState('unsupported'), []

    rc, doc = _invoke(tool, tmp_path, ['--offline-install', str(tmp_path / 'root')], install=backend)
    assert rc != 0 and doc['ok'] is False and doc['error_code'] == 'DOCPARSE_OFFLINE_UNSUPPORTED'
