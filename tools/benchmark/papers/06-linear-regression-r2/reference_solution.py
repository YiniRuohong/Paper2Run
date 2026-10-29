"""Reproduction of "OLS Linear Regression under Contaminated Training Data".

Method: closed-form ordinary least squares on the training split;
evaluation (R^2) on a clean test split.
Data source: locally synthesised, seed 20261006; y = 1.8x + 0.6 + N(0, 0.7).
Difficulty: 6% of TRAINING targets receive +14 (outliers), which drags
the fitted line away from the true relationship; test data is clean.
"""
import json
import random

SEED = 20261006
N_TRAIN = 160
N_TEST = 60
OUTLIER_RATE = 0.06
OUTLIER_SHIFT = 9.0


def sample(n, rng, contaminated):
    rows = []
    for _ in range(n):
        x = rng.uniform(0.0, 4.0)
        y = 1.8 * x + 0.6 + rng.gauss(0.0, 0.7)
        if contaminated and rng.random() < OUTLIER_RATE:
            y += OUTLIER_SHIFT
        rows.append((x, y))
    return rows


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng, True)
    test = sample(N_TEST, rng, False)
    n = float(len(train))
    sx = sum(r[0] for r in train)
    sy = sum(r[1] for r in train)
    sxx = sum(r[0] * r[0] for r in train)
    sxy = sum(r[0] * r[1] for r in train)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n
    predictions = []
    labels = {}
    squared_error = 0.0
    for index, row in enumerate(test):
        sample_id = "test-%02d" % index
        prediction = slope * row[0] + intercept
        squared_error += (row[1] - prediction) ** 2
        predictions.append({"id": sample_id, "prediction": prediction})
        labels[sample_id] = row[1]
    mean_y = sum(labels[key] for key in labels) / len(test)
    total_squared = sum((labels[key] - mean_y) ** 2 for key in labels)
    r2 = 1.0 - squared_error / total_squared
    with open("predictions.json", "w") as handle:
        json.dump(predictions, handle)
    with open("labels.json", "w") as handle:
        json.dump(labels, handle)
    with open("metrics.json", "w") as handle:
        json.dump({"metric": "r2", "value": r2}, handle)
    print("SUMMARY " + json.dumps({"metric": "r2", "value": r2}))


main()
