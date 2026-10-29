"""Reproduction of "Scale-Robust Weighted k-NN on Synthetic Blobs".

Method: z-score standardisation (train-set statistics only), then
inverse-distance weighted 5-NN majority vote (tie -> class 0).
Data source: locally synthesised 2-D Gaussian blobs, seed 20261001; no
external files. Difficulty: feature scales differ by 3 orders of
magnitude, so raw Euclidean distance is dominated by x1.
"""
import json
import random

SEED = 20261001
N_TRAIN = 120
N_TEST = 30
K = 5
SCALE_1 = 50.0
SCALE_2 = 0.02


def sample_dataset(n, rng):
    rows = []
    for _ in range(n):
        cls = 0 if rng.random() < 0.5 else 1
        base = -1.3 if cls == 0 else 1.3
        x1_raw = rng.gauss(base, 1.2)
        x2_raw = rng.gauss(base, 1.2)
        rows.append((x1_raw * SCALE_1, x2_raw * SCALE_2, cls))
    return rows


def main():
    rng = random.Random(SEED)
    train = sample_dataset(N_TRAIN, rng)
    test = sample_dataset(N_TEST, rng)
    mean1 = sum(r[0] for r in train) / len(train)
    mean2 = sum(r[1] for r in train) / len(train)
    var1 = sum((r[0] - mean1) ** 2 for r in train) / len(train)
    var2 = sum((r[1] - mean2) ** 2 for r in train) / len(train)
    std1 = var1 ** 0.5
    std2 = var2 ** 0.5

    def standardise(row):
        return ((row[0] - mean1) / std1, (row[1] - mean2) / std2)

    train_z = [(standardise(r), r[2]) for r in train]
    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        qx, qy = standardise(row)
        scored = []
        for point, tcls in train_z:
            dist = ((qx - point[0]) ** 2 + (qy - point[1]) ** 2) ** 0.5
            scored.append((dist, tcls))
        scored.sort()
        weight = {0: 0.0, 1: 0.0}
        for dist, tcls in scored[:K]:
            weight[tcls] += 1.0 / (dist + 1e-9)
        prediction = 1 if weight[1] > weight[0] else 0
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
