"""Trusted, deterministic offline evaluation primitives for Paper2Run."""
from .evaluator import (
    EVALUATOR_VERSION,
    FIXTURE_LABELS,
    PredictionValidationError,
    evaluate_predictions,
    labels_fixture,
)

__all__ = [
    "EVALUATOR_VERSION", "FIXTURE_LABELS", "PredictionValidationError",
    "evaluate_predictions", "labels_fixture",
]
