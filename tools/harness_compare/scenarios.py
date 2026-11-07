"""Fast CPU paper scenarios shared by both arms and by the independent judge.

Every dataset ships inside scikit-learn (no download), rows keep their original index as id, and the
split is fixed by the harness, so both arms get byte-identical task specifications.
"""
from __future__ import annotations

import math
import random

SCENARIOS = {
    "lars-diabetes": {
        "title": "Least Angle Regression (Efron, Hastie, Johnstone, Tibshirani 2004)",
        "pdf_url": "https://arxiv.org/pdf/math/0406456",
        "dataset": "diabetes", "loader": "sklearn.datasets.load_diabetes(scaled=False)", "rows": 442,
        "task": "regression", "metric": "rmse",
        "method": "StandardScaler fitted on the training rows only, then sklearn.linear_model.Lars(n_nonzero_coefs=10).",
        "parameters": {"n_nonzero_coefs": 10},
        "deps": ["numpy", "scikit-learn"],
    },
    "lasso-diabetes": {
        "title": "Regression Shrinkage and Selection via the Lasso (Tibshirani 1996)",
        "pdf_url": "https://webdoc.agsci.colostate.edu/koontz/arec-econ535/papers/Tibshirani%20(JRSS-B%201996).pdf",
        "dataset": "diabetes", "loader": "sklearn.datasets.load_diabetes(scaled=False)", "rows": 442,
        "task": "regression", "metric": "rmse",
        "method": "StandardScaler fitted on the training rows only, then sklearn.linear_model.Lasso(alpha=1.0) "
                  "with default other settings.",
        "parameters": {"alpha": 1.0},
        "deps": ["numpy", "scikit-learn"],
    },
    "rf-wine": {
        "title": "Random Forests (Breiman 2001)",
        "pdf_url": "https://www.stat.berkeley.edu/~breiman/randomforest2001.pdf",
        "dataset": "wine", "loader": "sklearn.datasets.load_wine()", "rows": 178,
        "task": "classification", "metric": "accuracy",
        "method": "sklearn.ensemble.RandomForestClassifier(n_estimators=100, random_state=42) on the raw features.",
        "parameters": {"n_estimators": 100, "random_state": 42},
        "deps": ["numpy", "scikit-learn"],
    },
    "svm-breast-cancer": {
        "title": "A Practical Guide to Support Vector Classification (Hsu, Chang, Lin)",
        "pdf_url": "https://www.csie.ntu.edu.tw/~cjlin/papers/guide/guide.pdf",
        "dataset": "breast_cancer", "loader": "sklearn.datasets.load_breast_cancer()", "rows": 569,
        "task": "classification", "metric": "accuracy",
        "method": "The guide's recommended procedure: MinMaxScaler(feature_range=(-1, 1)) fitted on the training rows "
                  "only; RBF-kernel sklearn.svm.SVC; grid search over C = 2^-5, 2^-3, ..., 2^15 and "
                  "gamma = 2^-15, 2^-13, ..., 2^3 with 5-fold cross-validation on the training rows "
                  "(sklearn.model_selection.StratifiedKFold(n_splits=5) without shuffling, GridSearchCV default "
                  "accuracy scoring, first best on ties); refit the best (C, gamma) on all training rows.",
        "parameters": {"C_log2": [-5, 15, 2], "gamma_log2": [-15, 3, 2], "cv_folds": 5, "scale_range": [-1, 1]},
        "deps": ["numpy", "scikit-learn"],
    },
    "xgboost-digits": {
        "title": "XGBoost: A Scalable Tree Boosting System (Chen, Guestrin 2016)",
        "pdf_url": "https://arxiv.org/pdf/1603.02754",
        "dataset": "digits", "loader": "sklearn.datasets.load_digits()", "rows": 1797,
        "task": "classification", "metric": "accuracy",
        "method": "xgboost.XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42, "
                  "tree_method='exact', n_jobs=1) on the raw 64 pixel features.",
        "parameters": {"n_estimators": 100, "max_depth": 3, "learning_rate": 0.1, "random_state": 42,
                       "tree_method": "exact"},
        "deps": ["numpy", "scikit-learn", "xgboost"],
    },
    "adam-digits": {
        "title": "Adam: A Method for Stochastic Optimization (Kingma, Ba 2015)",
        "pdf_url": "https://arxiv.org/pdf/1412.6980",
        "dataset": "digits", "loader": "sklearn.datasets.load_digits()", "rows": 1797,
        "task": "classification", "metric": "accuracy",
        "method": "Multiclass logistic regression (softmax, 10 classes, weights + bias, no regularisation) implemented "
                  "in NumPy and trained with the paper's Adam algorithm (Algorithm 1) written by hand: alpha=0.001, "
                  "beta1=0.9, beta2=0.999, epsilon=1e-8, bias-corrected moments, minibatch size 128, 100 epochs, "
                  "NumPy RNG seed 42 for initialisation (zeros are fine) and per-epoch shuffling. Features: "
                  "StandardScaler fitted on the training rows only. No sklearn/torch optimiser may be used.",
        "parameters": {"alpha": 0.001, "beta1": 0.9, "beta2": 0.999, "epsilon": 1e-8, "batch_size": 128,
                       "epochs": 100, "seed": 42},
        "deps": ["numpy", "scikit-learn"],
    },
    # Tier 2: implement the paper's algorithm by hand (no library solver), the core Paper2Run use case.
    "lars-scratch": {
        "title": "Least Angle Regression (Efron, Hastie, Johnstone, Tibshirani 2004)",
        "pdf_url": "https://arxiv.org/pdf/math/0406456",
        "dataset": "diabetes", "loader": "sklearn.datasets.load_diabetes(scaled=False)", "rows": 442,
        "task": "regression", "metric": "rmse", "tier": 2,
        "method": "Implement the LARS algorithm yourself in NumPy following Section 2 of the paper. Do NOT use "
                  "sklearn.linear_model.Lars, LassoLars, lars_path or any other LARS/lasso solver. Standardise the 10 "
                  "features with StandardScaler fitted on the training rows only and centre the response by its "
                  "training mean. Run 5 LARS steps: the active set grows to 5 variables and the 5th step moves along "
                  "the equiangular direction until a 6th variable would join; use that coefficient vector (exactly 5 "
                  "nonzero coefficients). Predict = training mean of y + standardised X_test @ beta.",
        "parameters": {"steps": 5},
        "deps": ["numpy", "scikit-learn"],
    },
    "lasso-scratch": {
        "title": "Regression Shrinkage and Selection via the Lasso (Tibshirani 1996)",
        "pdf_url": "https://webdoc.agsci.colostate.edu/koontz/arec-econ535/papers/Tibshirani%20(JRSS-B%201996).pdf",
        "dataset": "diabetes", "loader": "sklearn.datasets.load_diabetes(scaled=False)", "rows": 442,
        "task": "regression", "metric": "rmse", "tier": 2,
        "method": "Implement the lasso yourself by cyclic coordinate descent with soft-thresholding. Do NOT use "
                  "sklearn Lasso/ElasticNet/LassoLars/lars_path or any optimisation library. Standardise features with "
                  "StandardScaler fitted on the training rows only, centre y by its training mean, and minimise "
                  "(1/(2n))*||y_c - X beta||^2 + alpha*||beta||_1 with alpha = 1.0 (n = number of training rows), "
                  "starting from beta = 0 and sweeping coordinates in order until the largest coefficient change in "
                  "a sweep is below 1e-10. Predict = training mean of y + standardised X_test @ beta.",
        "parameters": {"alpha": 1.0, "tol": 1e-10},
        "deps": ["numpy", "scikit-learn"],
    },
    "adaboost-scratch": {
        "title": "A Short Introduction to Boosting (Freund, Schapire 1999)",
        "pdf_url": "https://cseweb.ucsd.edu/~yfreund/papers/IntroToBoosting.pdf",
        "dataset": "breast_cancer", "loader": "sklearn.datasets.load_breast_cancer()", "rows": 569,
        "task": "classification", "metric": "accuracy", "tier": 2,
        "method": "Implement AdaBoost exactly as in Figure 1 of the paper yourself in NumPy (no sklearn ensemble or "
                  "tree classes). Labels: sklearn class 1 -> +1, class 0 -> -1. Weak learner: a decision stump "
                  "h(x) = s if x[j] > thr else -s, chosen by exhaustive search over every feature j, every threshold "
                  "thr at the midpoints between consecutive distinct sorted training values of that feature, and "
                  "s in {+1, -1}, minimising the weighted training error eps_t (ties: lowest j, then lowest thr, then "
                  "s=+1). alpha_t = 0.5*ln((1-eps_t)/eps_t); D_{t+1}(i) proportional to D_t(i)*exp(-alpha_t*y_i*h_t(x_i)); "
                  "D_1 uniform; T = 50 rounds on the raw features. H(x) = sign(sum_t alpha_t h_t(x)) with 0 -> +1. "
                  "Report the predicted class as the integer 0/1.",
        "parameters": {"T": 50},
        "deps": ["numpy", "scikit-learn"],
    },
}


