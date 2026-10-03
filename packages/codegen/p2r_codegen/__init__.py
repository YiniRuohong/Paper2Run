"""Deterministic, side-effect-free code candidate and repair boundaries."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


class CodegenError(ValueError):
    pass


class SecretLeakError(CodegenError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _validate_path(path: str, allowlist: set[str]) -> None:
    if not isinstance(path, str) or not path or path != path.strip() or "\\" in path or "\x00" in path or any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise CodegenError("path must be a relative canonical POSIX path")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path) or path.endswith("/") or "//" in path:
        raise CodegenError("path must be a relative canonical POSIX path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise CodegenError("path traversal or non-canonical path")
    if path not in allowlist:
        raise CodegenError(f"path is not allowlisted: {path}")


@dataclass(frozen=True)
class Candidate:
    files: dict[str, str]
    manifest: dict[str, dict[str, Any]]
    base_hash: str
    allowlist: frozenset[str] = frozenset()
    max_file_size: int = 0
    max_total_size: int = 0
    directory_paths: frozenset[str] = frozenset()


def build_candidate(
    files: Mapping[str, str], *, allowlist: set[str], max_file_size: int = 100_000, max_total_size: int = 1_000_000,
    declared_hash: str | None = None,
    directory_paths: set[str] | None = None,
) -> Candidate:
    if max_file_size < 0 or max_total_size < 0:
        raise CodegenError("size limits must be non-negative")
    normalized: dict[str, str] = {}
    manifest: dict[str, dict[str, Any]] = {}
    total = 0
    directories = set(directory_paths or ())
    for directory in directories:
        _validate_path(directory, allowlist)
    for path, content in files.items():
        _validate_path(path, allowlist)
        if path in directories:
            raise CodegenError(f"path is a directory: {path}")
        if not isinstance(content, str):
            raise CodegenError("file content must be text")
        size = len(content.encode("utf-8"))
        if size > max_file_size:
            raise CodegenError(f"file exceeds size limit: {path}")
        total += size
        if total > max_total_size:
            raise CodegenError("candidate exceeds total size limit")
        normalized[path] = content
        manifest[path] = {"size": size, "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}
    sorted_paths = sorted(normalized)
    for parent, child in zip(sorted_paths, sorted_paths[1:]):
        if child.startswith(parent + "/"):
            raise CodegenError("file and directory prefix conflict")
    manifest = {path: manifest[path] for path in sorted(manifest)}
    digest = _sha(manifest)
    if declared_hash is not None and declared_hash != digest:
        raise CodegenError("declared hash does not match canonical manifest")
    return Candidate(files={path: normalized[path] for path in sorted(normalized)}, manifest=manifest, base_hash=digest, allowlist=frozenset(allowlist), max_file_size=max_file_size, max_total_size=max_total_size, directory_paths=frozenset(directories))


@dataclass(frozen=True)
class PatchOperation:
    operation: str
    path: str
    content: str | None = None
    expected_hash: str | None = None


@dataclass(frozen=True)
class Patch:
    base_hash: str
    operations: tuple[PatchOperation, ...]


def patch_hash(patch: Patch) -> str:
    return _sha({"base_hash": patch.base_hash, "operations": [{"operation": op.operation, "path": op.path, "content": op.content, "expected_hash": op.expected_hash} for op in patch.operations]})


def _candidate_integrity(candidate: Candidate) -> None:
    checked = build_candidate(candidate.files, allowlist=set(candidate.allowlist), max_file_size=candidate.max_file_size, max_total_size=candidate.max_total_size, directory_paths=set(candidate.directory_paths))
    if checked.manifest != candidate.manifest or checked.base_hash != candidate.base_hash or checked.directory_paths != candidate.directory_paths:
        raise CodegenError("candidate metadata does not match files")


def apply_patch(candidate: Candidate, patch: Patch, *, allowlist: set[str] | None = None, max_file_size: int | None = None, max_total_size: int | None = None) -> Candidate:
    _candidate_integrity(candidate)
    if patch.base_hash != candidate.base_hash:
        raise CodegenError("patch base hash does not match candidate")
    if allowlist is not None and set(allowlist) != set(candidate.allowlist):
        raise CodegenError("patch cannot override candidate allowlist")
    if max_file_size is not None and max_file_size > candidate.max_file_size:
        raise CodegenError("patch cannot relax file size limit")
    if max_total_size is not None and max_total_size > candidate.max_total_size:
        raise CodegenError("patch cannot relax total size limit")
    allowed = set(candidate.allowlist)
    files = dict(candidate.files)
    seen: set[str] = set()
    if not patch.operations:
        raise CodegenError("patch must contain operations")
    for op in patch.operations:
        if op.path in seen:
            raise CodegenError("patch contains duplicate path operation")
        seen.add(op.path)
        if op.operation not in {"add", "replace", "delete"}:
            raise CodegenError("unsupported patch operation")
        _validate_path(op.path, allowed)
        exists = op.path in files
        if op.operation == "add" and exists:
            raise CodegenError("add target already exists")
        if op.operation in {"replace", "delete"} and not exists:
            raise CodegenError("patch target does not exist")
        if op.operation in {"replace", "delete"} and op.expected_hash is None:
            raise CodegenError("replace/delete requires expected hash")
        if op.expected_hash is not None:
            actual = hashlib.sha256(files[op.path].encode("utf-8")).hexdigest() if exists else None
            if actual != op.expected_hash:
                raise CodegenError("patch expected hash does not match")
        if op.operation == "delete":
            if op.content is not None:
                raise CodegenError("delete cannot contain content")
            del files[op.path]
        else:
            if not isinstance(op.content, str):
                raise CodegenError("add/replace requires text content")
            files[op.path] = op.content
    return build_candidate(files, allowlist=allowed, max_file_size=candidate.max_file_size, max_total_size=candidate.max_total_size)


@dataclass(frozen=True)
class RepairBudget:
    spent: int
    budget: int
    repairs_used: int = 0


def next_repair_budget(state: RepairBudget, requested_cost: int = 1) -> RepairBudget:
    if not isinstance(requested_cost, int) or isinstance(requested_cost, bool) or requested_cost < 1:
        raise CodegenError("requested_cost must be a positive integer")
    if state.spent + requested_cost > state.budget:
        raise CodegenError("repair budget exhausted")
    return RepairBudget(state.spent + requested_cost, state.budget, state.repairs_used + 1)


def propose_repair(candidate: Candidate, patch: Patch, **gate_kwargs: Any) -> Any:
    decision = repair_gate(**gate_kwargs)
    if decision.status != "allowed":
        return decision
    return apply_patch(candidate, patch)


@dataclass(frozen=True)
class RepairDecision:
    status: str
    reason: str | None = None


def repair_gate(*, ordinal: int, error_code: str, base_hash: str, spent: int, budget: int, previous_errors: set[tuple[str, str]] | None = None, needs_revision: bool = False, requested_cost: int = 1) -> RepairDecision:
    for name, value in (("ordinal", ordinal), ("spent", spent), ("budget", budget), ("requested_cost", requested_cost)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CodegenError(f"{name} must be a non-negative integer")
    if ordinal < 1 or ordinal > 5:
        return RepairDecision("stopped", "repair ordinal must be between 1 and 5")
    if requested_cost < 1:
        raise CodegenError("requested_cost must be a positive integer")
    if needs_revision:
        return RepairDecision("needs_revision", "repair would change experiment semantics")
    if spent + requested_cost > budget:
        return RepairDecision("stopped", "repair budget exhausted")
    if (error_code, base_hash) in (previous_errors or set()):
        return RepairDecision("stopped", "repeated error for same base code")
    return RepairDecision("allowed")


_SECRET_KEY = re.compile(r"(secret|token|password|passwd|api[_-]?key|authorization|private[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|bearer\s+[A-Za-z0-9._-]{12,})", re.I)


def _reject_secret(value: Any, key: str = "") -> None:
    if _SECRET_KEY.search(key):
        raise SecretLeakError("audit payload contains secret-like field")
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        raise SecretLeakError("audit payload contains secret-like value")
    if isinstance(value, Mapping):
        for k, v in value.items():
            _reject_secret(v, str(k))
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_secret(item, key)


def audit_payload(
    *,
    source: str,
    base_hash: str | None = None,
    patch_hash: str | None = None,
    validation: Mapping[str, Any] | None = None,
    repair_ordinal: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    if validation is None:
        raise CodegenError("audit payload requires validation")
    if source not in {"template", "model", "repair"}:
        raise CodegenError("source must be template, model, or repair")
    for name, value in (("base_hash", base_hash), ("patch_hash", patch_hash)):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)):
            raise CodegenError(f"{name} must be a lowercase SHA-256 hex digest")
    if source == "repair" and (base_hash is None or patch_hash is None):
        raise CodegenError("repair audit requires base and patch hashes")
    if repair_ordinal is not None and (isinstance(repair_ordinal, bool) or not isinstance(repair_ordinal, int) or not 1 <= repair_ordinal <= 5):
        raise CodegenError("repair_ordinal must be an integer from 1 through 5")
    if error_code is not None and (not isinstance(error_code, str) or not error_code or len(error_code) > 100 or any(ord(c) < 32 or ord(c) == 127 for c in error_code)):
        raise CodegenError("error_code must be a bounded printable string")
    if validation is not None:
        if set(validation) - {"ok", "static_issues"} or not isinstance(validation.get("ok"), bool):
            raise CodegenError("validation must contain only typed ok/static_issues fields")
        if "static_issues" in validation and (
            not isinstance(validation["static_issues"], list)
            or len(validation["static_issues"]) > 50
            or not all(isinstance(v, str) and 0 < len(v) <= 200 and all(32 <= ord(c) < 127 for c in v) for v in validation["static_issues"])
        ):
            raise CodegenError("validation static_issues must be a bounded list of printable strings")
    payload: dict[str, Any] = {"source": source}
    if base_hash is not None:
        payload["base_hash"] = base_hash
    if patch_hash is not None:
        payload["patch_hash"] = patch_hash
    payload["validation"] = dict(validation)
    if repair_ordinal is not None:
        payload["repair_ordinal"] = repair_ordinal
    if error_code is not None:
        payload["error_code"] = error_code
    _reject_secret(payload)
    return payload
