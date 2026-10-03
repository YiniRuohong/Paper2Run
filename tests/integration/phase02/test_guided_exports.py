import hashlib
import io
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from p2r_orchestration.run_worker import process_next_run_job
from p2r_orchestration.report_jobs import process_next_report_job
from p2r_persistence.job_repository import JobRepository
from p2r_persistence.models import (
    ExportArtifactRecord,
    ExportItemRecord,
    ExportRecord,
    Job,
    MembershipRecord,
    OperationRecord,
    ReportRecord,
    RunRecord,
)
from test_export_artifacts_http import seeded, _prepare_run, _prepare_report, _headers


def _set_report_content(app, run_id, mutate):
    from p2r_orchestration.report_builder import report_hash

    factory = app.state.runtime["session_factory"]
    with factory.begin() as db:
        report = db.scalar(select(ReportRecord).where(ReportRecord.run_id == run_id))
        content = dict(report.content_json)
        mutate(content)
        new_hash = report_hash(content)
        report.content_json = content
        report.content_hash = new_hash
        run = db.get(RunRecord, run_id)
        run.report_hash = new_hash
    return new_hash


def _export(app, client, run_id, formats, key):
    response = client.post(
        f"/api/v1/runs/{run_id}/exports",
        headers=_headers(key),
        json={"formats": formats},
    )
    assert response.status_code == 202, response.text
    assert process_next_run_job(app.state.runtime["session_factory"], app.state.runtime["artifact_store"])
    exports = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"}).json()["data"]
    return response.json()["data"], exports[0]


def test_export_formats_and_checkpoint_validation(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        for key, body in [
            ("unknown", {"formats": ["nope"]}),
            ("empty", {"formats": []}),
            ("duplicate", {"formats": ["report_json", "report_json"]}),
            ("checkpoint", {"formats": ["report_json"], "include_checkpoint": True}),
        ]:
            response = client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers(key), json=body)
            assert response.status_code == 422
        missing = client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("missing"), json={})
        assert missing.status_code == 422


def test_export_creates_operation_and_tracks_status(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        queued, export = _export(
            app, client, run_id,
            ["report_json", "report_markdown", "bundle_zip"],
            "operation-export",
        )
        # _export already drained the job; rebuild one queued export to inspect
        # the pre-processing response contract and operation lifecycle.
        response = client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("operation-export-2"),
            json={"formats": ["report_json", "report_markdown", "bundle_zip"]},
        )
        assert response.status_code == 202
        body = response.json()["data"]
        assert body["operation_id"]
        operation = client.get(f"/api/v1/operations/{body['operation_id']}", headers={"X-Actor-ID": "owner"})
        assert operation.status_code == 200
        assert operation.json()["data"]["kind"] == "export_bundle"
        assert operation.json()["data"]["status"] == "queued"
        assert process_next_run_job(app.state.runtime["session_factory"], app.state.runtime["artifact_store"])
        operation = client.get(f"/api/v1/operations/{body['operation_id']}", headers={"X-Actor-ID": "owner"})
        assert operation.json()["data"]["status"] == "succeeded"
        exports = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"}).json()["data"]
        done = next(x for x in exports if x["id"] != export["id"])
        assert done["operation_id"] == body["operation_id"]
        assert done["report_hash"]
        assert {"id", "run_id", "status", "items", "artifact_id", "bytes", "created_at"} <= set(done)


