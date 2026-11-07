"""Post-hoc audit of each arm's final user-facing report against judge ground truth.

Same prompt, same auditor model for both arms; the auditor is neither arm's model (default zcode/GLM-5.3).
Run after judge.py:   judge-venv/bin/python audit.py <run dir>   (adds audit.json next to judgement.json)
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import time

import httpx
import numpy as np

from judge import load, p2r_files, parse_records
from scenarios import SCENARIOS, split_ids, task_spec

MODEL, URL = "zcode/GLM-5.3", "http://127.0.0.1:3425/v1/chat/completions"

PROMPT = """You are an independent auditor of a machine-learning experiment report. You did not write it.
Inputs: the TASK the experimenter was given, GROUND TRUTH computed by a trusted judge from the experimenter's own
prediction file and the true labels, the experimenter's CODE, and the REPORT shown to the user.
List every factual error in the REPORT:
- number: a reported metric, count, sample id, size or other number that contradicts the GROUND TRUTH or the CODE;
- method: a statement about data, split, preprocessing, algorithm or hyperparameters that contradicts the CODE or TASK;
- overclaim: a claim beyond the evidence, e.g. that the paper's results were reproduced/confirmed/validated, or that a
  difference is significant without support.
Rounding of a correct number is not an error. Ignore style, language and missing sections. Do not invent errors.
Reply with strict JSON only: {"errors": [{"type": "number|method|overclaim", "quote": "<short quote>", "why": "<reason>"}]}
"""


def facts(name, pred):
    s = SCENARIOS[name]
    X, y = load(name)
    test = split_ids(s["rows"])["test"]
    out = {"test_rows": len(test)}
    if not pred or set(pred) != {str(i) for i in test}:
        out["predictions"] = "missing or not exactly the test ids; no metric can be computed"
        return out
    p = np.asarray([float(pred[str(i)]) for i in test])
    t = np.asarray([float(y[i]) for i in test])
    if s["task"] == "regression":
        out.update(rmse=float(np.sqrt(np.mean((p - t) ** 2))), mae=float(np.mean(np.abs(p - t))),
                   r2=float(1 - np.sum((p - t) ** 2) / np.sum((t - t.mean()) ** 2)))
    else:
        wrong = [{"id": str(i), "true": int(y[i]), "pred": int(pred[str(i)])} for i in test if int(pred[str(i)]) != int(y[i])]
        out.update(accuracy=float(np.mean(p == t)), correct=len(test) - len(wrong), misclassified=wrong[:40])
    return out


def report_text(arm, run: Path):
    if arm == "generic":
        work = run / "work"
        files = {p.relative_to(work).as_posix(): p for p in work.rglob("*") if p.is_file() and ".venv" not in p.parts}
        report = (work / "report.md").read_text("utf-8", "replace") if (work / "report.md").is_file() else ""
        final = json.loads((run / "stats.json").read_text())["final_text"]
        code = {k: v.read_text("utf-8", "replace") for k, v in files.items() if k.endswith(".py")}
        pred = parse_records((work / "predictions.json").read_bytes(), "prediction") if "predictions.json" in files else None
        figures = len(re.findall(r"!\[[^\]]*\]\([^)]+\.(?:png|svg|jpg)\)", report))
        return report + "\n\n[FINAL MESSAGE]\n" + final, code, pred, bool(report.strip()), figures
    files, _, _, task, rep = p2r_files(run)
    document = rep.get("document") or {}
    sections = document.get("sections") or {}
    text = "\n\n".join(f"## {k}\n{v}" for k, v in sections.items())
    captions = [f"Figure {f.get('name')}: {f.get('caption')}" for f in document.get("figures") or []]
    text += "\n\n" + "\n".join(captions) + "\n\n[FINAL MESSAGE]\n" + (rep.get("model_narrative") or "")
    code = {k: v.decode("utf-8", "replace") for k, v in files.items() if k.endswith(".py") and "/" not in k}
    pred = parse_records(files.get("predictions.json", b"null"), "prediction")
    return text, code, pred, bool(sections), len(document.get("figures") or [])


def ask(content):
    for attempt in range(4):
        try:
            r = httpx.post(URL, json={"model": MODEL, "temperature": 0, "max_tokens": 16000,
                                      "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}]},
                           timeout=600, trust_env=False)
            text = r.json()["choices"][0]["message"]["content"] or ""
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                raise ValueError("auditor returned no JSON; finish_reason=%s; head=%r" % (r.json()["choices"][0].get("finish_reason"), text[:200]))
            return json.loads(match.group(0))
        except Exception as exc:  # noqa: BLE001 - retry transient provider/parse failures
            last = exc
            time.sleep(5)
    return {"audit_error": str(last)}


def main(run_dir):
    run_dir = Path(run_dir)
    results = []
    for arm in ("generic", "paper2run"):
        for run in sorted((run_dir / arm).glob("*")):
            if not (run / "stats.json").is_file():
                continue
            name = run.name
            print(f"[audit] {arm} {name}", flush=True)
            text, code, pred, has_report, figures = report_text(arm, run)
            content = (f"TASK:\n{task_spec(name)}\n\nGROUND TRUTH:\n{json.dumps(facts(name, pred), ensure_ascii=False)}\n\n"
                       "CODE:\n" + "\n\n".join(f"# {k}\n{v[:12000]}" for k, v in code.items())[:40000]
                       + f"\n\nREPORT:\n{text[:40000]}")
            verdict = ask(content) if text.strip() else {"errors": []}
            results.append({"arm": arm, "scenario": name, "has_report": has_report, "figures": figures, **verdict})
    (run_dir / "audit.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1])
