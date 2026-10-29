"""Reproduction of "Class-Weighted Logistic Regression under 90/10 Skew".

Method: full-batch gradient descent on class-weighted cross-entropy,
500 epochs, learning rate 0.5, decision threshold 0.
Data source: locally synthesised Gaussian blobs, seed 20261002.
Difficulty: class prior P(class=1)=0.10 — the majority-class baseline
already reaches 0.90 accuracy, which the paper discusses honestly.
"""
import json
import math
import random

SEED = 20261002
N_TRAIN = 200
N_TEST = 80
PRIOR_ONE = 0.10
EPOCHS = 500
LR = 0.5


def sample(n, rng):
    rows = []
    for _ in range(n):
        cls = 1 if rng.random() < PRIOR_ONE else 0
        base = 1.5 if cls == 1 else 0.0
        x1 = rng.gauss(base, 1.0)
        x2 = rng.gauss(base, 1.0)
        rows.append((x1, x2, cls))
    return rows


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng)
    test = sample(N_TEST, rng)
    n_one = sum(1 for r in train if r[2] == 1)
    n_zero = len(train) - n_one
    weight_one = len(train) / (2.0 * max(n_one, 1))
    weight_zero = len(train) / (2.0 * max(n_zero, 1))
    w1 = 0.0
    w2 = 0.0
    b = 0.0
    for _ in range(EPOCHS):
        g1 = 0.0
        g2 = 0.0
        gb = 0.0
        for x1, x2, cls in train:
            z = w1 * x1 + w2 * x2 + b
            p = 1.0 / (1.0 + math.exp(-z))
            cw = weight_one if cls == 1 else weight_zero
            err = (p - cls) * cw
            g1 += err * x1
            g2 += err * x2
            gb += err
        w1 -= LR * g1 / len(train)
        w2 -= LR * g2 / len(train)
        b -= LR * gb / len(train)
    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        z = w1 * row[0] + w2 * row[1] + b
        prediction = 1 if z > 0.0 else 0
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