def test_mixed_export_partial_pdf_unavailable_and_bundle_incomplete(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        _, export = _export(
            app, client, run_id,
            ["report_json", "report_markdown", "bundle_zip", "report_pdf"],
            "mixed",
        )
        assert export["status"] == "partial"
        by_format = {item["format"]: item for item in export["items"]}
        assert by_format["report_json"]["status"] == "ready"
        assert by_format["report_markdown"]["status"] == "ready"
        # No registered evidence exists for controlled smoke; the ZIP must
        # refuse rather than claim a complete reproduction bundle.
        assert by_format["bundle_zip"]["status"] == "failed"
        assert by_format["bundle_zip"]["error"]["code"] == "EXPORT_INCOMPLETE"
        assert by_format["report_pdf"]["status"] == "failed"
        assert by_format["report_pdf"]["error"]["code"] == "PDF_UNAVAILABLE"


@pytest.mark.parametrize("endpoint", ["download", "text"])
def test_export_rejects_item_mime_inconsistent_with_format(seeded, endpoint):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        _, export = _export(app, client, run_id, ["report_markdown"], "mime-mismatch")
        artifact_id = export["items"][0]["artifact_id"]
        with app.state.runtime["session_factory"].begin() as db:
            item = db.scalar(select(ExportItemRecord).where(ExportItemRecord.artifact_id == artifact_id))
            item.media_type = "text/html"
        response = client.get(f"/api/v1/artifacts/{artifact_id}/{endpoint}", headers={"X-Actor-ID": "owner"})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "ARTIFACT_INTEGRITY_FAILED"


def test_export_items_publish_safe_download_registration(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        _, export = _export(app, client, run_id, ["report_json", "report_markdown"], "registration")
        expected = {"report_json": ("application/json", "report.json"), "report_markdown": ("text/markdown", "report.md")}
        for item in export["items"]:
            assert item["sha256"] == item["artifact_id"]
            assert item["media_type"] == expected[item["format"]][0]
            assert item["filename"] == expected[item["format"]][1]
            content = client.get(f"/api/v1/artifacts/{item['artifact_id']}/download", headers={"X-Actor-ID": "owner"}).content
            assert item["bytes"] == len(content)
            assert hashlib.sha256(content).hexdigest() == item["sha256"]
            assert "storage_key" not in item


def test_export_reuse_verifies_and_repairs_damaged_cache(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        _, first = _export(app, client, run_id, ["report_json", "report_markdown"], "reuse-1")
        first_ids = {i["format"]: i["artifact_id"] for i in first["items"]}
        assert all(first_ids.values())
        _, second = _export(app, client, run_id, ["report_json", "report_markdown"], "reuse-2")
        second_ids = {i["format"]: i["artifact_id"] for i in second["items"]}
        assert second_ids == first_ids

        # Damage one cached reuse record: metadata mismatch must trigger a
        # verified rebuild, not blind reuse of the old ready flag.
        factory = app.state.runtime["session_factory"]
        with factory.begin() as db:
            record = db.get(ExportArtifactRecord, _reuse_key_for(run_id, first, "report_json"))
            assert record is not None
            record.media_type = "text/evil"
            record.filename = "../../evil.txt"
        _, third = _export(app, client, run_id, ["report_json", "report_markdown"], "reuse-3")
        assert {i["format"]: i["artifact_id"] for i in third["items"]} == first_ids
        with factory() as db:
            record = db.get(ExportArtifactRecord, _reuse_key_for(run_id, third, "report_json"))
            assert record.media_type == "application/json"
            assert record.filename == "report.json"
            assert record.rules_version == "guided-export-v1"


def _reuse_key_for(run_id, export_row, fmt):
    from p2r_orchestration.export_jobs import reuse_key
    return reuse_key(run_id, export_row["report_hash"], fmt)


def test_bundle_with_registered_same_run_evidence_succeeds(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        store = app.state.runtime["artifact_store"]
        evidence_bytes = b"registered controlled-smoke evidence"
        address = store.put_bytes(evidence_bytes)
        entry = {
            "run_id": run_id,
            "sha256": address.sha256,
            "bytes": address.bytes,
            "media_type": "text/plain",
        }
        _set_report_content(app, run_id, lambda content: content.update(evidence=[entry]))
        _, export = _export(app, client, run_id, ["bundle_zip"], "bundle-evidence")
        assert export["status"] == "succeeded"
        artifact_id = export["items"][0]["artifact_id"]
        download = client.get(f"/api/v1/artifacts/{artifact_id}/download", headers={"X-Actor-ID": "owner"})
        assert download.status_code == 200
        assert download.headers["content-disposition"] == 'attachment; filename="report-bundle.zip"'
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            names = archive.namelist()
            assert f"evidence/{address.sha256}.txt" in names
            assert archive.read(f"evidence/{address.sha256}.txt") == evidence_bytes
            assert "checksums.json" in names
            assert not any(name.startswith("/") or ".." in name or "\\" in name for name in names)


def test_bundle_rejects_cross_run_evidence(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        store = app.state.runtime["artifact_store"]
        address = store.put_bytes(b"other run evidence")
        entry = {
            "run_id": "run_11111111-1111-1111-1111-111111111111",
            "sha256": address.sha256,
            "bytes": address.bytes,
            "media_type": "text/plain",
        }
        _set_report_content(app, run_id, lambda content: content.update(evidence=[entry]))
        _, export = _export(app, client, run_id, ["report_json", "bundle_zip"], "bundle-cross-run")
        assert export["status"] == "partial"
        by_format = {item["format"]: item for item in export["items"]}
        assert by_format["report_json"]["status"] == "ready"
        assert by_format["bundle_zip"]["status"] == "failed"
        assert by_format["bundle_zip"]["error"]["code"] == "EXPORT_EVIDENCE_INVALID"


def test_bundle_rejects_corrupt_registered_evidence(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        store = app.state.runtime["artifact_store"]
        evidence_bytes = b"soon to be corrupt"
        address = store.put_bytes(evidence_bytes)
        entry = {"run_id": run_id, "sha256": address.sha256, "bytes": address.bytes, "media_type": "text/plain"}
        _set_report_content(app, run_id, lambda content: content.update(evidence=[entry]))
        store._path(address.sha256).write_bytes(b"tampered")
        _, export = _export(app, client, run_id, ["report_json", "bundle_zip"], "bundle-corrupt-evidence")
        by_format = {item["format"]: item for item in export["items"]}
        assert by_format["report_json"]["status"] == "ready"
        assert by_format["bundle_zip"]["status"] == "failed"
        assert by_format["bundle_zip"]["error"]["code"] == "EXPORT_EVIDENCE_INVALID"


def test_bundle_rejects_missing_required_fields(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        _set_report_content(app, run_id, lambda content: content.pop("manifest"))
        _, export = _export(app, client, run_id, ["report_json", "bundle_zip"], "bundle-missing-manifest")
        by_format = {item["format"]: item for item in export["items"]}
        assert by_format["report_json"]["status"] == "ready"
        assert by_format["bundle_zip"]["status"] == "failed"
        assert by_format["bundle_zip"]["error"]["code"] == "EXPORT_INPUT_INVALID"


def test_export_restart_mid_flight_recovers(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        response = client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("restart-export"),
            json={"formats": ["report_json", "report_markdown"]},
        )
        assert response.status_code == 202
        factory = app.state.runtime["session_factory"]
        with factory.begin() as db:
            claimed = JobRepository(db).claim(
                worker_id="crashed-worker",
                kinds=("export_bundle", "export_report_json"),
            )
            assert claimed is not None
            claimed.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert JobRepository(db).requeue_expired() == 1
        assert process_next_run_job(factory, app.state.runtime["artifact_store"])
        exports = client.get(f"/api/v1/runs/{run_id}/exports", headers={"X-Actor-ID": "owner"}).json()["data"]
        assert exports[0]["status"] == "succeeded"
        with factory() as db:
            job = db.scalar(select(Job).where(Job.kind == "export_bundle"))
            assert job.status == "succeeded"


def test_export_idempotency_same_key_and_conflict(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_id = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_id)
        body = {"formats": ["report_json"]}
        first = client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("idem-1"), json=body)
        repeat = client.post(f"/api/v1/runs/{run_id}/exports", headers=_headers("idem-1"), json=body)
        assert first.status_code == repeat.status_code == 202
        assert first.json()["data"]["export_id"] == repeat.json()["data"]["export_id"]
        conflict = client.post(
            f"/api/v1/runs/{run_id}/exports",
            headers=_headers("idem-1"),
            json={"formats": ["report_markdown"]},
        )
        assert conflict.status_code == 409


def test_same_hash_registration_is_evaluated_per_run_permission(seeded):
    app, ids = seeded
    with TestClient(app, base_url="https://testserver") as client:
        run_a = _prepare_run(app, client, ids)
        assert process_next_run_job(app.state.runtime["session_factory"])
        _prepare_report(client, app, run_a)

        factory = app.state.runtime["session_factory"]
        # Register one shared content-addressed artifact under two Runs, one in
        # an inaccessible project and one in an accessible project. The first
        # (inaccessible) registration must not deny the legitimate one.
        content = b"shared registered export bytes"
        address = app.state.runtime["artifact_store"].put_bytes(content)
        with factory.begin() as db:
            run_b = RunRecord(
                project_id=ids["other"],
                experiment_id=db.get(RunRecord, run_a).experiment_id,
                revision_id=db.get(RunRecord, run_a).revision_id,
                status="completed",
                manifest_hash="0" * 64,
            )
            db.add(run_b)
            db.flush()
            exports = []
            for run in (db.get(RunRecord, run_a), run_b):
                export = ExportRecord(run_id=run.id, status="succeeded", requested_formats_json=["report_json"])
                db.add(export)
                db.flush()
                exports.append(export)
                db.add(ExportItemRecord(
                    export_id=export.id,
                    format="report_json",
                    status="ready",
                    artifact_id=address.sha256,
                    storage_key=str(address.path),
                    sha256=address.sha256,
                    bytes=address.bytes,
                    media_type="application/json",
                    filename="report.json",
                ))
            db.delete(db.scalar(select(MembershipRecord).where(
                MembershipRecord.project_id == ids["project"],
                MembershipRecord.user_id == "owner",
            )))
        download = client.get(f"/api/v1/artifacts/{address.sha256}/download", headers={"X-Actor-ID": "owner"})
        assert download.status_code == 200
        assert hashlib.sha256(download.content).hexdigest() == address.sha256
        assert download.headers["content-disposition"] == 'attachment; filename="report.json"'
        text = client.get(f"/api/v1/artifacts/{address.sha256}/text", headers={"X-Actor-ID": "owner"})
        assert text.status_code == 200
        assert text.json()["data"]["text"] == content.decode()
