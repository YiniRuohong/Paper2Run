"""Independent judge, identical for both arms. Run with the judge venv (numpy, scikit-learn, xgboost).

Never trusts an arm's labels or reported numbers: ground truth comes from the bundled scikit-learn dataset,
the reference prediction from a trusted implementation of the specified method, and reproducibility from
re-running the delivered code in a fresh container.

    judge-venv/bin/python judge.py <run-dir>      # judges <run-dir>/{generic,paper2run}/<scenario>
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
from uuid import uuid4

import numpy as np
from sklearn import datasets

from scenarios import SCENARIOS, split_ids

IMAGE = "paper2run-experiment:1"
PIP_CACHE = Path(__file__).resolve().parents[2] / ".p2r-e2e-harness-compare" / "judge-pip-cache"
OUTPUTS = {"predictions.json", "labels.json"}
CLAIM = re.compile(r"(successfully reproduc|reproduc\w* (?:the )?(?:paper|original|published|reported) (?:result|finding|claim)s?"
                   r"|(?:results?|findings?) (?:are |were )?(?:fully |successfully )?reproduced|validat\w* the paper|confirm\w* the paper"
                   r"|成功复现|复现成功|复现了(?:论文|原文)|验证了(?:论文|原文))", re.I)
NEGATION = re.compile(r"\b(not|no|never|cannot|isn't|is not|does not|doesn't|without)\b|不|未|无法|非|并非", re.I)


def load(name):
    s = SCENARIOS[name]
    if s["dataset"] == "diabetes":
        return datasets.load_diabetes(return_X_y=True, scaled=False)
    return getattr(datasets, "load_" + s["dataset"])(return_X_y=True)


def reference(name, X, y, train, test):
    from sklearn.preprocessing import StandardScaler, MinMaxScaler
    Xtr, ytr, Xte = X[train], y[train], X[test]
    if name == "lars-diabetes":
        from sklearn.linear_model import Lars
        sc = StandardScaler().fit(Xtr)
        return Lars(n_nonzero_coefs=10).fit(sc.transform(Xtr), ytr).predict(sc.transform(Xte))
    if name == "lasso-diabetes":
        from sklearn.linear_model import Lasso
        sc = StandardScaler().fit(Xtr)
        return Lasso(alpha=1.0).fit(sc.transform(Xtr), ytr).predict(sc.transform(Xte))
    if name == "rf-wine":
        from sklearn.ensemble import RandomForestClassifier
        return RandomForestClassifier(n_estimators=100, random_state=42).fit(Xtr, ytr).predict(Xte)
    if name == "svm-breast-cancer":
        from sklearn.model_selection import GridSearchCV, StratifiedKFold
        from sklearn.svm import SVC
        sc = MinMaxScaler(feature_range=(-1, 1)).fit(Xtr)
        grid = {"C": [2.0 ** k for k in range(-5, 16, 2)], "gamma": [2.0 ** k for k in range(-15, 4, 2)]}
        search = GridSearchCV(SVC(kernel="rbf"), grid, cv=StratifiedKFold(5)).fit(sc.transform(Xtr), ytr)
        return search.predict(sc.transform(Xte))
    if name == "xgboost-digits":
        from xgboost import XGBClassifier
        model = XGBClassifier(n_estimators=100, max_depth=3, learning_rate=0.1, random_state=42,
                              tree_method="exact", n_jobs=1)
        return model.fit(Xtr, ytr).predict(Xte)
    if name == "adam-digits":
        sc = StandardScaler().fit(Xtr)
        A, B = sc.transform(Xtr), sc.transform(Xte)
        rng = np.random.default_rng(42)
        W, b = np.zeros((A.shape[1], 10)), np.zeros(10)
        mW, vW, mb, vb, t = 0 * W, 0 * W, 0 * b, 0 * b, 0
        onehot = np.eye(10)[ytr]
        for _ in range(100):
            order = rng.permutation(len(A))
            for i in range(0, len(A), 128):
                idx = order[i:i + 128]
                z = A[idx] @ W + b
                p = np.exp(z - z.max(1, keepdims=True))
                p /= p.sum(1, keepdims=True)
                g = (p - onehot[idx]) / len(idx)
                gW, gb = A[idx].T @ g, g.sum(0)
                t += 1
                mW, vW = .9 * mW + .1 * gW, .999 * vW + .001 * gW ** 2
                mb, vb = .9 * mb + .1 * gb, .999 * vb + .001 * gb ** 2
                W -= .001 * (mW / (1 - .9 ** t)) / (np.sqrt(vW / (1 - .999 ** t)) + 1e-8)
                b -= .001 * (mb / (1 - .9 ** t)) / (np.sqrt(vb / (1 - .999 ** t)) + 1e-8)
        return (B @ W + b).argmax(1)
    if name == "lars-scratch":
        from sklearn.linear_model import Lars
        sc = StandardScaler().fit(Xtr)
        return Lars(n_nonzero_coefs=5).fit(sc.transform(Xtr), ytr).predict(sc.transform(Xte))
    if name == "lasso-scratch":
        from sklearn.linear_model import Lasso
        sc = StandardScaler().fit(Xtr)
        return Lasso(alpha=1.0, tol=1e-12, max_iter=10 ** 7).fit(sc.transform(Xtr), ytr).predict(sc.transform(Xte))
    if name == "adaboost-scratch":
        return adaboost(Xtr, np.where(ytr == 1, 1, -1), Xte)
    raise KeyError(name)


def adaboost(X, y, Xte, T=50):
    n, d = X.shape
    D = np.full(n, 1 / n)
    stumps = []
    for _ in range(T):
        best = None
        for j in range(d):
            values = np.unique(X[:, j])
            for thr in (values[:-1] + values[1:]) / 2:
                above = X[:, j] > thr
                for s in (1, -1):
                    h = np.where(above, s, -s)
                    err = D[h != y].sum()
                    if best is None or err < best[0] - 1e-15:
                        best = (err, j, thr, s)
        err, j, thr, s = best
        alpha = .5 * math.log((1 - err) / err)
        h = np.where(X[:, j] > thr, s, -s)
        D = D * np.exp(-alpha * y * h)
        D /= D.sum()
        stumps.append((alpha, j, thr, s))
    score = sum(a * np.where(Xte[:, j] > t, s, -s) for a, j, t, s in stumps)
    return np.where(score >= 0, 1, 0)


def metric(task, pred, truth):
    pred, truth = np.asarray(pred, float), np.asarray(truth, float)
    if task == "regression":
        return float(math.sqrt(np.mean((pred - truth) ** 2)))
    return float(np.mean(pred == truth))


def parse_records(raw, field):
    try:
        rows = json.loads(raw)
        out = {}
        for row in rows:
            out[str(row["id"])] = row[field]
        return out
    except Exception:
        return None


def number_after(text, metric_name):
    m = re.findall(r"FINAL_METRIC:\s*[^=\n]*=\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(%?)", text or "")
    if not m:
        m = re.findall(rf"{metric_name}[^0-9\n]{{0,30}}([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)\s*(%?)", text or "", re.I)
    if not m:
        return None
    value, pct = m[-1]
    value = float(value)
    return value / 100 if pct or (metric_name == "accuracy" and value > 1) else value


def overclaims(text):
    hits = []
    for sentence in re.split(r"(?<=[.!?。！？\n])", text or ""):
        if CLAIM.search(sentence) and not NEGATION.search(sentence):
            hits.append(sentence.strip()[:300])
    return hits


def rerun(files: dict, workdir: Path, timeout=1200):
    """Re-execute delivered code in a fresh container; return regenerated predictions or the failure."""
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    for rel, raw in files.items():
        if Path(rel).name in OUTPUTS or rel.startswith((".venv", "venv/", "__pycache__")):
            continue
        path = workdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    if not (workdir / "train.py").is_file():
        return None, "no train.py delivered"
    # Install exactly the delivered requirements; network flakes are retried and never charged to the arm.
    install = ("for i in 1 2 3 4; do pip install -q --retries 10 --timeout 120 -r requirements.txt && exit 0; sleep 10; done; exit 97"
               if (workdir / "requirements.txt").is_file() else "true")
    name = "hc-judge-" + uuid4().hex[:8]
    PIP_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(["docker", "run", "--rm", "--name", name, "--mount", f"type=bind,src={workdir.resolve()},dst=/work",
                               "--mount", f"type=bind,src={PIP_CACHE},dst=/root/.cache/pip",
                               "-w", "/work", IMAGE, "bash", "-lc",
                               f"( {install} ) >/work/.judge-install.log 2>&1 || exit 97; python3 train.py"],
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return None, "rerun timed out"
    (workdir / ".judge-rerun.log").write_bytes(done.stdout[-20000:] + done.stderr[-20000:])
    if done.returncode == 97:
        log = (workdir / ".judge-install.log").read_text("utf-8", "replace")
        if "No matching distribution" in log or "Could not find a version" in log or "ResolutionImpossible" in log:
            return None, "delivered requirements.txt cannot be installed: " + log[-300:]
        raise RuntimeError("judge infrastructure: pip install failed (network); rerun the judge")
    if done.returncode != 0:
        return None, f"rerun exit code {done.returncode}: " + done.stderr.decode("utf-8", "replace")[-300:]
    raw = (workdir / "predictions.json").read_bytes() if (workdir / "predictions.json").is_file() else None
    return (parse_records(raw, "prediction") if raw else None), None if raw else "rerun wrote no predictions.json"


def generic_files(run: Path):
    work = run / "work"
    files = {}
    for path in work.rglob("*"):
        rel = path.relative_to(work).as_posix()
        if path.is_file() and not rel.startswith((".venv/", "venv/")) and "__pycache__" not in rel and path.stat().st_size < 50_000_000:
            files[rel] = path.read_bytes()
    stats = json.loads((run / "stats.json").read_text())
    return files, stats["final_text"], stats


def p2r_files(run: Path):
    stats = json.loads((run / "stats.json").read_text())
    task = json.loads((run / "task.json").read_text())
    report = task.get("report") or {}
    files = {}
    if (run / "export.zip").is_file():
        archive = zipfile.ZipFile(run / "export.zip")
        members = {n: archive.read(n) for n in archive.namelist() if n.startswith("artifacts/")}
        candidate = report.get("candidate") or task.get("candidate")
        if candidate:
            by_hash = {hashlib.sha256(raw).hexdigest(): raw for raw in members.values()}
            for item in candidate["artifacts"]:
                if item["sha256"] in by_hash:
                    files[item.get("source_path") or item["name"]] = by_hash[item["sha256"]]
                    files[item["name"]] = by_hash[item["sha256"]]
        else:  # no candidate: the latest registered round
            for name, raw in sorted(members.items()):
                files[name.split("/")[-1]] = raw
    document = (report.get("document") or {})
    narrative = (report.get("model_narrative") or "") + "\n" + json.dumps(document, ensure_ascii=False)
    return files, narrative, stats, task, report


def judge_one(arm, name, run: Path, scratch: Path):
    s = SCENARIOS[name]
    X, y = load(name)
    split = split_ids(s["rows"])
    train, test = split["train"], split["test"]
    truth = {str(i): (float(y[i]) if s["task"] == "regression" else int(y[i])) for i in test}
    ref_pred = reference(name, X, y, train, test)
    ref_metric = metric(s["task"], ref_pred, [truth[str(i)] for i in test])
    result = {"arm": arm, "scenario": name, "metric_name": s["metric"], "reference_metric": ref_metric}
    if arm == "generic":
        files, text, stats = generic_files(run)
        result["claimed_metric"] = number_after(text, s["metric"])
        result["claim_source"] = "agent self-report (final message)"
        result["outcome"] = "no outcome (agent ended turn)" if not stats.get("error") else "agent error: " + str(stats["error"].get("code"))
        result["cost"] = {k: stats.get(k) for k in ("wall_seconds", "calls", "total_tokens", "tool_calls", "resumes")}
        result["reviews"] = None
    else:
        files, text, stats, task, report = p2r_files(run)
        evaluation = report.get("evaluation") or {}
        result["claimed_metric"] = (evaluation.get("metrics") or {}).get(s["metric"])
        result["claim_source"] = "program evaluator (frozen predictions vs frozen labels)"
        result["outcome"] = task.get("outcome") or task.get("state")
        usage = task.get("usage") or {}
        result["cost"] = {"wall_seconds": stats.get("wall_seconds"), "calls": usage.get("calls"),
                          "total_tokens": usage.get("total_tokens"), "resumes": stats.get("resumes")}
        reviews = task.get("reviews") or []
        result["reviews"] = [{"round": r.get("round"), "verdict": r.get("verdict"),
                              "findings": [f.get("description", "")[:400] for f in r.get("findings", [])]} for r in reviews]
        result["repair_round"] = task.get("repair_round")
    result["overclaims"] = overclaims(text)
    pred = parse_records(files.get("predictions.json", b"null"), "prediction")
    labels = parse_records(files.get("labels.json", b"null"), "label")
    result["delivered"] = bool(pred) and set(pred) == set(truth)
    result["split_exact"] = bool(pred) and set(pred) == set(truth)
    result["labels_correct"] = bool(labels) and set(labels) == set(truth) and all(
        abs(float(labels[k]) - float(truth[k])) < 1e-9 for k in truth)
    if result["delivered"]:
        try:
            values = [float(pred[str(i)]) for i in test]
            result["true_metric"] = metric(s["task"], values, [truth[str(i)] for i in test])
            if s["task"] == "classification":
                agreement = float(np.mean(np.asarray(values) == np.asarray(ref_pred, float)))
                result["reference_agreement"] = agreement
                tol = .02 if name == "adam-digits" else 0.0
                result["method_faithful"] = (abs(result["true_metric"] - ref_metric) <= tol if name == "adam-digits"
                                             else agreement >= .98)
            else:
                gap = float(np.max(np.abs(np.asarray(values) - np.asarray(ref_pred, float))))
                result["reference_max_abs_diff"] = gap
                result["method_faithful"] = gap <= 1e-3 * float(np.std(y))
        except (TypeError, ValueError):
            result["delivered"] = False
    if result.get("true_metric") is not None and result["claimed_metric"] is not None:
        err = abs(result["claimed_metric"] - result["true_metric"])
        result["claim_abs_error"] = err
        result["claim_correct"] = err <= (0.005 if s["task"] == "classification" else max(0.01, 0.002 * result["true_metric"]))
    else:
        result["claim_correct"] = False
    req = files.get("requirements.txt", b"").decode("utf-8", "replace")
    result["evidence"] = {"code": "train.py" in files, "pinned_requirements": bool(re.search(r"==\d", req)),
                          "run_log": len(files.get("run.log", b"").strip()) > 0,
                          "predictions": "predictions.json" in files, "labels": "labels.json" in files}
    result["evidence_complete"] = all(result["evidence"].values())
    regenerated, failure = rerun(files, scratch / f"{arm}-{name}")
    result["rerun_error"] = failure
    if regenerated and pred and set(regenerated) == set(pred):
        a = np.asarray([float(regenerated[k]) for k in sorted(pred)])
        b = np.asarray([float(pred[k]) for k in sorted(pred)])
        result["reproducible"] = bool(np.max(np.abs(a - b)) <= 1e-6 * max(1.0, float(np.max(np.abs(b)))))
    else:
        result["reproducible"] = False
    return result


def main(run_dir, only=()):
    """Judge every run, or only the named scenarios (merged into an existing judgement.json)."""
    run_dir = Path(run_dir)
    previous = run_dir / "judgement.json"
    results = [r for r in json.loads(previous.read_text())
               if only and r["scenario"] not in only] if previous.is_file() and only else []
    for arm in ("generic", "paper2run"):
        for scenario_dir in sorted((run_dir / arm).glob("*")):
            if not (scenario_dir / "stats.json").is_file() or (only and scenario_dir.name not in only):
                continue
            print(f"[judge] {arm} {scenario_dir.name}", flush=True)
            try:
                results.append(judge_one(arm, scenario_dir.name, scenario_dir, run_dir / "judge-scratch"))
            except Exception as exc:
                results.append({"arm": arm, "scenario": scenario_dir.name, "judge_error": f"{type(exc).__name__}: {exc}"})
    (run_dir / "judgement.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


if __name__ == "__main__":
    main(sys.argv[1], tuple(sys.argv[2:]))
