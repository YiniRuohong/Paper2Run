"""Strict configuration and frozen experiment contracts for autonomous runs."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

from .common import canonical_json

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=8192)]
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Project(StrictModel):
    id: Name
    workspace: Text


class Model(StrictModel):
    provider_profile: Name
    credential_ref: Annotated[str, StringConstraints(
        pattern=r"^(credential-store:[A-Za-z0-9_.-]{1,128}|profile:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}|env:[A-Za-z_][A-Za-z0-9_]{0,127})$"
    )]


ResearchTaskType = Literal["paper_analysis", "method_design", "coding", "data_analysis", "review"]


class Specialist(StrictModel):
    id: Name
    description: Text
    task_types: list[ResearchTaskType] = Field(min_length=1, max_length=5)
    model: Model

    @model_validator(mode="after")
    def valid_capabilities(self) -> Specialist:
        if not self.description.strip() or len(set(self.task_types)) != len(self.task_types):
            raise ValueError("specialist requires a description and unique task types")
        return self


class Harness(StrictModel):
    adapter: Literal["pi"] = "pi"
    skills: list[Name] = Field(default_factory=list, max_length=128)


class Target(StrictModel):
    kind: Literal["local", "ssh"]
    profile: Name
    ssh_config_alias: Name | None = None
    remote_workspace: Text | None = None
    allocation: Literal["dedicated"] = "dedicated"
    isolation: Literal["required"] = "required"
    image: Literal["paper2run-experiment:1"] = "paper2run-experiment:1"

    @model_validator(mode="after")
    def paths(self) -> Target:
        if self.kind == "ssh":
            if not self.ssh_config_alias or not self.remote_workspace:
                raise ValueError("ssh requires ssh_config_alias and remote_workspace")
            path = PurePosixPath(self.remote_workspace)
            if not path.is_absolute() or '..' in path.parts or str(path) == '/' or '\\' in self.remote_workspace or any(ord(c) < 32 for c in self.remote_workspace):
                raise ValueError("remote_workspace must be an absolute non-root POSIX path")
        elif self.ssh_config_alias is not None or self.remote_workspace is not None:
            raise ValueError("local target cannot include SSH fields")
        return self


class Limits(StrictModel):
    wall_time_seconds: int = Field(gt=0, le=31_536_000)
    max_model_calls: int = Field(gt=0, le=1_000_000)
    max_model_tokens: int = Field(gt=0, le=10_000_000_000)
    storage_bytes: int = Field(gt=0, le=1_125_899_906_842_624)
    gpu_devices: list[Name] = Field(default_factory=list, max_length=256)


class Network(StrictModel):
    public_internet: bool
    host_and_metadata_access: Literal[False] = False


class Experiment(StrictModel):
    objective: Text
    protocol_change_policy: Literal["version_and_pause_if_outside_authorization"] = "version_and_pause_if_outside_authorization"


class ProjectConfig(StrictModel):
    schema_version: Literal["paper2run.project.v1"] = "paper2run.project.v1"
    project: Project
    model: Model
    harness: Harness
    target: Target
    limits: Limits
    network: Network
    experiment: Experiment
    specialists: list[Specialist] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def unique_specialists(self) -> ProjectConfig:
        ids = [item.id for item in self.specialists]
        if len(set(ids)) != len(ids):
            raise ValueError("specialist IDs must be unique")
        return self


def canonical_hash(value: Any) -> str:
    """Hash canonical JSON bytes; never retain mutable references."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def resolve_project_config(config: ProjectConfig | dict, project_root: Path) -> dict:
    parsed = config if isinstance(config, ProjectConfig) else ProjectConfig.model_validate(config)
    result = parsed.model_dump(mode="json")
    root = project_root.resolve()
    path = (root / parsed.project.workspace).resolve()
    if not path.is_relative_to(root):
        raise ValueError("project.workspace escapes project root")
    result["project"]["workspace"] = str(path)
    return result


def safe_config_snapshot(config: ProjectConfig | dict) -> dict:
    parsed = config if isinstance(config, ProjectConfig) else ProjectConfig.model_validate(config)
    result = parsed.model_dump(mode="json")
    # The reference is not a secret, but need not enter model context or exports.
    result["model"]["credential_ref"] = "[redacted]"
    for specialist in result["specialists"]:
        specialist["model"]["credential_ref"] = "[redacted]"
    return result


class Evaluation(StrictModel):
    adapter: Literal["accuracy", "regression", "none"]
    metric: Literal["accuracy", "rmse", "mae"] | None = None
    direction: Literal["maximize", "minimize"] | None = None
    reference_value: float | None = Field(default=None, allow_inf_nan=False)
    tolerance: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def matching_metric(self) -> Evaluation:
        if self.adapter == "accuracy" and self.metric not in (None, "accuracy"):
            raise ValueError("accuracy adapter requires accuracy metric")
        if self.adapter == "regression" and self.metric not in ("rmse", "mae"):
            raise ValueError("regression requires rmse or mae metric")
        return self


class Provenance(StrictModel):
    labels_sha256: Digest | None = None
    labels_independently_frozen: bool = False
    code_sha256: Digest | None = None
    environment_sha256: Digest | None = None


class ExperimentProtocol(StrictModel):
    schema_version: Literal["paper2run.protocol.v1"] = "paper2run.protocol.v1"
    objective: Text
    mode: Literal["exploratory", "reduced", "original", "comparable"]
    evidence: list[Text] = Field(default_factory=list)
    data_sources: list[Text] = Field(default_factory=list)
    split_hashes: dict[Name, Digest] = Field(default_factory=dict)
    method: Text
    parameters: dict[str, Any] = Field(default_factory=dict)
    entrypoint: Text
    expected_artifacts: list[Text] = Field(default_factory=list)
    evaluation: Evaluation
    allowed_changes: list[Text] = Field(default_factory=list)
    budget: Limits
    provenance: Provenance = Field(default_factory=Provenance)


def freeze_protocol(protocol: dict | ExperimentProtocol) -> dict:
    parsed = protocol if isinstance(protocol, ExperimentProtocol) else ExperimentProtocol.model_validate(protocol)
    snapshot = json.loads(canonical_json(parsed.model_dump(mode="json")))
    return {"protocol": snapshot, "sha256": canonical_hash(snapshot)}
