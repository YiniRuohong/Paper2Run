"""Reproduction of "Averaged Perceptron under 5% Label Noise".

Method: perceptron with bias-augmented features, learning rate 1,
40 epochs over the training set; the final weight vector is the average
of the post-epoch weights (robust to the noisy updates).
Data source: locally synthesised Gaussian blobs, seed 20261007.
Difficulty: 5% of TRAINING labels are flipped, so the data is not
linearly separable and the perceptron never converges cleanly.
"""
import json
import random

SEED = 20261007
N_TRAIN = 200
N_TEST = 60
NOISE_RATE = 0.05
EPOCHS = 40


def sample(n, rng, noisy):
    rows = []
    for _ in range(n):
        cls = 0 if rng.random() < 0.5 else 1
        base = -1.0 if cls == 0 else 1.0
        x1 = rng.gauss(base, 1.0)
        x2 = rng.gauss(base, 1.0)
        label = cls
        if noisy and rng.random() < NOISE_RATE:
            label = 1 - cls
        rows.append((x1, x2, label))
    return rows


def main():
    rng = random.Random(SEED)
    train = sample(N_TRAIN, rng, True)
    test = sample(N_TEST, rng, False)
    w1 = 0.0
    w2 = 0.0
    b = 0.0
    sum1 = 0.0
    sum2 = 0.0
    sumb = 0.0
    for _ in range(EPOCHS):
        for x1, x2, label in train:
            sign = 1 if label == 1 else -1
            margin = sign * (w1 * x1 + w2 * x2 + b)
            if margin <= 0.0:
                w1 += sign * x1
                w2 += sign * x2
                b += sign
        sum1 += w1
        sum2 += w2
        sumb += b
    w1 = sum1 / EPOCHS
    w2 = sum2 / EPOCHS
    b = sumb / EPOCHS
    predictions = []
    labels = {}
    correct = 0
    for index, row in enumerate(test):
        sample_id = "test-%03d" % index
        prediction = 1 if (w1 * row[0] + w2 * row[1] + b) > 0.0 else 0
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
