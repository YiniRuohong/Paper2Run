"""Model routing control-plane tests; no real provider is simulated as acceptance."""
import copy
import json

import pytest
from pydantic import ValidationError

from p2r_contracts.autonomous import ProjectConfig, safe_config_snapshot
from p2r_orchestration.autonomous.security import ProfileStore
from p2r_orchestration.service import ServiceError
from .test_service import service, configuration, create, wait, Bridge


def specialist():
    return {"id": "methods", "description": "Study methods and review code", "task_types": ["method_design", "review"],
            "model": {"provider_profile": "expert", "credential_ref": "profile:expert"}}


def routed_config():
    return {**configuration(), "specialists": [specialist()]}


def configure(service):
    service.profiles.put("owner", {"id": "expert", "base_url": "http://localhost:1234", "model": "expert-model",
        "api": "openai-completions", "api_key": "expert-unit-secret", "context_window": 2000, "max_tokens": 500})
    with service.sessions() as db:
        service.put_config(db, "project", routed_config(), "1")
        db.commit()


def test_routing_contract_and_safe_references():
    value = routed_config()
    parsed = ProjectConfig.model_validate(value)
    assert parsed.specialists[0].task_types == ["method_design", "review"]
    assert safe_config_snapshot(value)["specialists"][0]["model"]["credential_ref"] == "[redacted]"
    assert value["specialists"][0]["model"]["credential_ref"] == "profile:expert"
    assert ProjectConfig.model_validate(configuration()).specialists == []


@pytest.mark.parametrize("field,value", [("id", "../escape"), ("description", " "), ("task_types", []),
    ("task_types", ["arbitrary_execution"]), ("task_types", ["coding", "coding"]), ("api_key", "secret")])
def test_invalid_specialist_contract(field, value):
    cfg = routed_config()
    cfg["specialists"][0][field] = value
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(cfg)


def test_duplicate_and_excess_specialists_rejected():
    cfg = routed_config()
    cfg["specialists"] *= 2
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(cfg)
    cfg["specialists"] = [{**specialist(), "id": f"expert-{i}"} for i in range(17)]
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate(cfg)


def test_specialist_profiles_are_frozen_without_secrets(service):
    configure(service)
    task_id = create(service)
    service.profiles.put("owner", {"id": "expert", "base_url": "https://changed.invalid", "model": "changed",
        "api": "openai-completions", "api_key": "different-secret"})
    with service.sessions() as db:
        row = service.row(db, task_id)
        assert row.config_snapshot["specialist_profiles"]["methods"]["model"] == "expert-model"
        assert "expert-unit-secret" not in json.dumps(row.config_snapshot)
        row.state = "paused"
        with pytest.raises(ServiceError, match="credentials"):
            service.control(db, task_id, "resume")


def test_other_actor_specialist_not_available(service):
    service.profiles.put("other", {"id": "expert", "base_url": "http://localhost", "model": "private",
        "api": "openai-completions", "api_key": "other-secret"})
    with service.sessions() as db:
        service.put_config(db, "project", routed_config(), "1")
        db.commit()
    with pytest.raises(ServiceError):
        create(service)


def test_worker_receives_ephemeral_specialists_and_aggregates_usage(service):
    configure(service)
    configs = []
    class RoutingBridge(Bridge):
        def run(self, config, prompt, callback):
            configs.append(copy.deepcopy(config))
            assert "expert-unit-secret" not in prompt
            for delegated in [False, True]:
                callback({"type": "event", "event": {"type": "turn_start", "delegated": delegated}})
                callback({"type": "event", "event": {"type": "message_end", "delegated": delegated, "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "Expert claim" if delegated else "Main narrative"}],
                    "usage": {"input": 6, "output": 4, "totalTokens": 10}}}})
            callback({"type": "result", "text": "Main narrative", "usage": {"calls": 2, "total_tokens": 20}})
    service.bridge_factory = RoutingBridge
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert configs[0]["specialists"][0]["model"]["api_key"] == "expert-unit-secret"
    assert configs[0]["specialists"][0]["id"] == "methods"
    assert row.usage_json["calls"] == 2 and row.usage_json["total_tokens"] == 20
    assert row.report_json["model_narrative"] == "Main narrative"
    assert row.report_json["verdict"] == "not_comparable"
    assert "expert-unit-secret" not in json.dumps(service.public(row))


