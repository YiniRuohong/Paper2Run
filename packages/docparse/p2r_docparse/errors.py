from __future__ import annotations

ERROR_CODES = frozenset({
    "DOCPARSE_PDF_INVALID", "DOCPARSE_PAGE_TOO_LARGE", "DOCPARSE_RATE_LIMITED",
    "DOCPARSE_REMOTE_FAILED", "DOCPARSE_TIMEOUT", "DOCPARSE_UNSAFE_URL",
    "DOCPARSE_RESPONSE_TOO_LARGE", "DOCPARSE_OFFLINE_NOT_READY", "DOCPARSE_OFFLINE_FAILED",
    "DOCPARSE_OFFLINE_UNSUPPORTED", "DOCPARSE_CANCELLED", "DOCPARSE_MODE_UNSET",
})


class DocParseError(Exception):
    """Public, code-only failure; never carries raw third-party text."""

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown docparse error code: {code}")
        super().__init__(code)
        self.code = code
