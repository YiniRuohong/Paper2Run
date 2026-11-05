"""submit_candidate gate and immutable candidate snapshot."""
from __future__ import annotations

import hashlib
import shutil

from .evidence import (MAX_FILE, artifact_root, current_round, ensure_not_under_review, entrypoint_code,
                       entrypoint_executions, round_artifacts, safe_name)
from .security import digest, fail

REQUIRED_KINDS = ("code", "environment", "data", "result")
ENTRYPOINT_ITEM = "entrypoint code artifact"
RUN_ITEM = "successful entrypoint run after latest code registration"
LOG_PAGE = 1_000_000


def qualifying_run(db, row):
    """Latest successful direct entrypoint run launched after the round's latest code registration.

    Anchored on the job launch sequence, so re-observing an older job never qualifies it."""
    artifacts = round_artifacts(row)
    latest_code = max((a.get("sequence", 0) for a in artifacts if a["kind"] == "code"), default=0)
    runs = [e for e in entrypoint_executions(db, row)
            if e["matches_protocol_entrypoint"] and e["result"].get("exit_code") == 0
            and e["launch_sequence"] is not None and e["launch_sequence"] > latest_code]
    return runs[-1] if runs else None


def gate(db, row):
    """Missing submit_candidate requirements, in a stable order; the same rules report() applies."""
    missing = [] if row.protocol_json else ["frozen protocol"]
    artifacts = round_artifacts(row)
    kinds = {a["kind"] for a in artifacts}
    missing += [f"{kind} artifact" for kind in REQUIRED_KINDS if kind not in kinds]
    protocol = row.protocol_json["protocol"] if row.protocol_json else {}
    if entrypoint_code(artifacts, protocol.get("entrypoint")) is None:
        missing.append(ENTRYPOINT_ITEM)
    names = {a["name"] for a in artifacts}
    missing += [f"expected artifact {name}" for name in protocol.get("expected_artifacts", []) if name not in names]
    if qualifying_run(db, row) is None:
        missing.append(RUN_ITEM)
    return missing


def _copy_verified(source, destination, sha256):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_FILE:
        fail("EVIDENCE_INTEGRITY", "frozen artifact is missing or unsafe", 409)
    shutil.copyfile(source, destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != sha256:
        destination.unlink()
        fail("EVIDENCE_INTEGRITY", "frozen artifact hash mismatch", 409)


def _fetch_logs(service, target, job_id):
    chunks, total, offset = [], 0, 0
    while total < MAX_FILE:
        page = target.logs(job_id, offset, LOG_PAGE)
        text = page.get("stdout", page.get("text", ""))
        text = text if isinstance(text, str) else ""
        chunks.append(text)
        total += len(text.encode())
        following = page.get("next_offset")
        if type(following) is not int or following <= offset:
            break
        offset = following
    return service.profiles.clean("".join(chunks).encode()[:MAX_FILE].decode(errors="ignore"), bounded=False)


def freeze(service, target, task_id, summary):
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        ensure_not_under_review(row)
        missing = gate(db, row)
        if missing:
            fail("CANDIDATE_INCOMPLETE", "missing: " + "; ".join(missing), 409)
        run_fact = qualifying_run(db, row)
        round_ = current_round(row)
        protocol_sha = row.protocol_json["sha256"]
        artifacts = [dict(a) for a in round_artifacts(row)]
        root = artifact_root(service, row)
        task_root = service.task_root(task_id)
    run = {"job_id": run_fact["result"]["job_id"], "exit_code": run_fact["result"]["exit_code"],
           "sequence": run_fact["launch_sequence"]}
    if not isinstance(run["job_id"], str) or not run["job_id"].isalnum():
        fail("CANDIDATE_INCOMPLETE", "entrypoint run has no attributable job", 409)
    snapshot = digest({"protocol_sha256": protocol_sha, "round": round_, "run": run,
                       "artifacts": [{k: a[k] for k in ("name", "kind", "sha256")} for a in artifacts]})
    base = task_root / "candidates" / snapshot
    for item in artifacts:
        name = safe_name(item["name"])
        _copy_verified(root / name, base / "artifacts" / name, item["sha256"])
    log = _fetch_logs(service, target, run["job_id"])
    (base / "logs").mkdir(parents=True, exist_ok=True)
    (base / "logs" / f"{run['job_id']}.txt").write_text(log)
    candidate = {"snapshot_sha256": snapshot, "round": round_, "protocol_sha256": protocol_sha,
                 "artifacts": artifacts, "run": run, "summary": service.profiles.clean(summary),
                 "review_status": "pending", "attempts": 0}
    with service.lock, service.sessions() as db:
        row = service.row(db, task_id)
        ensure_not_under_review(row)
        if current_round(row) != round_ or not row.protocol_json or row.protocol_json["sha256"] != protocol_sha:
            fail("CANDIDATE_STALE", "evidence changed while the candidate was being frozen", 409)
        row.harness_json = {**row.harness_json, "candidate": candidate}
        service.event(db, row, "candidate_frozen", candidate)
        db.commit()
    return candidate