def test_delegate_budget_boundary_preserves_main_narrative(service):
    configure(service)
    cfg = routed_config()
    cfg["limits"]["max_model_calls"] = 2
    with service.sessions() as db:
        service.put_config(db, "project", cfg, "2")
        db.commit()
    class BudgetBridge(Bridge):
        def run(self, config, prompt, callback):
            for delegated in [False, True]:
                callback({"type": "event", "event": {"type": "turn_start", "delegated": delegated}})
                callback({"type": "event", "event": {"type": "message_end", "delegated": delegated, "message": {
                    "role": "assistant", "content": [{"type": "text", "text": "Unverified expert success" if delegated else "Main plan"}],
                    "usage": {"input": 1, "output": 1, "totalTokens": 2}}}})
    service.bridge_factory = BudgetBridge
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "stopped"
    assert row.usage_json["calls"] == 2
    assert row.report_json["model_narrative"] == "Main plan"
    assert row.report_json["verdict"] == "not_comparable"


def test_missing_frozen_specialist_cannot_fall_back_to_live_profile(service):
    configure(service)
    task_id = create(service)
    with service.sessions() as db:
        row = service.row(db, task_id)
        row.config_snapshot = {**row.config_snapshot, "specialist_profiles": {}}
        with pytest.raises(ServiceError) as exc:
            service.resolve_models(row)
        assert exc.value.code == "MODEL_SNAPSHOT_REQUIRED"


def test_interrupted_specialist_reserves_largest_frozen_profile(service):
    configure(service)
    # Use a small coordinator and a larger expert so reusing the main model's
    # reservation would undercount a specialist response lost on interruption.
    service.profiles.put("owner", {"id": "test", "base_url": "http://localhost:1234", "model": "test",
        "api": "openai-completions", "api_key": "unit-secret-123456", "context_window": 100, "max_tokens": 50})
    class InterruptedBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "event", "event": {"type": "turn_start", "delegated": True}})
            raise RuntimeError("connection lost")
    service.bridge_factory = InterruptedBridge
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "paused"
    assert row.usage_json["calls"] == 1
    assert row.usage_json["unverified_token_reservation"] == 2500
    assert row.usage_json["total_tokens"] == 2500


def test_failed_expert_reservation_survives_successful_main_response(service):
    configure(service)
    service.profiles.put("owner", {"id": "test", "base_url": "http://localhost:1234", "model": "test",
        "api": "openai-completions", "api_key": "unit-secret-123456", "context_window": 100, "max_tokens": 50})
    class FailedExpertBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "event", "event": {"type": "turn_start", "delegated": True}})
            callback({"type": "event", "event": {"type": "message_end", "delegated": True,
                "message": {"role": "assistant", "content": [], "stopReason": "error", "usage": {}}}})
            callback({"type": "event", "event": {"type": "turn_start"}})
            callback({"type": "event", "event": {"type": "message_end", "message": {
                "role": "assistant", "content": [], "usage": {"input": 6, "output": 4, "totalTokens": 10}}}})
            callback({"type": "result", "text": "No evidence", "usage": {"calls": 2, "total_tokens": 10}})
    service.bridge_factory = FailedExpertBridge
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.usage_json["calls"] == 2
    assert row.usage_json["total_tokens"] == 2510
    assert row.usage_json["unverified_token_reservation"] == 2500


def test_unknown_expert_usage_exhausts_budget_before_more_tools(service):
    configure(service)
    cfg = routed_config()
    cfg["limits"]["max_model_tokens"] = 3000
    with service.sessions() as db:
        service.put_config(db, "project", cfg, "2")
        db.commit()
    service.profiles.put("owner", {"id": "test", "base_url": "http://localhost:1234", "model": "test",
        "api": "openai-completions", "api_key": "unit-secret-123456", "context_window": 100, "max_tokens": 50})
    tool_results = []
    class UnknownUsageBridge(Bridge):
        def run(self, config, prompt, callback):
            for _ in range(2):
                callback({"type": "event", "event": {"type": "turn_start", "delegated": True}})
                callback({"type": "event", "event": {"type": "message_end", "delegated": True,
                    "message": {"role": "assistant", "content": [], "stopReason": "error", "usage": {}}}})
            tool_results.append(callback({"type": "tool_request", "id": "late", "name": "write",
                                          "arguments": {"path": "late.py", "content": "not allowed"}}))
            callback({"type": "result", "text": "late", "usage": {"calls": 2, "total_tokens": 0}})
    service.bridge_factory = UnknownUsageBridge
    task_id = create(service)
    service.launch(task_id)
    row = wait(service, task_id)
    assert row.state == "stopped"
    assert row.usage_json["total_tokens"] == 3000
    assert row.usage_json["unverified_token_reservation"] == 3000
    assert tool_results[0]["is_error"]
    assert not service.workers[task_id]["target"].files


