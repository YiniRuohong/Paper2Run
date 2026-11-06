"""Deterministic built-in evaluation; no experiment-supplied code is executed.

The caller must store the frozen envelope in trusted control storage. A digest
checks integrity, not authorship; an agent-controlled envelope is not evidence.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import errno
import random
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from p2r_contracts.autonomous import canonical_hash, freeze_protocol
from .safe_read import UnsafePath, open_below_root_windows

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
EVALUATOR_VERSION = "paper2run.builtin.v2"
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261104


def bootstrap_mean_ci(values: list[float], *, replicates: int = BOOTSTRAP_REPLICATES,
                      seed: int = BOOTSTRAP_SEED) -> dict[str, float]:
    """Deterministic percentile 95% confidence interval for a sample mean."""
    try:
        finite = bool(values) and all(type(value) in (int, float) and math.isfinite(value)
                                      for value in values)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("bootstrap values must be a nonempty finite numeric sequence")
    if type(replicates) is not int or replicates < 100:
        raise ValueError("bootstrap requires at least 100 replicates")
    if type(seed) is not int:
        raise ValueError("bootstrap seed must be an integer")
    return _bootstrap_ci(values, _finite_mean, replicates=replicates, seed=seed)


def _finite_mean(values: list[float]) -> float:
    # Preserve ordinary-input rounding; scale only if the intermediate sum overflows.
    try:
        return math.fsum(values) / len(values)
    except OverflowError:
        scale = max(abs(value) for value in values)
        return (math.fsum(value / scale for value in values) / len(values)) * scale


def _scaled_rms(values: list[float]) -> float:
    scale = max(abs(value) for value in values)
    if scale == 0:
        return 0.0
    return math.sqrt(math.fsum((value / scale) ** 2 for value in values) / len(values)) * scale


def _bootstrap_rms(values: list[float]) -> float:
    squares = [value * value for value in values]
    # Subnormal squares lose precision even when they have not rounded to zero.
    if any(not math.isfinite(square) or (value != 0 and square < float.fromhex("0x1p-1022"))
           for value, square in zip(values, squares)):
        return _scaled_rms(values)
    return math.sqrt(_finite_mean(squares))


def _bootstrap_ci(values, statistic, *, replicates=BOOTSTRAP_REPLICATES, seed=BOOTSTRAP_SEED):
    rng = random.Random(seed)
    size = len(values)
    estimates = sorted(statistic([values[rng.randrange(size)] for _ in range(size)])
                       for _ in range(replicates))
    if any(not math.isfinite(value) for value in estimates):
        raise ValueError("confidence interval must be finite")
    return {"lower": estimates[int(0.025 * (replicates - 1))],
            "upper": estimates[int(0.975 * (replicates - 1))]}


def _read_mode() -> str | None:
    """POSIX dir_fd/O_NOFOLLOW wherever available; the Windows checks only on Windows."""
    if hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd:
        return "posix"
    if os.name == "nt":
        return "windows"
    return None


@contextmanager
def _artifact_reader(root: Path):
    """Yield ``read(name) -> (value, sha256)`` for a trusted artifact directory.

    Any other platform fails closed.
    """
    mode = _read_mode()
    if mode == "posix":
        with _artifact_root(root) as root_fd:
            yield lambda name: _read(root_fd, name)
    elif mode == "windows":
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError("unsafe artifact root: expected absolute path without traversal")
        yield lambda name: _read_windows(root, name)
    else:
        raise ValueError("safe artifact reads require POSIX no-follow directory fds or Windows reparse-point checks")


@contextmanager
def _artifact_root(root: Path):
    """Pin a directory below the trusted filesystem root, rejecting every symlink.

    Callers supply an absolute artifact location in control-owned storage. On
    platforms without POSIX no-follow/dir-fd support we fail closed. The caller
    must canonicalize trusted OS aliases (e.g. macOS /var) before passing paths.
    """
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("safe artifact reads require POSIX no-follow directory fds")
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("unsafe artifact root: expected absolute path without traversal")
    fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in root.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise ValueError("unsafe artifact root: symlink or non-directory") from exc
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(root: int, name: str) -> tuple[Any, str]:
    if name not in ("predictions.json", "labels.json"):
        raise ValueError("unsafe artifact name")
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ValueError(f"unsafe artifact path: {name}") from exc
        raise
    return _parse(fd, name)


def _read_windows(root: Path, name: str) -> tuple[Any, str]:
    if name not in ("predictions.json", "labels.json"):
        raise ValueError("unsafe artifact name")
    try:
        fd = open_below_root_windows(root, [name])
    except UnsafePath as exc:
        raise ValueError(f"unsafe artifact path: {name}: {exc}") from exc
    return _parse(fd, name)


def _parse(fd: int, name: str) -> tuple[Any, str]:
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(f"unsafe artifact type: {name}")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError(f"artifact exceeds size limit: {name}")
        raw = stream.read(MAX_ARTIFACT_BYTES + 1)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError(f"artifact changed while reading: {name}")
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError(f"artifact exceeds size limit: {name}")
    value = json.loads(raw, parse_constant=lambda value: _invalid(f"nonfinite JSON: {value}"))
    return value, hashlib.sha256(raw).hexdigest()


def _invalid(message: str) -> Any:
    raise ValueError(message)


def _values(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a nonempty list of id/{field} records")
    result = {}
    for row in value:
        if not isinstance(row, dict) or set(row) != {"id", field}:
            raise ValueError(f"record must contain exactly id and {field}")
        key = row["id"]
        if not isinstance(key, str) or not key or key in result:
            raise ValueError("sample ids must be unique nonempty strings")
        item = row[field]
        try:
            finite = type(item) in (int, float) and math.isfinite(item)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{field} must be finite numeric values")
        result[key] = item
    return result


def evaluate_artifacts(frozen: dict, artifacts_dir: Path) -> dict:
    if set(frozen) != {"protocol", "sha256"} or canonical_hash(frozen["protocol"]) != frozen["sha256"]:
        raise ValueError("frozen protocol integrity check failed")
    validated = freeze_protocol(frozen["protocol"])
    if validated != frozen:
        raise ValueError("frozen protocol must contain a complete normalized snapshot")
    protocol = validated["protocol"]
    result = {"protocol_sha256": frozen["sha256"], "evaluator_version": EVALUATOR_VERSION,
              "verdict": "not_comparable", "comparison_eligible": False, "reasons": [],
              "metrics": {}, "confidence_intervals": {}, "artifact_hashes": {}}
    evaluation = protocol["evaluation"]
    if evaluation["adapter"] == "none":
        result["reasons"].append("no supported evaluation adapter selected")
        return result
    try:
        with _artifact_reader(artifacts_dir) as read:
            predictions, predictions_hash = read("predictions.json")
            labels, labels_hash = read("labels.json")
    except FileNotFoundError as exc:
        result["reasons"].append(f"missing evaluation artifact: {Path(exc.filename).name}")
        return result
    result["artifact_hashes"] = {"predictions.json": predictions_hash, "labels.json": labels_hash}
    provenance = protocol["provenance"]
    if provenance["labels_sha256"] and labels_hash != provenance["labels_sha256"]:
        raise ValueError("labels differ from independently frozen hash")
    predicted, expected = _values(predictions, "prediction"), _values(labels, "label")
    if predicted.keys() != expected.keys():
        raise ValueError("prediction and label sample ids must match exactly")
    keys = sorted(expected)
    if evaluation["adapter"] == "accuracy":
        if any(type(v) is not int for v in [*predicted.values(), *expected.values()]):
            raise ValueError("accuracy requires integer class labels")
        metric = "accuracy"
        correctness = [float(predicted[k] == expected[k]) for k in keys]
        correct = sum(correctness)
        score = correct / len(keys)
        result["metrics"]["correct"] = int(correct)
        result["confidence_intervals"]["accuracy_95"] = bootstrap_mean_ci(correctness)
    else:
        metric = evaluation["metric"]
        try:
            errors = [float(predicted[k]) - float(expected[k]) for k in keys]
            if any(not math.isfinite(error) for error in errors):
                raise ValueError("metric calculation requires finite residuals")
            # Preserve existing metric rounding; only rescue an overflowing norm.
            score = math.hypot(*errors) / math.sqrt(len(keys)) if metric == "rmse" else math.fsum(abs(e) / len(keys) for e in errors)
            if metric == "rmse" and not math.isfinite(score):
                score = _scaled_rms(errors)
        except (OverflowError, ValueError) as exc:
            raise ValueError("metric calculation overflow") from exc
        if not math.isfinite(score):
            raise ValueError("metric calculation must be finite")
        if metric == "rmse":
            interval = _bootstrap_ci(errors, _bootstrap_rms)
        else:
            interval = bootstrap_mean_ci([abs(error) for error in errors])
        result["confidence_intervals"][f"{metric}_95"] = interval
    result["metrics"].update({metric: score, "total": len(keys)})
    reasons = result["reasons"]
    if protocol["mode"] != "comparable":
        reasons.append("protocol mode is not comparable")
    for field in ("evidence", "data_sources", "split_hashes"):
        if not protocol[field]:
            reasons.append(f"missing {field}")
    for field in ("labels_sha256", "labels_independently_frozen", "code_sha256", "environment_sha256"):
        if not provenance[field]:
            reasons.append(f"missing independently frozen provenance: {field}")
    if evaluation["reference_value"] is None or evaluation["tolerance"] is None or evaluation["direction"] is None:
        reasons.append("missing reference value, tolerance or direction")
    if not reasons:
        result["comparison_eligible"] = True
        # This is a numerical comparison, never a claim of reproduction success.
        result["verdict"] = "comparison_available"
        result["within_tolerance"] = abs(score - evaluation["reference_value"]) <= evaluation["tolerance"]
    return result


def evidence_manifest(frozen: dict, artifacts_dir: Path) -> dict:
    """Return a detached manifest of only built-in evaluation inputs and outputs."""
    result = evaluate_artifacts(frozen, artifacts_dir)
    return {"schema_version": "paper2run.evidence.v2", "evaluation": result,
            "sha256": canonical_hash(result)}
