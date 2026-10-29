"""Reproduction of "One-Level Decision Stumps on Concentric Rings".

Method: exhaustive axis-aligned stump search (feature x threshold x
polarity) minimising training error over quantile candidate thresholds.
Data source: locally synthesised concentric rings, seed 20261004.
Difficulty: the classes are NOT linearly separable (disc vs annulus), so
a single stump has a hard accuracy ceiling - reported honestly.
"""
import json
import math
import random

SEED = 20261004
N_TRAIN = 240
N_TEST = 80
CANDIDATES = 32


def sample(n, rng):
    rows = []
    for _ in range(n):
        cls = 0 if rng.random() < 0.5 else 1
        if cls == 0:
            radius = (rng.random() ** 0.5) * 0.55
        else:
            radius = 0.65 + rng.random() * 0.35
        angle = rng.random() * 2.0 * math.pi
        rows.append((radius * math.cos(angle), radius * math.sin(angle), cls))
    return rows


def candidate_thresholds(train, feature):
    values = sorted(r[feature] for r in train)
    step = max(1, len(values) // CANDIDATES)
    points = []
    for i in range(step, len(values), step):
        points.append((values[i - 1] + values[i]) / 2.0)
    return points


def stump_error(train, feature, threshold, positive_when_gt):
    errors = 0
    for row in train:
        value = row[feature]
        prediction = 1 if ((value > threshold) == positive_when_gt) else 0
        if prediction != row[2]:
            errors += 1
    return errors


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng)
    test = sample(N_TEST, rng)
    best = None
    for feature in (0, 1):
        for threshold in candidate_thresholds(train, feature):
            for positive_when_gt in (True, False):
                errors = stump_error(train, feature, threshold, positive_when_gt)
                if best is None or errors < best[0]:
                    best = (errors, feature, threshold, positive_when_gt)
    _, feature, threshold, positive_when_gt = best
    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        prediction = 1 if ((row[feature] > threshold) == positive_when_gt) else 0
        if prediction == row[2]:
            correct += 1
        predictions.append({"id": sample_id, "prediction": prediction})
        labels[sample_id] = row[2]
    accuracy = correct / len(test)
    with open("predictions.json", "w") as handle:
        json.dump(predictions, handle)
    with open("labels.json", "w") as handle:
        json.dump(labels, handle)
    with open("metrics.json", "w") as handle:
        json.dump({"metric": "accuracy", "value": accuracy}, handle)
    print("SUMMARY " + json.dumps({"metric": "accuracy", "value": accuracy}))


main()
