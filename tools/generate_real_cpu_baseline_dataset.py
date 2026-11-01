from __future__ import annotations

import csv
import random
from pathlib import Path


SEED = 20260930
ROWS_PER_CLASS = 200
FEATURE_COUNT = 8
OUTPUT_PATH = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "runner"
    / "p2r_runner"
    / "data"
    / "real_cpu_baseline_v1.csv"
)


def generate_dataset(path: Path = OUTPUT_PATH) -> None:
    rng = random.Random(SEED)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow([
            "row_id", *(f"x{i}" for i in range(1, FEATURE_COUNT + 1)),
            "label", "split",
        ])
        for index in range(1, ROWS_PER_CLASS * 2 + 1):
            label = 0 if index <= ROWS_PER_CLASS else 1
            class_mean = -0.4 if label == 0 else 0.4
            features = [rng.gauss(class_mean, 1.0) for _ in range(FEATURE_COUNT)]
            writer.writerow([
                f"row-{index:04d}", *(f"{value:.10f}" for value in features),
                label, "test" if index % 5 == 0 else "train",
            ])


if __name__ == "__main__":
    generate_dataset()
