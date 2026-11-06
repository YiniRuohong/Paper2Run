"""Exact loopback origins for the local runtime's write guard (fix round 1, IMPORTANT 2)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from p2r_api.auth import CsrfError, parse_loopback_origins, validate_origin

ROOT = Path(__file__).resolve().parents[2]


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value, expected", [
    ("http://127.0.0.1:8010", ("http://127.0.0.1:8010",)),
    ("http://127.0.0.1:8010,http://localhost:8010", ("http://127.0.0.1:8010", "http://localhost:8010")),
    (" http://localhost:5173 , http://[::1]:5173 ", ("http://localhost:5173", "http://[::1]:5173")),
])
def test_parse_accepts_exact_loopback_origins(value, expected):
    assert parse_loopback_origins(value) == expected


@pytest.mark.parametrize("value", [
    "https://evil.example",            # not loopback, not http
    "http://evil.example:8010",        # not loopback
    "http://127.0.0.1:8010/app",       # path
    "http://127.0.0.1:8010/",          # path (an Origin never has one)
    "http://user:pw@127.0.0.1:8010",   # userinfo
    "http://user@127.0.0.1:8010",      # userinfo
    "http://127.0.0.1",                # missing port
    "https://127.0.0.1:8010",          # scheme
    "http://127.0.0.1:8010?x=1",       # query
    "http://127.0.0.1:8010#f",         # fragment
    "http://127.0.0.1:99999",          # invalid port
    "http://127.0.0.1:8010,",          # empty entry
    "",                                # nothing configured
])
def test_parse_rejects_anything_else(value):
    with pytest.raises(ValueError, match="P2R_ALLOWED_ORIGINS"):
        parse_loopback_origins(value)


def test_validate_origin_accepts_only_exact_members_of_a_set():
    allowed = ("http://127.0.0.1:8010", "http://localhost:8010")
    validate_origin("http://127.0.0.1:8010", allowed)
    validate_origin("http://localhost:8010", allowed)
    for origin in (None, "", "http://127.0.0.1:5173", "http://evil.example", "http://127.0.0.1:8010/", "HTTP://127.0.0.1:8010"):
        with pytest.raises(CsrfError):
            validate_origin(origin, allowed)


def test_validate_origin_set_fails_closed_on_an_invalid_entry():
    with pytest.raises(CsrfError):
        validate_origin("http://127.0.0.1:8010", ("http://127.0.0.1:8010", "ftp://x"))
    with pytest.raises(CsrfError):
        validate_origin("http://127.0.0.1:8010", ())


def test_single_origin_behaviour_is_unchanged():
    validate_origin("https://p2r.example", "https://p2r.example")
    with pytest.raises(CsrfError):
        validate_origin("https://other.example", "https://p2r.example")


def test_launcher_exports_both_loopback_hosts_for_the_bound_port():
    launch = load_script("_origins_launch", "scripts/launch.py")
    value = launch.allowed_origins_env(8012)
    assert value == "http://127.0.0.1:8012,http://localhost:8012"
    assert parse_loopback_origins(value) == ("http://127.0.0.1:8012", "http://localhost:8012")


def test_desktop_backend_allows_exactly_its_host_guard_origins():
    serve = load_script("_origins_serve", "apps/desktop/backend/serve.py")
    origins = serve.allowed_origins(49152)
    assert origins == tuple(f"http://{host}:49152" for host in serve.LOOPBACK_HOSTS)
    assert parse_loopback_origins(",".join(origins)) == origins


def test_launcher_downloads_parse_models_from_modelscope_unless_overridden():
    launch = load_script("p2r_launch_for_docparse_source", "scripts/launch.py")
    assert launch.DOCPARSE_MODEL_SOURCE == "modelscope"
    source = (ROOT / "scripts/launch.py").read_text(encoding="utf-8")
    # setdefault: a user-set P2R_DOCPARSE_MODEL_SOURCE wins over the default.
    assert 'env.setdefault("P2R_DOCPARSE_MODEL_SOURCE", DOCPARSE_MODEL_SOURCE)' in source