def test_expert_reservations_survive_resume_without_double_counting(service):
    configure(service)
    service.profiles.put("owner", {"id": "test", "base_url": "http://localhost:1234", "model": "test",
        "api": "openai-completions", "api_key": "unit-secret-123456", "context_window": 100, "max_tokens": 50})
    class FailedExpertBridge(Bridge):
        def run(self, config, prompt, callback):
            callback({"type": "event", "event": {"type": "turn_start", "delegated": True}})
            callback({"type": "event", "event": {"type": "message_end", "delegated": True,
                "message": {"role": "assistant", "content": [], "stopReason": "error", "usage": {}}}})
            callback({"type": "event", "event": {"type": "turn_start"}})
            callback({"type": "event", "event": {"type": "message_end", "message": {
                "role": "assistant", "content": [], "usage": {"totalTokens": 10}}}})
            # Ends without result, leaving explicit user resume required.
    service.bridge_factory = FailedExpertBridge
    task_id = create(service)
    for run in range(2):
        if run:
            with service.sessions() as db:
                service.control(db, task_id, "resume")
                db.commit()
        service.launch(task_id)
        row = wait(service, task_id)
        assert row.state == "paused"
        assert row.usage_json["total_tokens"] == 2510 * (run + 1)
        assert row.usage_json["unverified_token_reservation"] == 2500 * (run + 1)
        assert row.usage_json["calls"] == 2 * (run + 1)


def source_profile():
    return {"id": "source", "base_url": "https://provider.invalid/v1", "model": "main",
            "api": "openai-completions", "api_key": "source-runtime-secret"}


def test_reuse_credentials_is_explicit_actor_and_destination_bound():
    store = ProfileStore()
    store.put("owner", source_profile())
    target = {k: v for k, v in source_profile().items() if k != "api_key"}
    target.update(id="expert", model="expert-model", reuse_credentials_from="source")
    public = store.put("owner", target)
    assert store.resolve("owner", "expert")["api_key"] == "source-runtime-secret"
    assert public["secret_configured"]
    assert "reuse_credentials_from" not in public
    assert "source-runtime-secret" not in json.dumps(public)
    assert "reuse_credentials_from" not in store.profiles[("owner", "expert")]
    with pytest.raises(ServiceError):
        store.put("other", target)


def test_anonymous_profile_uses_same_loopback_hosts_as_sdk():
    store = ProfileStore()
    with pytest.raises(ServiceError):
        store.put("owner", {"id": "anonymous", "base_url": "http://127.0.0.2:3425", "model": "glm",
                            "api": "openai-completions", "auth_mode": "none"})


def test_reuse_cannot_broaden_environment_model_grant(monkeypatch):
    monkeypatch.setenv("P2R_UNIT_ROUTING_KEY", "environment-unit-secret")
    source = source_profile()
    source.pop("api_key")
    source["credential_ref"] = "env:P2R_UNIT_ROUTING_KEY"
    grant = ("owner", source["id"], source["base_url"], source["api"], source["model"], source["credential_ref"])
    store = ProfileStore(environment_grants=[grant])
    store.put("owner", source)
    with pytest.raises(ServiceError):
        store.put("owner", {"id": "expert", "base_url": source["base_url"], "api": source["api"],
                            "model": "not-in-grant", "reuse_credentials_from": "source"})


@pytest.mark.parametrize("change", [{"base_url": "https://other.invalid/v1"}, {"api": "anthropic-messages"},
    {"api_key": "conflicting-secret"}, {"credential_ref": "env:SOME_KEY"}, {"auth_mode": "none"}])
def test_reuse_credentials_rejects_ambiguous_or_different_destination(change):
    store = ProfileStore()
    store.put("owner", source_profile())
    target = {k: v for k, v in source_profile().items() if k != "api_key"}
    target.update(id="expert", model="expert-model", reuse_credentials_from="source", **change)
    with pytest.raises(ServiceError):
        store.put("owner", target)
