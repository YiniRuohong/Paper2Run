from __future__ import annotations

from p2r_orchestration.environment_checker import (
    CheckItem,
    EnvironmentChecker,
    EnvironmentDecision,
    ProbeResult,
)


class FakeProbe:
    def __init__(self, items):
        self.items = items
        self.calls = 0

    def check(self, requirements):
        self.calls += 1
        return self.items


def item(key, status="pass", required=True):
    return CheckItem(
        key=key,
        status=status,
        required=required,
        observed="observed",
        required_value="required",
        suggestion="suggestion",
    )


def test_ready_requires_supported_recipe_and_all_required_items_pass():
    checker = EnvironmentChecker(FakeProbe([item("python"), item("gpu", "not_needed", False)]))
    result = checker.check(recipe_supported=True, requirements={"python": ">=3.12"})
    assert result.decision is EnvironmentDecision.READY
    assert result.checker_version
    assert result.items[0].key == "python"


def test_fail_or_unknown_blocks_and_never_runs_repairs():
    probe = FakeProbe([item("python", "unknown")])
    result = EnvironmentChecker(probe).check(recipe_supported=True, requirements={})
    assert result.decision is EnvironmentDecision.BLOCKED
    assert result.items[0].status == "unknown"
    assert probe.calls == 1
    assert result.repair_commands == []


def test_unsupported_recipe_is_not_ready_even_when_environment_passes():
    result = EnvironmentChecker(FakeProbe([item("python")])).check(
        recipe_supported=False, requirements={}
    )
    assert result.decision is EnvironmentDecision.UNSUPPORTED


def test_probe_result_is_json_safe_and_no_unknown_is_pass():
    result = ProbeResult(items=[item("disk", "unknown")])
    assert result.model_dump()["items"][0]["status"] == "unknown"
