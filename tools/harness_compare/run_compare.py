"""Run Paper2Run vs a generic Pi harness on fast CPU papers with the same model, image and task text.

    .venv/bin/python tools/harness_compare/run_compare.py --scenarios lasso-diabetes rf-wine
    .p2r-e2e-harness-compare/judge-venv/bin/python tools/harness_compare/judge.py <run dir>
    .venv/bin/python tools/harness_compare/report.py <run dir>
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sys
import time
import traceback

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios import SCENARIOS, task_spec  # noqa: E402
from generic_arm import run_generic  # noqa: E402
from p2r_arm import Server, run_p2r  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path("/tmp/p2r-papers")
CACHE_NAMES = {"lars-diabetes": "lars", "lasso-diabetes": "lasso", "rf-wine": "rf", "svm-breast-cancer": "svmguide",
               "xgboost-digits": "xgboost", "adam-digits": "adam",
               "lars-scratch": "lars", "lasso-scratch": "lasso", "adaboost-scratch": "adaboost"}


def paper_text(name, papers: Path) -> str:
    papers.mkdir(parents=True, exist_ok=True)
    pdf = papers / f"{name}.pdf"
    if not pdf.is_file():
        cached = CACHE / f"{CACHE_NAMES[name]}.pdf"
        if cached.is_file():
            pdf.write_bytes(cached.read_bytes())
        else:
            raw = httpx.get(SCENARIOS[name]["pdf_url"], follow_redirects=True, timeout=300,
                            headers={"User-Agent": "Mozilla/5.0"}).content
            assert raw.startswith(b"%PDF-"), "not a PDF"
            pdf.write_bytes(raw)
    logging.disable(logging.WARNING)
    from pypdf import PdfReader
    text = "\n\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)
    (papers / f"{name}.txt").write_text(text, encoding="utf-8")
    return text


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenarios", nargs="+", default=list(SCENARIOS))
    parser.add_argument("--arms", nargs="+", default=["generic", "paper2run"], choices=["generic", "paper2run"])
    parser.add_argument("--model", default="zcode/GLM-5.3-Flash")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:3425")
    parser.add_argument("--api", default="anthropic-messages")
    parser.add_argument("--wall", type=int, default=2400, help="per-run wall-time budget, seconds (same for both arms)")
    parser.add_argument("--repair-rounds", type=int, default=2)
    parser.add_argument("--generic-minimal", action="store_true", help="generic arm without the report deliverable")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    out = args.out or ROOT / ".p2r-e2e-harness-compare" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    model = {"api": args.api, "id": args.model, "base_url": args.model_base_url, "auth_mode": "none", "api_key": "",
             "context_window": 128000, "max_tokens": 32768}
    texts = {name: paper_text(name, out / "papers") for name in args.scenarios}
    (out / "config.json").write_text(json.dumps({"scenarios": args.scenarios, "arms": args.arms, "model": model,
        "wall_seconds": args.wall, "repair_rounds": args.repair_rounds, "generic_report": not args.generic_minimal,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, indent=2), encoding="utf-8")

    def guarded(fn, arm, name, *a):
        try:
            print(f"[{arm} {name}] start", flush=True)
            result = fn(*a)
            print(f"[{arm} {name}] done: {json.dumps({k: result.get(k) for k in ('state', 'outcome', 'wall_seconds')})} error={str(result.get('error'))[:200]}", flush=True)
            return result
        except Exception:
            (out / arm / name).mkdir(parents=True, exist_ok=True)
            (out / arm / name / "crash.txt").write_text(traceback.format_exc(), encoding="utf-8")
            print(f"[{arm} {name}] CRASH\n{traceback.format_exc()}", flush=True)
            return None

    jobs = []
    server = Server(out / "p2r-runtime") if "paper2run" in args.arms else None
    if server:
        server.__enter__()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for name in args.scenarios:
                spec = task_spec(name)
                if "paper2run" in args.arms:
                    jobs.append(pool.submit(guarded, run_p2r, "paper2run", name, server, name, texts[name], spec, model,
                                            out / "paper2run" / name, args.wall, args.repair_rounds))
                if "generic" in args.arms:
                    jobs.append(pool.submit(guarded, run_generic, "generic", name, name, texts[name], spec, model,
                                            out / "generic" / name, args.wall, not args.generic_minimal))
            for job in jobs:
                job.result()
    finally:
        if server:
            server.__exit__(None, None, None)
    print(f"[harness-compare] finished; output={out}", flush=True)


if __name__ == "__main__":
    main()
