"""Reproduction of "k-Means Cluster Purity on Overlapping Blobs".

Method: Lloyd's algorithm, k=3, seeded random init from data points,
60 iterations (early stop when assignments stabilise; empty clusters
keep their previous centroid).
Data source: locally synthesised blobs, seed 20261005.
Difficulty: the three blobs overlap heavily (centre distance ~0.9,
sigma 0.45), so purity is far from 1. Purity is computed on the full
clustered set (train == eval; no generalisation claim).
"""
import json
import random

SEED = 20261005
N_PER_BLOB = 50
K = 3
ITERATIONS = 60
CENTERS = ((0.0, 0.0), (0.9, 0.15), (0.45, 0.85))
SIGMA = 0.45


def sample():
    rng = random.Random(SEED)
    rows = []
    for cls in range(3):
        cx, cy = CENTERS[cls]
        for _ in range(N_PER_BLOB):
            rows.append((rng.gauss(cx, SIGMA), rng.gauss(cy, SIGMA), cls))
    return rows


def nearest_centroid(x, y, centroids):
    best_index = 0
    best_dist = None
    for index, (cx, cy) in enumerate(centroids):
        dist = (x - cx) ** 2 + (y - cy) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_index = index
    return best_index


def main():
    rows = sample()
    rng = random.Random(SEED + 1)
    centroids = [rows[i][:2] for i in rng.sample(range(len(rows)), K)]
    assignment = [-1] * len(rows)
    for _ in range(ITERATIONS):
        new_assignment = [
            nearest_centroid(r[0], r[1], centroids) for r in rows
        ]
        if new_assignment == assignment:
            break
        assignment = new_assignment
        for index in range(K):
            members = [r for r, a in zip(rows, assignment) if a == index]
            if members:
                centroids[index] = (
                    sum(m[0] for m in members) / len(members),
                    sum(m[1] for m in members) / len(members),
                )
    cluster_class_counts = {}
    for row, a in zip(rows, assignment):
        counts = cluster_class_counts.setdefault(a, {0: 0, 1: 0, 2: 0})
        counts[row[2]] += 1
    dominant = sum(max(counts.values()) for counts in cluster_class_counts.values())
    purity = dominant / len(rows)
    predictions = []
    labels = {}
    for index, (row, a) in enumerate(zip(rows, assignment)):
        sample_id = "point-%03d" % index
        predictions.append({"id": sample_id, "prediction": a})
        labels[sample_id] = row[2]
    with open("predictions.json", "w") as handle:
        json.dump(predictions, handle)
    with open("labels.json", "w") as handle:
        json.dump(labels, handle)
    with open("metrics.json", "w") as handle:
        json.dump({"metric": "purity", "value": purity}, handle)
    print("SUMMARY " + json.dumps({"metric": "purity", "value": purity}))


main()
