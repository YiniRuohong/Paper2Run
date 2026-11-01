from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SOURCE = 256 * 1024
MAX_OUTPUT = 256 * 1024


class ExecuteCellRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=MAX_SOURCE)
    timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    @field_validator("source")
    @classmethod
    def source_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_SOURCE:
            raise ValueError("source exceeds 256 KiB")
        return value


class ExecuteCellResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stdout: str = ""
    stderr: str = ""
    result_summary: str = ""
    namespace_hash: str
    error_code: str | None = None
