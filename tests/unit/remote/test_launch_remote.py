"""launch.py starts the p2r-tunnel sidecar, or explains why it can't."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def load_launch():
    spec = importlib.util.spec_from_file_location("p2r_launch_under_test", ROOT / "scripts" / "launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_config(path: Path) -> Path:
    path.write_text(json.dumps({"relay_url": "https://60.204.202.153:12234", "relay_quic_port": 12234,
                                "rendezvous_id": "e" * 64}))
    return path


def test_remote_command_uses_config_and_backend_port(tmp_path):
    launch = load_launch()
    binary = tmp_path / "p2r-tunnel"
    cmd = launch.remote_command(binary, write_config(tmp_path / "remote.json"), tmp_path / "ca.pem", tmp_path / "data", 8017)
    assert cmd[0] == str(binary) and cmd[1] == "serve"
    args = dict(zip(cmd[2::2], cmd[3::2]))
    assert args == {
        "--key": str(tmp_path / "data" / "desktop.key"),
        "--relay": "https://60.204.202.153:12234",
        "--relay-quic-port": "12234",
        "--relay-ca": str(tmp_path / "ca.pem"),
        "--backend-port": "8017",
        "--devices": str(tmp_path / "data" / "devices.json"),
        "--rendezvous": "e" * 64,
        "--control-file": str(tmp_path / "data" / "control.json"),
    }


def test_remote_unavailable_reason_without_binary_or_cargo(tmp_path, monkeypatch):
    launch = load_launch()
    monkeypatch.setattr(launch, "find_cargo", lambda: None)
    binary, reason = launch.ensure_remote_binary(tmp_path / "missing-p2r-tunnel")
    assert binary is None
    assert "cargo" in reason


def test_remote_config_must_name_a_rendezvous(tmp_path):
    launch = load_launch()
    bad = tmp_path / "remote.json"
    bad.write_text(json.dumps({"relay_url": "https://x:1", "relay_quic_port": 1, "rendezvous_id": ""}))
    try:
        launch.remote_command(tmp_path / "bin", bad, tmp_path / "ca.pem", tmp_path / "data", 1)
    except ValueError as exc:
        assert "rendezvous" in str(exc)
    else:
        raise AssertionError("empty rendezvous_id accepted")
