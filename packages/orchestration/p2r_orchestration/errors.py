from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """A failure with a fixed public code and HTTP status; the API renders it as an error envelope."""

    status = 400
    code = "SERVICE_ERROR"

    def __init__(self, message: str | None = None, *, details: dict[str, Any] | None = None):
        self.message = message or self.code
        self.details = details or {}
        super().__init__(self.message)
