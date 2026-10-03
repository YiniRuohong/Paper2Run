import pytest

from p2r_evaluation.evaluator import PredictionValidationError, evaluate_predictions, labels_fixture


def prediction_rows():
    return [{"id": sample_id, "prediction": prediction} for sample_id, prediction in labels_fixture().items()]


def test_evaluator_rejects_duplicate_prediction_id():
    rows = prediction_rows() + [{"id": "sample-001", "prediction": 0}]
    with pytest.raises(PredictionValidationError, match="duplicate prediction id"):
        evaluate_predictions(rows)


def test_evaluator_rejects_missing_prediction_id():
    with pytest.raises(PredictionValidationError, match="missing prediction ids"):
        evaluate_predictions(prediction_rows()[:-1])


def test_evaluator_rejects_unknown_prediction_id():
    rows = prediction_rows() + [{"id": "unknown", "prediction": 0}]
    with pytest.raises(PredictionValidationError, match="unknown prediction id"):
        evaluate_predictions(rows)


@pytest.mark.parametrize("value", [True, False, 1.0, "1", None])
def test_evaluator_rejects_invalid_prediction_values(value):
    rows = prediction_rows()
    rows[0]["prediction"] = value
    with pytest.raises(PredictionValidationError, match="finite integer"):
        evaluate_predictions(rows)


def test_evaluator_uses_predictions_not_stdout(capsys):
    rows = prediction_rows()
    rows[0]["prediction"] = 1
    expected = evaluate_predictions(rows)
    print('{"accuracy": 1.0, "correct": 4, "total": 4}')
    actual = evaluate_predictions(rows)
    assert capsys.readouterr().out
    assert actual == expected
    assert actual["metrics"] == {"accuracy": 0.75, "correct": 3, "total": 4}
