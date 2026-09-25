from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


def _validate_json(value: Any, path: str = "$") -> None:
    """Validate the deliberately limited JSON value set used by this contract."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number at {path} is not valid JSON")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"JSON object key at {path} must be a string")
            _validate_json(item, f"{path}.{key}")
        return
    raise TypeError(f"unsupported JSON value at {path}: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize the contract's JSON subset deterministically.

    This is *not* a claim of RFC 8785/JCS compatibility. The contract is limited
    to JSON primitives, arrays, and objects with string keys; objects are sorted
    lexicographically by Python's Unicode code points, strings are emitted as
    UTF-8 characters, and non-finite numbers are rejected. Consumers needing
    full JCS number/string canonicalization must use a dedicated JCS library.
    """
    _validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class FieldError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    code: str
    message: str


class Ok(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    data: T


class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class Meta(BaseModel):
    request_id: str


class OkEnvelope(BaseModel, Generic[T]):
    data: T
    meta: Meta

    def __init__(self, *, request_id: str, **data: Any) -> None:
        super().__init__(data=data["data"], meta=Meta(request_id=request_id))


class ErrorEnvelope(BaseModel):
    error: ApiError
    meta: Meta

    def __init__(self, *, error: ApiError, request_id: str) -> None:
        super().__init__(error=error, meta=Meta(request_id=request_id))
