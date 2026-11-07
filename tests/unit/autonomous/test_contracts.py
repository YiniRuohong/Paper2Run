import copy

import pytest
from pydantic import ValidationError

from p2r_contracts.autonomous import OUTCOMES, ProjectConfig, canonical_hash, resolve_project_config, safe_config_snapshot


def config():
    return {
        "project": {"id": "test", "workspace": "./workspace"},
        "model": {"provider_profile": "research", "credential_ref": "credential-store:default"},
        "harness": {"adapter": "pi", "skills": ["paper-analysis"]},
        "target": {"kind": "local", "profile": "local"},
        "limits": {"wall_time_seconds": 60, "max_model_calls": 10, "max_model_tokens": 1000, "storage_bytes": 1024, "gpu_devices": []},
        "network": {"public_internet": True, "host_and_metadata_access": False},
        "experiment": {"objective": "test"},
    }


def test_resolution_snapshot_and_hash(tmp_path):
    original = config()
    resolved = resolve_project_config(original, tmp_path)
    assert resolved["project"]["workspace"] == str(tmp_path / "workspace")
    assert original["project"]["workspace"] == "./workspace"
    assert resolved["schema_version"] == "paper2run.project.v1"
    snap = safe_config_snapshot(original)
    assert snap["model"]["credential_ref"] == "[redacted]"
    snap["harness"]["skills"].append("changed")
    assert original["harness"]["skills"] == ["paper-analysis"]
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical_hash({"x": float("nan")})


@pytest.mark.parametrize("section,key,value", [
    ("model", "api_key", "secret"), ("target", "command", "id"),
    ("limits", "wall_time_seconds", 0), ("limits", "max_model_tokens", -1),
    ("limits", "max_model_calls", True), ("limits", "storage_bytes", 2**64),
    ("network", "host_and_metadata_access", True), ("harness", "adapter", "shell"),
    ("model", "credential_ref", "sk-secret"),
])
def test_invalid_config(section, key, value):
    data = config()
    data[section][key] = value
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


def test_every_task_budget_is_optional():
    data = config()
    for key in ("max_model_calls", "max_model_tokens", "wall_time_seconds"):
        del data["limits"][key]
    limits = ProjectConfig.model_validate(data).limits
    assert (limits.max_model_calls, limits.max_model_tokens, limits.wall_time_seconds) == (None, None, None)
    for key in ("max_model_calls", "wall_time_seconds"):
        data["limits"][key] = None
    assert ProjectConfig.model_validate(data).limits.max_model_calls is None


def test_token_limit_is_optional():
    data = config()
    del data["limits"]["max_model_tokens"]
    assert ProjectConfig.model_validate(data).limits.max_model_tokens is None
    data["limits"]["max_model_tokens"] = None
    assert ProjectConfig.model_validate(data).limits.max_model_tokens is None


@pytest.mark.parametrize("reference", [
    "credential-store:default", "credential-store:legacy.v1",
    "profile:research-model_1.v2", "profile:0", "profile:" + "a" * 128,
    "env:OPENAI_API_KEY", "env:_private_key", "env:apiKey2",
])
def test_credential_reference_schemes(reference):
    data = config()
    data["model"]["credential_ref"] = reference
    assert ProjectConfig.model_validate(data).model.credential_ref == reference
    assert safe_config_snapshot(data)["model"]["credential_ref"] == "[redacted]"


@pytest.mark.parametrize("reference", [
    "profile:", "profile:-option", "profile:../secret", "profile:user@host",
    "profile:a;id", "profile:a b", "profile:a\n", "profile:" + "a" * 129,
    "env:", "env:1KEY", "env:API-KEY", "env:API.KEY", "env:${KEY}",
    "env:KEY=value", "env:KEY;id", "env:KEY\n", "env:" + "A" * 129,
    "file:/secret", "credential-store:",
])
def test_invalid_credential_references(reference):
    data = config()
    data["model"]["credential_ref"] = reference
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


@pytest.mark.parametrize("workspace", ["../escape", "/outside"])
def test_workspace_escape(tmp_path, workspace):
    data = config()
    data["project"]["workspace"] = workspace
    with pytest.raises(ValueError, match="escapes"):
        resolve_project_config(data, tmp_path)


def test_symlink_escape(tmp_path):
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    data = config()
    data["project"]["workspace"] = "link/escape"
    with pytest.raises(ValueError):
        resolve_project_config(data, tmp_path)


@pytest.mark.parametrize("alias", ["-oProxyCommand=id", "host;id", "user@host", "host\ncmd", "host secret"])
def test_ssh_alias_not_command(alias):
    data = config()
    data["target"].update(kind="ssh", ssh_config_alias=alias, remote_workspace="/workspace/project")
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(data)


def test_remote_path_not_local_resolved(tmp_path):
    data = config()
    data["target"].update(kind="ssh", ssh_config_alias="gpu", remote_workspace="/workspace/project")
    assert resolve_project_config(data, tmp_path)["target"]["remote_workspace"] == "/workspace/project"
    for path in ["relative", "/", "/workspace/../host"]:
        bad = copy.deepcopy(data)
        bad["target"]["remote_workspace"] = path
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate(bad)


def test_limits_default_repair_rounds():
    assert ProjectConfig.model_validate(config()).limits.max_repair_rounds == 3
    for bad in (11, -1):
        value = config()
        value["limits"]["max_repair_rounds"] = bad
        with pytest.raises(ValidationError):
            ProjectConfig.model_validate(value)
    for ok in (0, 10):
        value = config()
        value["limits"]["max_repair_rounds"] = ok
        assert ProjectConfig.model_validate(value).limits.max_repair_rounds == ok


def test_outcomes_are_fixed():
    assert OUTCOMES == ("verified", "completed_with_issues", "review_unavailable", "blocked_by_safety")


def test_default_output_cap_leaves_room_for_reasoning():
    from p2r_contracts.autonomous import DEFAULT_MAX_TOKENS
    # 4096 was used up by a reasoning model's thinking before any tool call.
    assert DEFAULT_MAX_TOKENS >= 32_768


@pytest.mark.parametrize("entrypoint", ["train.py", "src/run_experiment.py", "scripts/run.sh", "main"])
def test_entrypoint_is_a_script_path(entrypoint):
    from p2r_contracts.autonomous import ExperimentProtocol
    from .test_service import protocol
    ExperimentProtocol.model_validate({**protocol(), "entrypoint": entrypoint})


@pytest.mark.parametrize("entrypoint", [
    "/work/.venv/bin/python run_experiment.py --data-seed 42",  # a whole command: the gate can never match it
    "python train.py", "train.py --epochs 3", "/work/train.py", "./train.py", "../train.py", "a/../b.py", "run.py; rm -rf x",
])
def test_entrypoint_rejects_commands_with_a_hint(entrypoint):
    from pydantic import ValidationError
    from p2r_contracts.autonomous import ExperimentProtocol
    from .test_service import protocol
    with pytest.raises(ValidationError) as caught:
        ExperimentProtocol.model_validate({**protocol(), "entrypoint": entrypoint})
    assert "put the interpreter and arguments in parameters" in str(caught.value)
