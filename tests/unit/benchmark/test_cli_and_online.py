"""CLI behaviour and the online (provider) five-stage pipeline — no network."""
from __future__ import annotations

import json

import pytest

import run_benchmark
from run_benchmark import (
    ONLINE_STAGES,
    BenchmarkError,
    NotConfiguredProvider,
    ProviderNotConfigured,
    load_provider,
    main,
    run_online_benchmark,
    run_online_paper,
)

PAPERS_DIR = run_benchmark.DEFAULT_PAPERS_DIR


class ReferenceSolutionProvider:
    """In-process fake provider: 'generates' the bundled reference code."""

    def extract(self, paper_md, task):
        return {"task_id": task["task_id"], "metric": task["metric"], "summary": "ok"}

    def generate_code(self, paper_md, task, extraction):
        path = PAPERS_DIR / task["task_id"] / "reference_solution.py"
        return path.read_text(encoding="utf-8")


class NeverGeneratesProvider:
    """Fails at the extract stage; later stages must be skipped, not run."""

    def extract(self, paper_md, task):
        raise ProviderNotConfigured("no provider configured")

    def generate_code(self, paper_md, task, extraction):
        raise AssertionError("generate_code must not run after extract failed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_offline_single_paper_writes_outputs(tmp_path):
    out_path = tmp_path / "results.json"
    md_path = tmp_path / "report.md"
    code = main([
        "--mode", "offline",
        "--papers", "01-weighted-knn",
        "--out", str(out_path),
        "--md", str(md_path),
    ])
    assert code == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "offline"
    assert payload["summary"]["passed"] == 1
    assert "01-weighted-knn" in md_path.read_text(encoding="utf-8")
    assert "诚实声明" in md_path.read_text(encoding="utf-8")


def test_cli_unknown_paper_exits_2(capsys):
    assert main(["--mode", "offline", "--papers", "ghost-paper"]) == 2
    assert "unknown paper task" in capsys.readouterr().err


def test_cli_unknown_mode_exits_2():
    with pytest.raises(SystemExit) as excinfo:
        main(["--mode", "teleport"])
    assert excinfo.value.code == 2


def test_cli_online_without_provider_exits_2(capsys):
    assert main(["--mode", "online"]) == 2
    captured = capsys.readouterr()
    assert "--provider-module" in captured.err


def test_cli_online_with_unknown_provider_module_exits_2(capsys):
    code = main(["--mode", "online", "--provider-module", "definitely_not_a_module"])
    assert code == 2
    assert "cannot import provider module" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# online five-stage pipeline (in-process providers only, never network)
# ---------------------------------------------------------------------------


def test_online_five_stage_pipeline_with_fake_provider():
    payload = run_online_benchmark(
        ReferenceSolutionProvider(), "01-weighted-knn,06-linear-regression-r2",
        papers_dir=PAPERS_DIR,
    )
    assert payload["mode"] == "online"
    assert payload["summary"]["failed"] == 0
    assert payload["summary"]["pass_rate"] == 1.0
    for result in payload["results"]:
        assert list(result["stages"]) == list(ONLINE_STAGES)
        assert all(stage["status"] == "pass" for stage in result["stages"].values())
    assert set(payload["summary"]["stage_pass_rates"]) == set(ONLINE_STAGES)
    assert payload["summary"]["stage_pass_rates"]["extract"] == 1.0
    assert payload["summary"]["stage_pass_rates"]["codegen"] == 1.0


def test_online_extract_failure_skips_downstream_stages():
    result = run_online_paper(
        NeverGeneratesProvider(), PAPERS_DIR / "02-logistic-regression",
    )
    assert result["passed"] is False
    assert result["stages"]["extract"]["status"] == "fail"
    assert "no provider configured" in result["stages"]["extract"]["error"]
    for stage in ("codegen", "ast_validate", "sandbox", "interval"):
        assert result["stages"][stage]["status"] == "skipped"
    assert result["metric_value"] is None


def test_not_configured_provider_refuses_with_wiring_example():
    provider = NotConfiguredProvider()
    with pytest.raises(ProviderNotConfigured, match="OpenAICompatibleProvider"):
        provider.extract("paper", {})
    with pytest.raises(ProviderNotConfigured, match="OpenAICompatibleProvider"):
        provider.generate_code("paper", {}, {})


def test_load_provider_builds_object_from_module(tmp_path, monkeypatch):
    module_path = tmp_path / "fake_bench_provider_module.py"
    module_path.write_text(
        "class P:\n"
        "    def extract(self, paper_md, task):\n"
        "        return {}\n"
        "    def generate_code(self, paper_md, task, extraction):\n"
        "        return 'x = 1\\n'\n"
        "def create_provider():\n"
        "    return P()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    provider = load_provider("fake_bench_provider_module")
    assert provider.extract("paper", {}) == {}
    assert provider.generate_code("paper", {}, {}) == "x = 1\n"


def test_load_provider_rejects_module_without_factory(tmp_path, monkeypatch):
    module_path = tmp_path / "fake_no_factory_module.py"
    module_path.write_text("X = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(BenchmarkError, match="no callable factory"):
        load_provider("fake_no_factory_module")


def test_load_provider_rejects_protocol_violation(tmp_path, monkeypatch):
    module_path = tmp_path / "fake_bad_provider_module.py"
    module_path.write_text(
        "class P:\n"
        "    def extract(self, paper_md, task):\n"
        "        return {}\n"
        "def create_provider():\n"
        "    return P()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(BenchmarkError, match="ProviderCodegen protocol"):
        load_provider("fake_bad_provider_module")
