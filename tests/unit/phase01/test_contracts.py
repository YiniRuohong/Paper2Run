import pytest

from p2r_contracts.common import (
    ApiError,
    ErrorEnvelope,
    FieldError,
    OkEnvelope,
    canonical_json,
    sha256_hex,
)


def test_canonical_json_is_stable_and_hashable():
    assert canonical_json({"b": 1, "a": "中文"}) == '{"a":"中文","b":1}'
    assert sha256_hex({"b": 1, "a": "中文"}) == sha256_hex('{"a":"中文","b":1}')


def test_canonical_json_covers_numbers_unicode_and_key_order():
    left = {"z": [1, 1.5, -2], "text": "é/中文"}
    right = {"text": "é/中文", "z": [1, 1.5, -2]}
    assert canonical_json(left) == canonical_json(right)
    assert canonical_json({"n": 1.0}) == '{"n":1.0}'


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value):
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"value": value})


def test_envelopes_and_error_dto():
    ok = OkEnvelope(data={"value": 1}, request_id="req-1")
    assert ok.meta.request_id == "req-1"
    error = ErrorEnvelope(
        error=ApiError(
            code="INVALID",
            message="bad input",
            retryable=False,
            details={"field_errors": [FieldError(path="/name", code="required", message="required")]},
        ),
        request_id="req-1",
    )
    assert error.error.details["field_errors"][0].path == "/name"
