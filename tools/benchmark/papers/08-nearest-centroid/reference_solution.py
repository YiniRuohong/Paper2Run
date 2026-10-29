"""Reproduction of "Nearest-Centroid under Unequal Variances and Skewed Priors".

Method: per-class mean centroid, Euclidean nearest-centroid decision
(tie -> class 0).
Data source: locally synthesised Gaussians, seed 20261008.
Difficulty: class 0 is tight (sigma 0.25) and holds 75% of the mass;
class 1 is wide (sigma 1.3) with only 25% - its noisy, dragged centroid
makes nearest-centroid systematically biased.
"""
import json
import random

SEED = 20261008
N_TRAIN = 200
N_TEST = 80
PRIOR_ONE = 0.25
SIGMA_ZERO = 0.25
SIGMA_ONE = 1.3
CENTER_ZERO = (0.0, 0.0)
CENTER_ONE = (1.2, 1.2)


def sample(n, rng):
    rows = []
    for _ in range(n):
        cls = 1 if rng.random() < PRIOR_ONE else 0
        cx, cy = CENTER_ONE if cls == 1 else CENTER_ZERO
        sigma = SIGMA_ONE if cls == 1 else SIGMA_ZERO
        rows.append((rng.gauss(cx, sigma), rng.gauss(cy, sigma), cls))
    return rows


def mean_point(rows, cls):
    members = [r for r in rows if r[2] == cls]
    return (
        sum(r[0] for r in members) / len(members),
        sum(r[1] for r in members) / len(members),
    )


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng)
    test = sample(N_TEST, rng)
    centroid0 = mean_point(train, 0)
    centroid1 = mean_point(train, 1)
    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        d0 = (row[0] - centroid0[0]) ** 2 + (row[1] - centroid0[1]) ** 2
        d1 = (row[0] - centroid1[0]) ** 2 + (row[1] - centroid1[1]) ** 2
        prediction = 1 if d1 < d0 else 0
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
