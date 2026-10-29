"""Reproduction of "Gaussian Naive Bayes under Strongly Correlated Features".

Method: per-class mean/variance Gaussian NB scored in log space, with a
variance floor; priors from class frequencies.
Data source: locally synthesised correlated Gaussians, seed 20261003.
Difficulty: feature correlation rho=0.9 violates the independence
assumption, so NB double-counts the shared evidence.
"""
import json
import math
import random

SEED = 20261003
N_TRAIN = 200
N_TEST = 60
RHO = 0.9


def sample(n, rng):
    rows = []
    for _ in range(n):
        cls = 0 if rng.random() < 0.5 else 1
        base = -1.2 if cls == 0 else 1.2
        a = rng.gauss(0.0, 1.0)
        c = rng.gauss(0.0, 1.0)
        b = RHO * a + (1.0 - RHO * RHO) ** 0.5 * c
        rows.append((base + a, base + b, cls))
    return rows


def class_stats(rows, cls, feature):
    values = [r[feature] for r in rows if r[2] == cls]
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, max(var, 1e-6)


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng)
    test = sample(N_TEST, rng)
    stats = {}
    for cls in (0, 1):
        stats[cls] = (class_stats(train, cls, 0), class_stats(train, cls, 1))
    priors = {
        cls: sum(1 for r in train if r[2] == cls) / len(train) for cls in (0, 1)
    }

    def log_score(row, cls):
        total = math.log(max(priors[cls], 1e-12))
        for feature in (0, 1):
            mean, var = stats[cls][feature]
            x = row[feature]
            total += -0.5 * math.log(2.0 * math.pi * var)
            total += -((x - mean) ** 2) / (2.0 * var)
        return total

    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        prediction = 1 if log_score(row, 1) > log_score(row, 0) else 0
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
