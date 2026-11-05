"""Freeze real files and build bounded exports without exposing control paths."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import zipfile

from sqlalchemy import select
from p2r_evaluation.autonomous import evaluate_artifacts
from p2r_evaluation.reference_verification import verify_reference_values
from p2r_persistence.autonomous import AutonomousEventRecord as Event
from .security import fail, digest

def verify_export_zip(raw):
    from p2r_artifacts.archive_verify import verify_export_zip as verify
    result = verify(raw)
    return {"files": result["files"], "manifest_sha256": result["manifest_sha256"]}


MAX_FILE = 32 * 1024 * 1024
MAX_EXPORT = 128 * 1024 * 1024


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or "\\" in name or any(ord(c) < 32 for c in name):
        fail("INVALID_ARTIFACT", "artifact name must be a relative safe path")
    if len(path.parts) > 8 or len(name) > 255:
        fail("INVALID_ARTIFACT", "artifact name exceeds limits")
    return path.as_posix()


def current_round(row):
    return int((row.harness_json or {}).get("repair_round", 0))


def artifact_root(service, row, round_=None):
    version = row.protocol_json["sha256"] if row.protocol_json else "unversioned"
    root = service.task_root(row.id) / "artifacts" / version
    if row.harness_json is None:  # legacy task: unversioned-by-round layout
        return root
    return root / f"r{current_round(row) if round_ is None else round_}"


def round_artifacts(row):
    """Artifacts of the current protocol version and the current round."""
    current = row.protocol_json["sha256"] if row.protocol_json else None
    if row.harness_json is None:
        return [a for a in row.artifacts_json if a["protocol_sha256"] == current]
    round_ = current_round(row)
    return [a for a in row.artifacts_json
            if a["protocol_sha256"] == current and a.get("round", 0) == round_]


def ensure_not_under_review(row):
    candidate = (row.harness_json or {}).get("candidate")
    if candidate and candidate.get("review_status") == "pending":
        fail("CANDIDATE_UNDER_REVIEW", "a candidate is under review; evidence is frozen until it completes", 409)


def register(service, target, task_id, args):
    name = safe_name(args["name"])
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        ensure_not_under_review(row)
        root = artifact_root(service, row)
        version = row.protocol_json["sha256"] if row.protocol_json else None
        round_ = None if row.harness_json is None else current_round(row)
        scope = [a for a in row.artifacts_json
                 if a["protocol_sha256"] == version and a.get("round", 0) == (0 if round_ is None else round_)]
        if any(a["name"] == name for a in scope):
            fail("ARTIFACT_IMMUTABLE", "evidence is immutable within a protocol version and repair round", 409)
        if len(scope) >= 256:
            fail("ARTIFACT_LIMIT", "round artifact count exhausted", 409)
    destination = root / name
    root.mkdir(parents=True, exist_ok=True)
    if not destination.resolve().is_relative_to(root.resolve()) or destination.is_symlink():
        fail("INVALID_ARTIFACT", "unsafe artifact destination")
    target.collect(args["path"], destination)
    if not destination.is_file() or destination.is_symlink() or destination.stat().st_size > MAX_FILE:
        fail("INVALID_ARTIFACT", "collection did not produce a bounded regular file")
    raw = destination.read_bytes()
    # Secrets may appear in experiment output accidentally; never freeze/export
    # raw credential-bearing evidence. Refuse rather than silently alter hashes.
    with service.profiles.lock:
        if any(secret.encode() in raw for secret in service.profiles.secrets):
            destination.unlink()
            fail("SECRET_IN_ARTIFACT", "artifact contains a runtime credential")
    info = {"name": name, "kind": args["kind"], "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw), "protocol_sha256": version, "source_path": args["path"]}
    if round_ is not None:
        info["round"] = round_
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        ensure_not_under_review(row)
        row.report_json = None
        service.event(db, row, "artifact_frozen", info)
        info["sequence"] = row.event_sequence
        row.artifacts_json = [*row.artifacts_json, info]
        db.commit()
    return info


def verified_files(service, row):
    result = []
    for item in row.artifacts_json:
        name = safe_name(item["name"])
        version = item["protocol_sha256"] or "unversioned"
        root = service.task_root(row.id) / "artifacts" / version
        prefix = f"artifacts/{version}"
        if item.get("round") is not None:
            root = root / f"r{int(item['round'])}"
            prefix += f"/r{int(item['round'])}"
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not path.is_file() or path.stat().st_size > MAX_FILE:
            fail("EVIDENCE_INTEGRITY", "frozen artifact is missing or unsafe", 409)
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            fail("EVIDENCE_INTEGRITY", "frozen artifact hash mismatch", 409)
        result.append((f"{prefix}/{name}", raw))
    return result


def evaluation(service, row):
    verified_files(service, row)
    return evaluation_at(service, row, artifact_root(service, row))


def evaluation_at(service, row, root):
    """Program-owned evaluation of the artifacts under root (live round or frozen candidate)."""
    if not row.protocol_json:
        return {"verdict": "not_comparable", "comparison_eligible": False, "metrics": {}, "reasons": ["No approved protocol."]}
    result = evaluate_artifacts(row.protocol_json, root)
    # API-v1 does not expose independent evaluator custody/attestation. User or
    # agent booleans cannot establish independent labels or absence of leakage.
    result["comparison_eligible"] = False
    result["verdict"] = "not_comparable"
    result.pop("within_tolerance", None)
    result.setdefault("reasons", []).append("Independent evaluation custody has not been verified by the control plane.")
    return result


def entrypoint_executions(db, row):
    """Observed execute/status facts, each flagged by whether it ran the protocol entrypoint directly."""
    current = row.protocol_json["sha256"] if row.protocol_json else None
    facts = db.scalars(select(Event).where(Event.task_id == row.id, Event.type == "tool_completed").order_by(Event.sequence)).all()
    import shlex
    jobs = {j.get("job_id"): j for j in row.jobs_json.values()}
    entrypoint = row.protocol_json["protocol"]["entrypoint"] if row.protocol_json else None
    executions = []
    for fact in facts:
        if fact.payload.get("name") not in ("execute", "status"):
            continue
        observation = fact.payload.get("result")
        if not isinstance(observation, dict):
            continue
        job = jobs.get(observation.get("job_id"), {})
        command = job.get("command", "")
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars="();<>|&")
            lexer.whitespace_split = True
            lexer.commenters = ""
            tokens = list(lexer)
        except ValueError:
            tokens = []
        executables = {entrypoint, "./" + entrypoint} if entrypoint else set()
        direct = bool(tokens and tokens[0] in executables)
        interpreted = bool(len(tokens) >= 2 and PurePosixPath(tokens[0]).name in
                           {"python", "python3", "bash", "sh", "node", "Rscript", "julia"} and tokens[1] in executables)
        # Conservative direct invocation gate: echo/installation commands and
        # compound shells cannot masquerade as an experiment merely naming it.
        matches = bool((direct or interpreted) and job.get("protocol_sha256") == current
                       and not any(char in command for char in ";|&<>($`\n\r\\")
                       and not any(t and all(char in "();<>|&" for char in t) for t in tokens))
        executions.append({"sequence": fact.sequence, "tool": fact.payload.get("name"), "command": command,
            "launch_sequence": job.get("sequence") if type(job.get("sequence")) is int else None,
            "result": observation, "matches_protocol_entrypoint": matches})
    return executions


def report(service, db, row, narrative=""):
    try:
        calculated = evaluation(service, row)
    except (ValueError, OSError) as exc:
        calculated = {"verdict": "not_comparable", "comparison_eligible": False,
                      "metrics": {}, "reasons": [str(exc)]}
    artifacts = round_artifacts(row)
    expected = row.protocol_json["protocol"]["expected_artifacts"] if row.protocol_json else []
    names = {a["name"] for a in artifacts}
    missing = sorted(set(expected) - names)
    kinds = {a["kind"] for a in artifacts}
    missing_kinds = sorted({"code", "environment", "data", "result"} - kinds)
    executions = entrypoint_executions(db, row)
    entrypoint = row.protocol_json["protocol"]["entrypoint"] if row.protocol_json else None
    frozen_code = next((a for a in artifacts if a["kind"] == "code" and
                        entrypoint in (a["name"], a["source_path"])), None)
    observed_success = bool(frozen_code) and any(f["matches_protocol_entrypoint"] and f["result"].get("exit_code") == 0 for f in executions)
    verification = None
    if row.reference_values_json is not None and row.protocol_json:
        try:
            verification = verify_reference_values(
                row.protocol_json, artifact_root(service, row), row.reference_values_json,
                scope=row.reference_scope or "", user_confirmed_sources=row.reference_sources_confirmed,
                comparison_custody_verified=False,
            )
        except (ValueError, OSError) as exc:
            verification = {"schema_version": "paper2run.independent_verification.v1",
                            "scope": row.reference_scope, "verdict": "not_comparable",
                            "comparisons": [], "limitations": ["Reference verification failed safely."]}
    result = {"status": "incomplete" if missing or missing_kinds or not artifacts or not observed_success else "not_comparable",
              "execution_observations": executions[-100:], "successful_execution_observed": observed_success,
              "completion_gate": {"entrypoint": entrypoint, "frozen_code_sha256": frozen_code["sha256"] if frozen_code else None,
                  "rule": "Approved entrypoint appears in an observed command with exit_code=0; entrypoint code and code/environment/data/result artifacts are frozen. This does not prove scientific validity or that artifact contents came from that command."},
              "verdict": "not_comparable", "model_narrative": service.profiles.clean(narrative),
              "evaluation": calculated, "artifacts": artifacts, "missing_artifacts": missing,
              "missing_evidence_kinds": missing_kinds, "protocol": row.protocol_json,
              "reference_values": row.reference_values_json,
              "verification": verification,
              "limitations": ["Model narrative is not execution evidence.",
                              "Storage is observed; token usage is provider-reported."]}
    result["sha256"] = digest(result)
    row.report_json = result
    service.event(db, row, "report_created", result)
    return result


def export_zip(service, task_id):
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        files = verified_files(service, row)
        events = db.scalars(select(Event).where(Event.task_id == task_id).order_by(Event.sequence).limit(10001)).all()
        if len(events) > 10000:
            fail("EXPORT_LIMIT", "event journal exceeds export limit", 409)
        payloads = {
            "task.json": service.public(row), "config.json": row.config_snapshot,
            "skills.json": row.skills_json, "paper-context.json": row.context_json,
            "protocol.json": row.protocol_json, "report.json": row.report_json,
            "jobs.json": row.jobs_json, "provenance.json": row.artifacts_json,
            "logs/events.json": [{"sequence": e.sequence, "type": e.type, "payload": e.payload,
                                  "created_at": e.created_at.isoformat()} for e in events],
        }
        if row.reference_values_json is not None:
            payloads["reference_values.json"] = row.reference_values_json
            payloads["verification.json"] = (row.report_json or {}).get("verification")
        for name, payload in payloads.items():
            files.append((name, json.dumps(service.profiles.clean(payload, bounded=False), ensure_ascii=False, sort_keys=True).encode()))
        if sum(len(data) for _, data in files) > MAX_EXPORT:
            fail("EXPORT_LIMIT", "export exceeds bounded archive size", 409)
        with service.profiles.lock:
            secrets = tuple(s.encode() for s in service.profiles.secrets)
        for _, raw in files:
            if any(s in raw for s in secrets):
                fail("SECRET_IN_ARTIFACT", "export refused because evidence contains a credential", 409)
        manifest = {name: {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} for name, raw in files}
        files.append(("manifest.json", json.dumps(manifest, sort_keys=True).encode()))
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, raw in files:
                archive.writestr(name, raw)
        return output.getvalue()