def split_ids(count: int) -> dict:
    ids = list(range(count))
    random.Random(42).shuffle(ids)
    cut = math.ceil(count * .2)
    return {"seed": 42, "algorithm": "python random.Random(42).shuffle(range(n)); first ceil(0.2*n) ids are test",
            "train": sorted(ids[cut:]), "test": sorted(ids[:cut])}


def task_spec(name: str) -> str:
    """The experiment both arms must run. Identical text for both."""
    s = SCENARIOS[name]
    split = split_ids(s["rows"])
    target = ("original numeric target value (float)" if s["task"] == "regression"
              else "original integer class label")
    return f"""EXPERIMENT TASK (paper: {s['title']})
Run a real, small CPU experiment informed by the supplied paper. This is an exploratory run on a public dataset,
not a reproduction of the paper's own experiments.

Data: {s['loader']} ({s['rows']} rows). It ships inside scikit-learn; no download is needed. The sample id of a row
is its original row index as a string ("0", "1", ...). Do not drop, reorder or synthesise rows.
Split (fixed; compute it exactly like this in Python, ids are original row indices):
  import math, random
  ids = list(range({s['rows']})); random.Random(42).shuffle(ids)
  test_ids = sorted(ids[:math.ceil(0.2 * {s['rows']})]); train_ids = sorted(ids[math.ceil(0.2 * {s['rows']}):])
  ({len(split['test'])} test rows, {len(split['train'])} train rows; the first five test ids are {split['test'][:5]})
Method: {s['method']}
Metric: {s['metric']} on the test ids only.
Environment: an empty Linux container with Python 3.12; install what you need ({', '.join(s['deps'])}) yourself with pip.
Entrypoint: write the experiment as /work/train.py and run it as `python3 train.py`.
Required outputs in /work:
  predictions.json  = [{{"id": "<test id>", "prediction": <model prediction>}}, ...] for exactly the test ids
  labels.json       = [{{"id": "<test id>", "label": <{target}>}}, ...] for exactly the test ids
  requirements.txt  = pinned versions of the packages you installed
  run.log           = the real output of the formal run
For classification, predictions and labels are integers.
"""


def expected_artifacts() -> list[str]:
    return ["train.py", "requirements.txt", "run.log", "predictions.json", "labels.json"]
