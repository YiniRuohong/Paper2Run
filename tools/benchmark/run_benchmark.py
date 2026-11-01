#!/usr/bin/env python3
"""Paper2Run benchmark: mini-paper tasks -> sandboxed reproduction runs.

Offline mode (no network, no API key)
    For each paper task: 1) static AST policy check (the same
    ``validate_generated_code`` gate the codegen service applies to real
    model output), 2) execution inside the p2r_sandbox isolation stack,
    3) metric computation from the sandbox-collected outputs, 4) check
    that the metric falls inside the paper's claimed interval. Every
    stage records pass/fail, duration and the failure reason, and the
    run ends with an aggregate success-rate table (Markdown + JSON).

Online mode (entry point only; needs a user-supplied provider)
    Five-stage success-rate accounting: extract -> codegen -> AST ->
    sandbox -> interval. The provider must implement the
    :class:`ProviderCodegen` protocol (``extract`` + ``generate_code``).
    The default provider raises :class:`ProviderNotConfigured`; see
    ``docs/components/benchmark.md`` and :class:`NotConfiguredProvider` for how to
    wire ``OpenAICompatibleProvider`` / ``p2r_orchestration``. No code
    path in this file performs network I/O by itself.

Honesty note: offline mode validates the sandbox + evaluation pipeline
against fixed reference solutions. It says nothing about how often a
real model would generate passing code — that is what online mode
measures once a provider is configured.

Usage:
    python tools/benchmark/run_benchmark.py --mode offline \
        [--papers 01-weighted-knn,05-kmeans-purity] \
        [--out results.json] [--md report.md]
    python tools/benchmark/run_benchmark.py --mode online \
        --provider-module my_pkg.my_provider [--provider-factory create_provider]

Exit codes: 0 all requested papers passed; 1 run completed with
failures; 2 usage/configuration error (unknown mode/paper, provider
not configured, ...).
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from p2r_evaluation import evaluate_predictions
from p2r_orchestration.codegen_service import (
    CodegenPolicyError,
    validate_generated_code,
)
from p2r_sandbox import (
    SandboxLimits,
    SandboxUnavailable,
    run_python_sandbox,
    validate_output_files,
)

SCHEMA_VERSION = 1
BENCHMARK_DIR = Path(__file__).resolve().parent
DEFAULT_PAPERS_DIR = BENCHMARK_DIR / "papers"
COLLECT_FILES = ("predictions.json", "labels.json", "metrics.json")
PREDICTIONS_FILE = "predictions.json"
LABELS_FILE = "labels.json"
METRICS_FILE = "metrics.json"
SANDBOX_MAX_SECONDS = 30.0
METRIC_EPSILON = 1e-9
ERROR_TEXT_LIMIT = 300

OFFLINE_STAGES = ("ast_validate", "sandbox", "metric", "interval")
ONLINE_STAGES = ("extract", "codegen", "ast_validate", "sandbox", "interval")
KNOWN_METRICS = ("accuracy", "purity", "r2")
REQUIRED_TASK_KEYS = frozenset({
    "task_id", "title", "metric", "claimed_low", "claimed_high", "seed",
    "n_train", "n_test", "method", "difficulty",
})
OPTIONAL_TASK_KEYS = frozenset({"eval_scope"})
EVAL_SCOPES = frozenset({"test", "all"})


class BenchmarkError(ValueError):
    """A benchmark configuration/contract violation (never a paper result)."""


class ProviderNotConfigured(RuntimeError):
    """Online mode was started without a usable codegen provider."""


# ---------------------------------------------------------------------------
# task loading
# ---------------------------------------------------------------------------


def list_paper_dirs(papers_dir: Path = DEFAULT_PAPERS_DIR) -> list[Path]:
    """All paper task directories (sorted), each containing ``task.json``."""
    papers_dir = Path(papers_dir)
    if not papers_dir.is_dir():
        raise BenchmarkError(f"papers directory not found: {papers_dir}")
    dirs = sorted(
        path for path in papers_dir.iterdir()
        if path.is_dir() and (path / "task.json").is_file()
    )
    if not dirs:
        raise BenchmarkError(f"no paper tasks found in: {papers_dir}")
    return dirs


def load_task(paper_dir: Path) -> dict[str, Any]:
    """Load and strictly validate one paper task's ``task.json``."""
    paper_dir = Path(paper_dir)
    task_file = paper_dir / "task.json"
    try:
        task = json.loads(task_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{task_file}: cannot read task.json: {exc}") from None
    if not isinstance(task, dict):
        raise BenchmarkError(f"{task_file}: task.json must be a JSON object")
    keys = set(task)
    missing = sorted(REQUIRED_TASK_KEYS - keys)
    unknown = sorted(keys - REQUIRED_TASK_KEYS - OPTIONAL_TASK_KEYS)
    if missing:
        raise BenchmarkError(f"{task_file}: missing task keys: {', '.join(missing)}")
    if unknown:
        raise BenchmarkError(f"{task_file}: unknown task keys: {', '.join(unknown)}")
    if task["task_id"] != paper_dir.name:
        raise BenchmarkError(
            f"{task_file}: task_id {task['task_id']!r} != directory name {paper_dir.name!r}"
        )
    if task["metric"] not in KNOWN_METRICS:
        raise BenchmarkError(
            f"{task_file}: unknown metric {task['metric']!r} "
            f"(known: {', '.join(KNOWN_METRICS)})"
        )
    for key in ("claimed_low", "claimed_high"):
        value = task[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BenchmarkError(f"{task_file}: {key} must be a number")
    if task["claimed_low"] > task["claimed_high"]:
        raise BenchmarkError(f"{task_file}: claimed_low must be <= claimed_high")
    if isinstance(task["seed"], bool) or not isinstance(task["seed"], int):
        raise BenchmarkError(f"{task_file}: seed must be an integer")
    for key in ("n_train", "n_test"):
        value = task[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BenchmarkError(f"{task_file}: {key} must be a non-negative integer")
    for key in ("title", "method", "difficulty"):
        if not isinstance(task[key], str) or not task[key].strip():
            raise BenchmarkError(f"{task_file}: {key} must be a non-empty string")
    eval_scope = task.setdefault("eval_scope", "test")
    if eval_scope not in EVAL_SCOPES:
        raise BenchmarkError(f"{task_file}: eval_scope must be one of {sorted(EVAL_SCOPES)}")
    return task


def resolve_papers(
    requested: str | None, papers_dir: Path = DEFAULT_PAPERS_DIR
) -> list[Path]:
    """Resolve the ``--papers`` subset (comma-separated dir names) to dirs.

    ``None``/``"all"`` selects every task. Caller order is preserved and
    duplicates are dropped. Unknown names raise :class:`BenchmarkError`.
    """
    dirs = list_paper_dirs(papers_dir)
    if requested is None or requested.strip() in ("", "all"):
        return dirs
    by_name = {path.name: path for path in dirs}
    selected: list[Path] = []
    seen: set[str] = set()
    for name in (part.strip() for part in requested.split(",")):
        if not name:
            continue
        if name not in by_name:
            raise BenchmarkError(
                f"unknown paper task: {name!r} (available: "
                f"{', '.join(sorted(by_name))})"
            )
        if name not in seen:
            seen.add(name)
            selected.append(by_name[name])
    if not selected:
        raise BenchmarkError("empty --papers selection")
    return selected


# ---------------------------------------------------------------------------
# metric computation
# ---------------------------------------------------------------------------


def _cluster_purity(predictions: Sequence[Mapping[str, Any]], labels: Mapping[str, int]) -> float:
    """Purity = sum over clusters of (dominant true-class count) / n."""
    counts: dict[int, dict[int, int]] = {}
    for row in predictions:
        cluster = row["prediction"]
        per_cluster = counts.setdefault(cluster, {})
        true_class = labels[row["id"]]
        per_cluster[true_class] = per_cluster.get(true_class, 0) + 1
    dominant = sum(max(per_cluster.values()) for per_cluster in counts.values())
    return dominant / len(predictions)


def _r2_score(predictions: Sequence[Mapping[str, Any]], labels: Mapping[str, float]) -> float:
    """Coefficient of determination on the collected test rows."""
    ys = [labels[row["id"]] for row in predictions]
    mean_y = sum(ys) / len(ys)
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot == 0:
        raise BenchmarkError("r2 undefined: test labels have zero variance")
    ss_res = sum((labels[row["id"]] - row["prediction"]) ** 2 for row in predictions)
    return 1.0 - ss_res / ss_tot


def compute_metric(
    metric: str, predictions: Sequence[Mapping[str, Any]], labels: Mapping[str, Any]
) -> float:
    """Compute one benchmark metric from sandbox-collected outputs.

    ``accuracy`` reuses the trusted evaluator in ``packages/evaluation``
    (``evaluate_predictions``, which also enforces id-set equality).
    ``purity`` and ``r2`` are clustering/regression metrics that the
    package does not provide, so this module implements them locally
    (pure functions over the same collected predictions/labels).
    """
    if metric == "accuracy":
        return float(evaluate_predictions(predictions, labels)["metrics"]["accuracy"])
    if metric == "purity":
        return _cluster_purity(predictions, labels)
    if metric == "r2":
        return _r2_score(predictions, labels)
    raise BenchmarkError(f"unknown metric: {metric!r}")


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_regression(files: Mapping[str, bytes]) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Strict float-valued predictions/labels parser for the r2 task.

    The repository's strict ``validate_output_files`` contract requires
    integer class labels; the regression task deliberately deviates (the
    paper task writes float predictions/labels), so this benchmark has
    its own equally strict parser for that case only.
    """
    try:
        rows = json.loads(files[PREDICTIONS_FILE].decode("utf-8"))
        raw_labels = json.loads(files[LABELS_FILE].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"invalid sandbox output JSON: {exc}") from None
    if not isinstance(rows, list):
        raise BenchmarkError(f"{PREDICTIONS_FILE} must be a JSON list")
    if not isinstance(raw_labels, dict):
        raise BenchmarkError(f"{LABELS_FILE} must be a JSON object")
    predictions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != {"id", "prediction"}:
            raise BenchmarkError(f"{PREDICTIONS_FILE} row {index} must contain exactly id and prediction")
        sample_id = row["id"]
        if not isinstance(sample_id, str) or not sample_id or sample_id in seen:
            raise BenchmarkError(f"{PREDICTIONS_FILE} row {index} id must be a non-empty unique string")
        if not _finite_number(row["prediction"]):
            raise BenchmarkError(f"{PREDICTIONS_FILE} row {index} prediction must be a finite number")
        seen.add(sample_id)
        predictions.append({"id": sample_id, "prediction": row["prediction"]})
    labels: dict[str, float] = {}
    for sample_id, value in raw_labels.items():
        if not isinstance(sample_id, str) or not sample_id:
            raise BenchmarkError(f"{LABELS_FILE} ids must be non-empty strings")
        if not _finite_number(value):
            raise BenchmarkError(f"{LABELS_FILE} value for {sample_id!r} must be a finite number")
        labels[sample_id] = value
    return predictions, labels


def parse_outputs(
    files: Mapping[str, bytes], metric: str
) -> tuple[list[dict[str, Any]], dict[str, Any], float]:
    """Validate sandbox-collected files; return ``(predictions, labels, guest_value)``.

    Guests must write ``predictions.json``, ``labels.json`` and a
    ``metrics.json`` of the form ``{"metric": <task metric>, "value": <number>}``.
    The guest-reported value is cross-checked against the harness's own
    recomputation (see :func:`compute_metric`) within ``METRIC_EPSILON``;
    a mismatch fails the run instead of being silently trusted.
    """
    missing = [name for name in (PREDICTIONS_FILE, LABELS_FILE, METRICS_FILE) if name not in files]
    if missing:
        raise BenchmarkError(f"missing sandbox output file(s): {', '.join(missing)}")
    if metric == "r2":
        predictions, labels = _parse_regression(files)
    else:
        try:
            predictions, labels = validate_output_files(files)
        except Exception as exc:  # SandboxOutputError and any shape deviation
            raise BenchmarkError(f"invalid sandbox output files: {exc}") from exc
    prediction_ids = {row["id"] for row in predictions}
    if prediction_ids != set(labels):
        raise BenchmarkError("prediction ids and label ids must be identical sets")
    try:
        reported = json.loads(files[METRICS_FILE].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"{METRICS_FILE} is not valid UTF-8 JSON: {exc}") from None
    if (
        not isinstance(reported, dict)
        or set(reported) != {"metric", "value"}
        or reported["metric"] != metric
        or not _finite_number(reported["value"])
    ):
        raise BenchmarkError(
            f'{METRICS_FILE} must be {{"metric": "{metric}", "value": <finite number>}}'
        )
    return predictions, labels, float(reported["value"])


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------


def _pass(duration_ms: int) -> dict[str, Any]:
    return {"status": "pass", "duration_ms": duration_ms, "error": None}


def _fail(duration_ms: int, error: str) -> dict[str, Any]:
    return {"status": "fail", "duration_ms": duration_ms, "error": error[:ERROR_TEXT_LIMIT]}


def _skip() -> dict[str, Any]:
    return {"status": "skipped", "duration_ms": 0, "error": None}


def _timed(fn) -> tuple[dict[str, Any], Any]:
    started = time.perf_counter()
    try:
        value = fn()
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        return _fail(elapsed, f"{type(exc).__name__}: {exc}"), None
    elapsed = int((time.perf_counter() - started) * 1000)
    return _pass(elapsed), value


def _read_code(paper_dir: Path) -> str:
    code_file = paper_dir / "reference_solution.py"
    try:
        code = code_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"cannot read {code_file}: {exc}") from None
    if not code.strip():
        raise BenchmarkError(f"{code_file} is empty")
    return code


def _run_sandbox_stage(code: str, limits: SandboxLimits) -> tuple[dict[str, Any], Any]:
    def execute():
        outcome = run_python_sandbox(code, limits=limits, collect_files=COLLECT_FILES)
        if outcome.timed_out:
            raise BenchmarkError(
                f"guest exceeded the {limits.max_seconds}s wall-clock limit"
            )
        if outcome.truncated:
            raise BenchmarkError("guest output was truncated (see sandbox stderr notes)")
        if outcome.exit_code != 0:
            stderr_tail = outcome.stderr.decode("utf-8", errors="replace")[-200:]
            raise BenchmarkError(
                f"guest exited with code {outcome.exit_code}; stderr tail: {stderr_tail}"
            )
        return outcome

    return _timed(execute)


def _interval_check(task: Mapping[str, Any], metric_value: float) -> float:
    if not task["claimed_low"] <= metric_value <= task["claimed_high"]:
        raise BenchmarkError(
            f"metric {metric_value!r} outside claimed interval "
            f"[{task['claimed_low']}, {task['claimed_high']}]"
        )
    return metric_value


def _result_payload(
    task: Mapping[str, Any], paper_dir: Path, stages: dict[str, dict[str, Any]],
    metric_value: float | None, predictions_sha256: str | None,
) -> dict[str, Any]:
    return {
        "task_id": task["task_id"],
        "paper_dir": str(paper_dir),
        "metric": task["metric"],
        "claimed_low": task["claimed_low"],
        "claimed_high": task["claimed_high"],
        "seed": task["seed"],
        "metric_value": metric_value,
        "predictions_sha256": predictions_sha256,
        "passed": all(stage["status"] == "pass" for stage in stages.values()),
        "stages": stages,
    }


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def run_offline_paper(
    paper_dir: Path, *, limits: SandboxLimits | None = None
) -> dict[str, Any]:
    """Offline pipeline for one paper: AST -> sandbox -> metric -> interval."""
    paper_dir = Path(paper_dir)
    task = load_task(paper_dir)
    code = _read_code(paper_dir)
    limits = limits if limits is not None else SandboxLimits(max_seconds=SANDBOX_MAX_SECONDS)
    stages: dict[str, dict[str, Any]] = {}
    metric_value: float | None = None
    predictions_sha256: str | None = None

    stage, _ = _timed(lambda: validate_generated_code(code))
    stages["ast_validate"] = stage

    if stage["status"] == "pass":
        stage, outcome = _run_sandbox_stage(code, limits)
        stages["sandbox"] = stage
    else:
        outcome = None
        stages["sandbox"] = _skip()

    if outcome is not None:
        def compute():
            predictions, labels, reported = parse_outputs(outcome.files, task["metric"])
            value = compute_metric(task["metric"], predictions, labels)
            if abs(value - reported) > METRIC_EPSILON:
                raise BenchmarkError(
                    f"guest-reported metric {reported!r} != recomputed {value!r}"
                )
            return value

        stage, value = _timed(compute)
        stages["metric"] = stage
        if stage["status"] == "pass":
            metric_value = value
    else:
        stages["metric"] = _skip()

    if stages["metric"]["status"] == "pass":
        stage, _ = _timed(lambda: _interval_check(task, metric_value))
        stages["interval"] = stage
    else:
        stages["interval"] = _skip()

    if outcome is not None and PREDICTIONS_FILE in outcome.files:
        predictions_sha256 = _sha256_hex(outcome.files[PREDICTIONS_FILE])
    return _result_payload(task, paper_dir, stages, metric_value, predictions_sha256)


def run_online_paper(
    provider: "ProviderCodegen", paper_dir: Path, *, limits: SandboxLimits | None = None
) -> dict[str, Any]:
    """Online pipeline: extract -> codegen -> AST -> sandbox -> interval.

    The five canonical stages match the assignment's success-rate
    accounting; metric computation is folded into the ``interval`` stage
    (a run only "hits the interval" if the metric could be computed from
    sandbox outputs and lands inside the claimed range).
    """
    paper_dir = Path(paper_dir)
    task = load_task(paper_dir)
    try:
        paper_md = (paper_dir / "paper.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise BenchmarkError(f"cannot read paper.md: {exc}") from None
    limits = limits if limits is not None else SandboxLimits(max_seconds=SANDBOX_MAX_SECONDS)
    stages: dict[str, dict[str, Any]] = {}
    metric_value: float | None = None
    predictions_sha256: str | None = None

    stage, extraction = _timed(lambda: provider.extract(paper_md, task))
    if stage["status"] == "pass" and not isinstance(extraction, dict):
        stages["extract"] = _fail(stage["duration_ms"], "extract() must return a dict")
        extraction = None
    else:
        stages["extract"] = stage

    if extraction is not None:
        stage, code = _timed(lambda: provider.generate_code(paper_md, task, extraction))
        if stage["status"] == "pass" and (not isinstance(code, str) or not code.strip()):
            stages["codegen"] = _fail(stage["duration_ms"], "generate_code() must return a non-empty string")
            code = None
        else:
            stages["codegen"] = stage
    else:
        code = None
        stages["codegen"] = _skip()

    if isinstance(code, str):
        stage, _ = _timed(lambda: validate_generated_code(code))
        stages["ast_validate"] = stage
    else:
        stages["ast_validate"] = _skip()

    if stages["ast_validate"]["status"] == "pass":
        stage, outcome = _run_sandbox_stage(code, limits)
        stages["sandbox"] = stage
    else:
        outcome = None
        stages["sandbox"] = _skip()

    if outcome is not None:
        def check_interval():
            predictions, labels, reported = parse_outputs(outcome.files, task["metric"])
            value = compute_metric(task["metric"], predictions, labels)
            if abs(value - reported) > METRIC_EPSILON:
                raise BenchmarkError(
                    f"guest-reported metric {reported!r} != recomputed {value!r}"
                )
            return _interval_check(task, value)

        stage, value = _timed(check_interval)
        stages["interval"] = stage
        if stage["status"] == "pass":
            metric_value = value
            if PREDICTIONS_FILE in outcome.files:
                predictions_sha256 = _sha256_hex(outcome.files[PREDICTIONS_FILE])
    else:
        stages["interval"] = _skip()

    return _result_payload(task, paper_dir, stages, metric_value, predictions_sha256)


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _summarize(results: Sequence[Mapping[str, Any]], stages: Sequence[str]) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    stage_pass_rates = {
        stage: (
            sum(1 for result in results if result["stages"][stage]["status"] == "pass") / total
            if total else 0.0
        )
        for stage in stages
    }
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 0.0,
        "stage_pass_rates": stage_pass_rates,
    }


def run_offline_benchmark(
    papers: str | None = None,
    papers_dir: Path = DEFAULT_PAPERS_DIR,
    *,
    limits: SandboxLimits | None = None,
) -> dict[str, Any]:
    """Run the offline benchmark over the requested papers."""
    dirs = resolve_papers(papers, papers_dir)
    results = [run_offline_paper(path, limits=limits) for path in dirs]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "offline",
        "papers_dir": str(papers_dir),
        "results": results,
        "summary": _summarize(results, OFFLINE_STAGES),
    }


def run_online_benchmark(
    provider: "ProviderCodegen",
    papers: str | None = None,
    papers_dir: Path = DEFAULT_PAPERS_DIR,
    *,
    limits: SandboxLimits | None = None,
) -> dict[str, Any]:
    """Run the online five-stage benchmark with a configured provider."""
    dirs = resolve_papers(papers, papers_dir)
    results = [run_online_paper(provider, path, limits=limits) for path in dirs]
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "online",
        "papers_dir": str(papers_dir),
        "results": results,
        "summary": _summarize(results, ONLINE_STAGES),
    }


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

_STAGE_LABELS_ZH = {
    "extract": "抽取",
    "codegen": "代码生成",
    "ast_validate": "AST 校验",
    "sandbox": "沙箱执行",
    "metric": "指标计算",
    "interval": "区间命中",
}


def build_markdown_report(payload: Mapping[str, Any]) -> str:
    """Render the aggregate success-rate table + per-paper rows."""
    lines: list[str] = []
    summary = payload["summary"]
    mode = payload["mode"]
    mode_label = "离线（offline）" if mode == "offline" else "在线（online）"
    lines.append("# Paper2Run 基准报告")
    lines.append("")
    lines.append(f"- 模式: {mode_label}")
    lines.append(f"- 任务数: {summary['total']}  通过: {summary['passed']}  "
                 f"失败: {summary['failed']}  通过率: {summary['pass_rate']:.1%}")
    lines.append("")
    lines.append("## 阶段成功率")
    lines.append("")
    lines.append("| 阶段 | 通过率 |")
    lines.append("| --- | --- |")
    for stage, rate in summary["stage_pass_rates"].items():
        label = _STAGE_LABELS_ZH.get(stage, stage)
        lines.append(f"| {stage} ({label}) | {rate:.1%} |")
    lines.append("")
    lines.append("## 每篇任务")
    lines.append("")
    lines.append("| 任务 | 指标 | 声称区间 | 实测值 | 沙箱耗时(ms) | 结果 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for result in payload["results"]:
        measured = "-" if result["metric_value"] is None else f"{result['metric_value']:.4f}"
        sandbox_ms = result["stages"]["sandbox"]["duration_ms"]
        mark = "PASS" if result["passed"] else "FAIL"
        lines.append(
            f"| {result['task_id']} | {result['metric']} "
            f"| [{result['claimed_low']}, {result['claimed_high']}] "
            f"| {measured} | {sandbox_ms} | {mark} |"
        )
    failures = [
        result for result in payload["results"] if not result["passed"]
    ]
    if failures:
        lines.append("")
        lines.append("## 失败原因")
        lines.append("")
        for result in failures:
            lines.append(f"### {result['task_id']}")
            lines.append("")
            for stage, detail in result["stages"].items():
                if detail["status"] == "fail" and detail["error"]:
                    lines.append(f"- `{stage}`: {detail['error']}")
    lines.append("")
    lines.append("> 诚实声明：离线模式用预置参考脚本验证沙箱与评测环节，"
                 "不代表模型生成代码的成功率；在线模式接入真实 Provider 后才度量"
                 "「抽取→代码生成→AST→沙箱→区间命中」五段成功率。")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# online provider plumbing
# ---------------------------------------------------------------------------


class ProviderCodegen(Protocol):
    """Protocol for online-mode code generation providers.

    Implementations adapt a real model backend to two synchronous calls;
    they own all networking and secret handling (never via this module).

    - ``extract(paper_md, task) -> dict``: structured extraction of the
      mini-paper (method, data generation, metrics, claimed interval ...).
    - ``generate_code(paper_md, task, extraction) -> str``: one
      self-contained standard-library script source that writes
      ``predictions.json`` / ``labels.json`` / ``metrics.json`` in its
      working directory (the same contract ``reference_solution.py``
      follows, and the same one ``codegen_service``'s system prompt
      enforces for real model output).
    """

    def extract(self, paper_md: str, task: Mapping[str, Any]) -> dict[str, Any]: ...

    def generate_code(
        self, paper_md: str, task: Mapping[str, Any], extraction: Mapping[str, Any]
    ) -> str: ...


_WIRING_EXAMPLE = """
How to configure a provider (see docs/components/benchmark.md for the full guide):

1. Preferred: adapt the production service —
       from p2r_orchestration.codegen_service import CodegenService
       from p2r_orchestration.providers import (
           OpenAICompatibleProvider, ProviderConfig,
       )
       provider = OpenAICompatibleProvider(
           config=ProviderConfig(base_url=..., model_id=..., ...),
           transport=...,            # your async HTTP transport
           secret_resolver=...,      # keeps the API key out of env/logs
       )
       service = CodegenService(provider, artifact_store)
       # CodegenService.generate(...) is async and already applies the
       # AST policy; wrap it in the two sync methods of ProviderCodegen.

2. Minimal: implement ProviderCodegen yourself —
       class MyProvider:
           def extract(self, paper_md, task): ...
           def generate_code(self, paper_md, task, extraction): ...
   then pass it via
       --provider-module my_pkg.my_provider [--provider-factory create_provider]
"""


class NotConfiguredProvider:
    """Default online provider: always refuses with setup instructions."""

    def __init__(self, message: str | None = None) -> None:
        self._message = message

    def _refuse(self) -> None:
        raise ProviderNotConfigured(
            self._message
            or ("online 模式未配置代码生成 Provider。\n" + _WIRING_EXAMPLE)
        )

    def extract(self, paper_md: str, task: Mapping[str, Any]) -> dict[str, Any]:
        self._refuse()
        raise AssertionError  # unreachable

    def generate_code(
        self, paper_md: str, task: Mapping[str, Any], extraction: Mapping[str, Any]
    ) -> str:
        self._refuse()
        raise AssertionError  # unreachable


def load_provider(module_name: str, factory_name: str = "create_provider") -> Any:
    """Import ``module_name`` and build the provider via ``factory_name()``.

    The module is plain importlib usage — no network I/O happens here;
    whatever the factory returns must satisfy :class:`ProviderCodegen`.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise BenchmarkError(f"cannot import provider module {module_name!r}: {exc}") from None
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise BenchmarkError(
            f"provider module {module_name!r} has no callable factory {factory_name!r}"
        )
    provider = factory()
    for method in ("extract", "generate_code"):
        if not callable(getattr(provider, method, None)):
            raise BenchmarkError(
                f"provider {provider!r} does not satisfy the ProviderCodegen protocol "
                f"(missing callable {method})"
            )
    return provider


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_benchmark.py",
        description="Paper2Run paper->sandbox reproduction benchmark",
    )
    parser.add_argument("--mode", choices=("offline", "online"), required=True,
                        help="offline: reference solutions; online: provider-generated code")
    parser.add_argument("--papers", default=None,
                        help="comma-separated paper task ids (default: all)")
    parser.add_argument("--papers-dir", default=None,
                        help="alternative papers directory (default: bundled tasks)")
    parser.add_argument("--out", default=None, help="write results JSON here")
    parser.add_argument("--md", default=None, help="write Markdown report here")
    parser.add_argument("--provider-module", default=None,
                        help="online mode: module providing the codegen provider factory")
    parser.add_argument("--provider-factory", default="create_provider",
                        help="online mode: factory callable name (default: create_provider)")
    args = parser.parse_args(argv)

    papers_dir = Path(args.papers_dir) if args.papers_dir else DEFAULT_PAPERS_DIR
    try:
        if args.mode == "offline":
            payload = run_offline_benchmark(args.papers, papers_dir)
        else:
            if not args.provider_module:
                print(
                    "error: online 模式需要 --provider-module（未配置 Provider）。\n"
                    + _WIRING_EXAMPLE,
                    file=sys.stderr,
                )
                return 2
            provider = load_provider(args.provider_module, args.provider_factory)
            payload = run_online_benchmark(provider, args.papers, papers_dir)
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.out:
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.md:
        Path(args.md).write_text(build_markdown_report(payload), encoding="utf-8")

    summary = payload["summary"]
    stage_rates = ", ".join(
        f"{stage} {rate:.0%}" for stage, rate in summary["stage_pass_rates"].items()
    )
    print(f"Paper2Run benchmark ({payload['mode']}): "
          f"{summary['passed']}/{summary['total']} passed")
    print(f"stage pass rates: {stage_rates}")
    if args.out:
        print(f"results: {args.out}")
    if args.md:
        print(f"report:  {args.md}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
