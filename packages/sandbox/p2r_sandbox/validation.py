"""Shape validation for sandbox-collected output files.

``predictions.json`` must be a JSON list of rows ``{"id": str,
"prediction": int}`` (exactly those keys, matching the offline evaluator's
strictness) and ``labels.json`` must be a JSON object mapping id -> int.
Anything else raises :class:`~p2r_sandbox.errors.SandboxOutputError`.
"""

from __future__ import annotations

import json
from typing import Mapping

from .errors import SandboxOutputError

PREDICTIONS_FILE = "predictions.json"
LABELS_FILE = "labels.json"


def _decode(name: str, raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SandboxOutputError(f"{name} is not valid UTF-8 JSON: {exc}") from exc


def _is_int(value: object) -> bool:
    # bool is a subclass of int in Python but is not an integer class label.
    return isinstance(value, int) and not isinstance(value, bool)


def validate_output_files(files: Mapping[str, bytes]) -> tuple[list, dict]:
    """Validate collected sandbox outputs; returns ``(predictions, labels)``.

    ``predictions`` is a list of ``{"id": str, "prediction": int}`` rows and
    ``labels`` is a ``dict[str, int]``. Raises
    :class:`SandboxOutputError` when a file is missing, not valid JSON, or
    shaped differently (wrong container type, missing/extra row keys,
    non-string or empty ids, duplicate ids, non-integer labels/predictions).
    """
    missing = [name for name in (PREDICTIONS_FILE, LABELS_FILE) if name not in files]
    if missing:
        raise SandboxOutputError(f"missing output files: {', '.join(missing)}")

    predictions_raw = _decode(PREDICTIONS_FILE, files[PREDICTIONS_FILE])
    if not isinstance(predictions_raw, list):
        raise SandboxOutputError(
            f"{PREDICTIONS_FILE} must be a JSON list of {{id, prediction}} rows, "
            f"got {type(predictions_raw).__name__}"
        )
    predictions: list = []
    seen_ids: set[str] = set()
    for index, row in enumerate(predictions_raw):
        if not isinstance(row, dict) or set(row) != {"id", "prediction"}:
            raise SandboxOutputError(
                f"{PREDICTIONS_FILE} row {index} must contain exactly id and prediction"
            )
        sample_id = row["id"]
        if not isinstance(sample_id, str) or not sample_id:
            raise SandboxOutputError(
                f"{PREDICTIONS_FILE} row {index} id must be a non-empty string"
            )
        if sample_id in seen_ids:
            raise SandboxOutputError(f"duplicate prediction id: {sample_id}")
        if not _is_int(row["prediction"]):
            raise SandboxOutputError(
                f"{PREDICTIONS_FILE} row {index} prediction must be an integer"
            )
        seen_ids.add(sample_id)
        predictions.append({"id": sample_id, "prediction": row["prediction"]})

    labels_raw = _decode(LABELS_FILE, files[LABELS_FILE])
    if not isinstance(labels_raw, dict):
        raise SandboxOutputError(
            f"{LABELS_FILE} must be a JSON object mapping id to int, "
            f"got {type(labels_raw).__name__}"
        )
    labels: dict = {}
    for sample_id, value in labels_raw.items():
        if not isinstance(sample_id, str) or not sample_id:
            raise SandboxOutputError(f"{LABELS_FILE} ids must be non-empty strings")
        if not _is_int(value):
            raise SandboxOutputError(
                f"{LABELS_FILE} value for {sample_id!r} must be an integer"
            )
        labels[sample_id] = value

    return predictions, labels
