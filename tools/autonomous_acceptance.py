"""Real HTTP + model + isolated Docker acceptance (never a mocked smoke).

Run with the repository environment, e.g.:
    .venv/bin/python tools/autonomous_acceptance.py
    .venv/bin/python tools/autonomous_acceptance.py --base-url http://127.0.0.1:8000 --pdf

Only P2R_TEST_API_KEY supplies credentials. This program never writes that key.
Default paper ingestion is explicitly external pypdf extraction -> text upload;
--pdf instead exercises the product's online MinerU PDF ingestion. No experiment
code or predictions are supplied by this runner. Existing Docker image / harness
builds are prerequisites; missing prerequisites fail, never trigger a fake run.
Artifacts and failure reports live in a fresh run directory below --output.
Independent method verification additionally requires numpy and scikit-learn in
the acceptance interpreter (python -m pip install numpy scikit-learn). These are
acceptance-only prerequisites, not product runtime dependencies; absence fails.
The checker compares predictions with trusted local StandardScaler/Lars training,
not code identity. No submitted Python is executed by the checker on the host.
"""
from __future__ import annotations

import argparse
import base64
from collections import deque
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import random
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "artifacts"))
from p2r_artifacts.archive_verify import ArchiveVerificationError, verify_export_zip

PAPER_URL = "https://arxiv.org/pdf/math/0406456"
PAPER_SOURCE = "https://arxiv.org/abs/math/0406456"
DATA_ROOT = "https://raw.githubusercontent.com/scikit-learn/scikit-learn/main/sklearn/datasets/data/"
DATA_URL = DATA_ROOT + "diabetes_data_raw.csv.gz"
LABEL_URL = DATA_ROOT + "diabetes_target.csv.gz"
MAX_DOWNLOAD = 32 * 1024 * 1024
MAX_ZIP = 128 * 1024 * 1024
BUNDLED_SKILLS = {"background-training", "result-audit", "research-report", "data-preparation",
                  "implementation-lookup", "paper-analysis", "research-environment", "experiment-debugging"}
EXPECTED = ["train.py", "data.py", "dataset.json", "split.json", "data-provenance.json",
            "requirements.txt", "environment.txt", "run.log", "predictions.json", "labels.json"]


class AcceptanceError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise AcceptanceError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    def bad_constant(_):
        raise AcceptanceError("nonfinite JSON value")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=bad_constant)


def clean(value, secret):
    # Also handles JSON-escaped copies of credentials before any output write.
    text = json.dumps(value, ensure_ascii=False, default=str)
    if secret:
        text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], "[REDACTED]")
        text = text.replace(secret, "[REDACTED]")
    return json.loads(text)


def contains_secret(raw, secret):
    return bool(secret) and any(token in raw for token in
        (secret.encode(), json.dumps(secret, ensure_ascii=False)[1:-1].encode(),
         json.dumps(secret, ensure_ascii=True)[1:-1].encode()))


def scan_local_secrets(root, secret, *, byte_limit=768 * 1024 * 1024):
    """Read-only streaming audit; never print/write matching bytes or follow links."""
    if not secret:
        tokens = []
    else:
        tokens = [secret.encode(), json.dumps(secret, ensure_ascii=False)[1:-1].encode(),
                  json.dumps(secret, ensure_ascii=True)[1:-1].encode()]
    overlap = max(map(len, tokens), default=0) - 1
    total, count, skipped_links = 0, 0, 0
    for directory, directories, filenames in os.walk(root, followlinks=False):
        skipped_links += sum((Path(directory) / name).is_symlink() for name in directories)
        directories[:] = [name for name in directories if not (Path(directory) / name).is_symlink()]
        for name in filenames:
            path = Path(directory) / name
            if path.is_symlink():
                # Workspace venv executables can point outside owned output.
                skipped_links += 1
                continue
            if not path.is_file():
                continue
            count += 1
            require(count <= 10000, "secret audit file bound exceeded")
            tail = b""
            with path.open("rb") as stream:
                while chunk := stream.read(65536):
                    total += len(chunk)
                    require(total <= byte_limit, "secret audit byte bound exceeded")
                    combined = tail + chunk
                    require(not any(token in combined for token in tokens), "credential detected in local output/runtime; inspect privately")
                    tail = combined[-overlap:] if overlap else b""
    return {"files_checked": count, "bytes_checked": total, "symlinks_not_followed": skipped_links}


def build_profile_payload(profile_id, base_url, model, api, secret, *, anonymous=False):
    payload = {"id": profile_id, "base_url": base_url, "model": model, "api": api,
               "auth_mode": "none" if anonymous else "api-key", "max_tokens": 8192}
    if not anonymous:
        payload["api_key"] = secret
    return payload


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--base-url", help="Existing loopback demo API origin; otherwise spawn real factory")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output", type=Path, default=ROOT / ".p2r-e2e-autonomous/fullflow")
    parser.add_argument("--pdf", action="store_true", help="Use product online MinerU PDF path (no text fallback)")
    parser.add_argument("--model", default="zcode/GLM-5.3-Flash")
    parser.add_argument("--model-api", choices=("anthropic-messages", "openai-completions"),
                        default="anthropic-messages")
    parser.add_argument("--specialist-model", action="append", default=[],
                        help="Candidate research specialist model (repeat up to 16); shares main endpoint/API/auth")
    parser.add_argument("--model-base-url", default="http://127.0.0.1:3425")
    parser.add_argument("--allow-unauthenticated-loopback", action="store_true",
                        help="Allow an empty API key only for a loopback HTTP model provider")
    parser.add_argument("--public-download-proxy", help="Explicit HTTP(S) proxy for unauthenticated public downloads only; no URL credentials")
    parser.add_argument("--timeout", type=float, default=1200, help="Task polling deadline in seconds")
    parser.add_argument("--request-timeout", type=float, default=45)
    parser.add_argument("--startup-timeout", type=float, default=90)
    parser.add_argument("--poll-interval", type=float, default=2)
    parser.add_argument("--max-model-calls", type=int, default=80)
    parser.add_argument("--max-model-tokens", type=int, default=600000)
    args = parser.parse_args(argv)
    if len(args.specialist_model) > 16 or any(not model.strip() or len(model) > 256 for model in args.specialist_model):
        parser.error("--specialist-model requires at most 16 nonempty model names, each at most 256 characters")
    args.spawn = args.base_url is None
    for key in ("timeout", "request_timeout", "startup_timeout", "poll_interval"):
        if not math.isfinite(getattr(args, key)) or getattr(args, key) <= 0:
            parser.error(key.replace("_", "-") + " must be positive and finite")
    if not 1 <= args.port <= 65535 or args.max_model_calls <= 0 or args.max_model_tokens <= 0:
        parser.error("invalid port or model budget")
    args.base_url = (args.base_url or f"http://127.0.0.1:{args.port}").rstrip("/")
    parsed = urlsplit(args.base_url)
    if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        parser.error("--base-url must be a loopback HTTP origin, with no path or credentials")
    provider = urlsplit(args.model_base_url)
    if provider.scheme not in ("http", "https") or not provider.hostname or provider.username or provider.password or provider.query or provider.fragment:
        parser.error("--model-base-url must not contain credentials, query or fragment")
    if args.allow_unauthenticated_loopback:
        if provider.scheme != "http" or provider.hostname not in ("127.0.0.1", "localhost", "::1"):
            parser.error("--allow-unauthenticated-loopback requires HTTP with host 127.0.0.1, localhost or ::1")
    if args.public_download_proxy is not None:
        try:
            proxy = urlsplit(args.public_download_proxy)
            valid = (proxy.scheme in ("http", "https") and bool(proxy.hostname)
                     and proxy.username is None and proxy.password is None
                     and proxy.path in ("", "/") and not proxy.query and not proxy.fragment
                     and (proxy.port is None or 1 <= proxy.port <= 65535)
                     and not any(c.isspace() or ord(c) < 32 for c in args.public_download_proxy))
        except ValueError:
            valid = False
        if not valid:
            # Never echo a rejected URL: it may contain proxy credentials.
            parser.error("--public-download-proxy requires an HTTP(S) origin without credentials, query or fragment")
    return args


def split_ids(count=442):
    ids = list(range(count))
    random.Random(42).shuffle(ids)
    return {"seed": 42, "algorithm": "python.random.Random.shuffle; first ceil(0.2*n) test",
            "train": ids[math.ceil(count * .2):], "test": ids[:math.ceil(count * .2)]}


def protocol_and_objective(args, source):
    limits = {"wall_time_seconds": max(1, math.ceil(args.timeout)), "max_model_calls": args.max_model_calls,
              "max_model_tokens": args.max_model_tokens, "storage_bytes": 512 * 1024 * 1024, "gpu_devices": [],
              "max_repair_rounds": 3}
    objective = (
        "Run a real exploratory diabetes regression experiment informed by Least Angle Regression. "
        "Read the supplied full paper context. Inside the isolated Docker target, install numpy and scikit-learn "
        "in a workspace virtual environment; do not assume either is installed. Implement your own separate "
        "data.py and train.py. Use sklearn.datasets.load_diabetes(scaled=False) and a StandardScaler + Lars "
        "pipeline (fit scaler on training data only), n_nonzero_coefs=10. No synthetic data or substituted model. "
        "Use the exact fixed train/test index lists in protocol.parameters, seed 42, preserving original row ids. "
        "Use background for training and poll status/logs to completion, retaining exit code and actual logs. "
        "Save dataset.json as {data: [[10 raw numeric features per row]], target: [442 original targets]}; "
        "split.json must contain seed, train, test from protocol.parameters.split. Save predictions.json as "
        "[{id: original row index as string, prediction: numeric prediction}] and labels.json as "
        "[{id: same index, label: original target}], exactly the held-out test rows. "
        "Save data-provenance.json with loader='sklearn.datasets.load_diabetes', scaled=false, "
        "source_urls containing both URLs in protocol.data_sources, dataset_sha256 (actual dataset.json bytes), "
        "split_sha256 (actual split.json bytes), and sklearn_version. Save requirements.txt with pinned numpy "
        "and scikit-learn versions, environment.txt with actual Python and pip freeze output, and run.log with "
        "real training output. Register EVERY expected artifact using register_artifact: .py as code, "
        "requirements/environment as environment, dataset/split/provenance/labels as data, predictions as result, "
        "run.log as log. Then invoke evaluate and report. Do not merely describe work. Do not change the frozen "
        "protocol; repair code/dependencies as needed within it. Explicitly report exploratory/not_comparable; "
        "there is no paper reference metric and this is not a reproduction of paper results."
    )
    if args.specialist_model:
        objective += (
            " Candidate research specialists are available, not preassigned workers. Explain your choice of "
            "specialist and task type, and use research_delegate for at least one suitable research subtask. "
            "Choose the subtask and specialist yourself based on the experiment's needs. Treat all specialist "
            "responses as unverified advisory material, never as execution or evaluation evidence. You remain "
            "responsible for actual isolated execution, artifacts and independent evaluation requirements above."
        )
    protocol = {"schema_version": "paper2run.protocol.v1", "objective": objective, "mode": "exploratory",
        "evidence": [PAPER_SOURCE, "Original PDF SHA256: " + source["pdf_sha256"], source["ingestion"]],
        "data_sources": [DATA_URL, LABEL_URL], "split_hashes": {},
        "method": "StandardScaler fit only on training rows, then sklearn.linear_model.Lars(n_nonzero_coefs=10)",
        "parameters": {"seed": 42, "scaled": False, "n_nonzero_coefs": 10, "split": split_ids()},
        "entrypoint": "train.py", "expected_artifacts": EXPECTED,
        "evaluation": {"adapter": "regression", "metric": "rmse", "direction": "minimize",
                       "reference_value": None, "tolerance": None},
        "allowed_changes": ["implementation fixes within the frozen method, data and split", "dependency installation"],
        "budget": limits,
        "provenance": {"labels_sha256": None, "labels_independently_frozen": False,
                       "code_sha256": None, "environment_sha256": None}}
    return protocol, objective


def safe_member(name):
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name
            and str(path) == name and not any(ord(c) < 32 for c in name))


def read_verified_zip(raw, secret=""):
    require(len(raw) <= MAX_ZIP, "ZIP exceeds download limit")
    try:
        verify_export_zip(raw)
    except ArchiveVerificationError as exc:
        raise AcceptanceError("ZIP integrity verification failed") from exc
    files = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos = archive.infolist()
        require(len(infos) <= 1024, "too many ZIP members")
        require(sum(i.file_size for i in infos) <= MAX_ZIP, "ZIP uncompressed size exceeds limit")
        for info in infos:
            require(safe_member(info.filename) and not info.is_dir(), "unsafe ZIP member")
            require(info.filename not in files, "duplicate ZIP member")
            require((info.external_attr >> 16) & 0o170000 != 0o120000, "ZIP symlink refused")
            require(info.file_size <= MAX_DOWNLOAD, "ZIP member exceeds limit")
            value = archive.read(info)
            require(not contains_secret(value, secret), "credential detected in export; raw export not saved")
            files[info.filename] = value
    require("manifest.json" in files, "missing manifest.json")
    manifest = strict_json(files["manifest.json"])
    require(isinstance(manifest, dict) and set(manifest) == set(files) - {"manifest.json"}, "manifest file set mismatch")
    for name, metadata in manifest.items():
        require(isinstance(metadata, dict) and metadata.get("sha256") == digest(files[name])
                and metadata.get("bytes") == len(files[name]), "manifest checksum/size mismatch")
    return files


def verify_uploaded_paper(paper, expected_sha256, *, pdf_pages=None):
    require(isinstance(paper, dict) and paper.get("paper_version_id"), "uploaded paper has no version ID")
    require(paper.get("sha256") == expected_sha256, "product paper SHA256 differs from uploaded source")
    if pdf_pages is not None:
        require(type(paper.get("page_count")) is int and paper["page_count"] == pdf_pages,
                "product PDF page count differs from original PDF")


def verify_complete_pdf_parse(status, expected_pages):
    require(status.get("status") == "ready" and status.get("backend") == "online_agent",
            "PDF did not finish online parsing")
    require(type(expected_pages) is int and expected_pages > 0
            and type(status.get("page_count")) is int and status["page_count"] == expected_pages
            and type(status.get("pages_parsed")) is int and status["pages_parsed"] == expected_pages,
            "PDF parsing is partial or page counts differ")
    require(not status.get("error_code"), "ready PDF has a parse error")
    chunks = status.get("chunks")
    require(isinstance(chunks, list) and chunks, "PDF parse chunks missing")
    covered = set()
    for chunk in chunks:
        require(isinstance(chunk, dict) and chunk.get("status") == "done", "PDF chunk not complete")
        first, last = chunk.get("first_page"), chunk.get("last_page")
        require(type(first) is int and type(last) is int and 1 <= first <= last <= expected_pages,
                "PDF chunk page range invalid")
        pages = set(range(first, last + 1))
        require(not covered.intersection(pages), "PDF chunks overlap")
        covered.update(pages)
    require(covered == set(range(1, expected_pages + 1)), "PDF parse has missing page chunks")
    preview = status.get("preview")
    require(isinstance(preview, str) and preview.strip(), "ready PDF has no parsed text preview")


def select_bundled_skills(capabilities):
    """Config takes IDs only; retain live DTO versions/hashes for export attestation."""
    require(isinstance(capabilities, list), "invalid capabilities DTO")
    selected = []
    for item in capabilities:
        require(isinstance(item, dict), "invalid capability entry")
        if item.get("id") not in BUNDLED_SKILLS:
            continue
        require(item.get("source") == "bundled" and item.get("enabled") is True,
                "required built-in skill disabled or replaced")
        require(isinstance(item.get("version"), str) and item["version"]
                and isinstance(item.get("sha256"), str) and len(item["sha256"]) == 64
                and all(c in "0123456789abcdef" for c in item["sha256"])
                and item.get("entrypoint") == "SKILL.md"
                and "paper2run.skills.v1" in item.get("compatibility", []), "invalid built-in skill metadata")
        selected.append(item)
    require(len(selected) == 8 and {item["id"] for item in selected} == BUNDLED_SKILLS,
            "expected exactly eight distinct built-in research skills")
    return sorted(selected, key=lambda item: item["id"])


def verify_skill_snapshots(files, selected):
    selected = select_bundled_skills(selected)
    require("skills.json" in files and "config.json" in files, "skill/config export missing")
    snapshots = strict_json(files["skills.json"])
    require(isinstance(snapshots, list) and len(snapshots) == len(selected), "skill snapshot count mismatch")
    expected_ids = [item["id"] for item in selected]
    require([item.get("id") for item in snapshots] == expected_ids, "skill snapshot selection mismatch")
    config = strict_json(files["config.json"])
    require(config.get("harness", {}).get("skills") == expected_ids, "export config skill selection mismatch")
    manifest_fields = ("id", "version", "name", "description", "entrypoint", "capabilities", "compatibility")
    for snapshot, expected in zip(snapshots, selected):
        require(all(snapshot.get(k) == v for k, v in expected.items()), "skill snapshot metadata/version/hash changed")
        package = snapshot.get("files")
        require(isinstance(package, dict) and "manifest.json" in package and "SKILL.md" in package,
                "skill package content missing")
        require(all(safe_member(name) and isinstance(value, str) for name, value in package.items()),
                "unsafe skill package entry")
        decoded = {name: base64.b64decode(value, validate=True) for name, value in package.items()}
        content = decoded["SKILL.md"].decode("utf-8")
        require(content.strip() and snapshot.get("content") == content, "skill content differs from packaged SKILL.md")
        manifest = strict_json(decoded["manifest.json"])
        require(all(manifest.get(key) == snapshot.get(key) for key in manifest_fields), "skill manifest metadata differs")
        normalized = {key: manifest[key] for key in manifest_fields}
        hash_files = dict(package)
        hash_files["manifest.json"] = normalized
        computed = digest(canonical(hash_files))
        require(computed == expected["sha256"] == snapshot["sha256"], "skill package checksum mismatch")
        require(manifest.get("content_sha256") in (None, computed), "skill manifest checksum mismatch")
    return [{"id": item["id"], "version": item["version"], "sha256": item["sha256"]} for item in selected]


def records(value, field):
    require(isinstance(value, list) and value, "empty evaluation records")
    result = {}
    for row in value:
        require(isinstance(row, dict) and set(row) == {"id", field}, "invalid evaluation record shape")
        key, number = row["id"], row[field]
        require(isinstance(key, str) and key and key not in result, "duplicate or invalid sample id")
        require(type(number) in (int, float) and math.isfinite(number), "nonfinite or invalid evaluation value")
        result[key] = number
    return result


def reference_dependencies():
    """Acceptance-only dependencies; never import from the submitted workspace."""
    try:
        import numpy as np
        import sklearn
        from sklearn.linear_model import Lars
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise AcceptanceError(
            "independent reference method prerequisite missing: numpy and scikit-learn "
            "must be installed in the acceptance interpreter; install with "
            "python -m pip install numpy scikit-learn (no automatic installation)"
        ) from exc
    return np, sklearn, Lars, StandardScaler


def verify_reference_method(predicted, reference_data, reference_labels):
    """Check numerical equivalence, NOT code identity or actual execution history.

    Inputs are the runner's independently acquired public raw data, not artifacts.
    Only this trusted implementation runs locally; submitted code is never loaded.
    Reference predictions remain local and are not included in prompts or results.
    """
    np, sklearn, Lars, StandardScaler = reference_dependencies()
    x = np.asarray(reference_data, dtype=float)
    y = np.asarray(reference_labels, dtype=float)
    require(x.shape == (442, 10) and y.shape == (442,)
            and np.isfinite(x).all() and np.isfinite(y).all(),
            "invalid independent public diabetes reference data")
    split = split_ids()
    train, test = split["train"], split["test"]
    scaler = StandardScaler()
    x_train = scaler.fit_transform(x[train])
    model = Lars(n_nonzero_coefs=10)
    model.fit(x_train, y[train])
    expected = model.predict(scaler.transform(x[test]))
    # |submitted - reference| <= atol + rtol * |reference| for EVERY row.
    # Allows floating-point/library variation, not rounded or merely similar RMSE.
    rtol, atol = 1e-7, 1e-8
    actual = np.asarray([predicted[str(i)] for i in test], dtype=float)
    require(np.isfinite(expected).all() and np.isfinite(actual).all()
            and np.isclose(actual, expected, rtol=rtol, atol=atol).all(),
            "predictions disagree with independent reference method (per-sample numerical equivalence)")
    return {"check_type": "independent_reference_prediction_equivalence",
            "method": "StandardScaler(train-only) + sklearn.linear_model.Lars(n_nonzero_coefs=10)",
            "numpy_version": np.__version__, "sklearn_version": sklearn.__version__,
            "rtol": rtol, "atol": atol, "samples_checked": len(test),
            "code_identity_proven": False}


def verify_delegations(events, configured_specialists, *, primary_model=None):
    """Check SDK observations against a local [{id, model: name, task_types}] ledger.

    Model names attest routing identities only, not the provider's underlying weights.
    Specialist prose is advisory and never substitutes for verify_evidence.
    """
    if not configured_specialists:
        return {"required": False, "successful_delegations": 0}
    expected = {}
    for specialist in configured_specialists:
        require(isinstance(specialist, dict) and isinstance(specialist.get("id"), str)
                and specialist["id"] and specialist["id"] not in expected
                and isinstance(specialist.get("model"), str) and specialist["model"].strip()
                and isinstance(specialist.get("task_types"), list) and specialist["task_types"],
                "invalid configured specialist ledger")
        expected[specialist["id"]] = specialist
    require(isinstance(events, list), "invalid delegation event stream")
    pending, successful = {}, []
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "sdk_event":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") not in (
                "research_delegation_started", "research_delegation_completed"):
            continue
        specialist_id, task_type, model = (payload.get(key) for key in ("specialist_id", "task_type", "model"))
        require(isinstance(specialist_id, str) and specialist_id in expected,
                "delegation used an unconfigured specialist")
        specialist = expected[specialist_id]
        require(isinstance(task_type, str) and task_type in specialist["task_types"]
                and model == specialist["model"], "delegation task type/model differs from configured specialist")
        require(payload.get("evidence_status") == "unverified_advisory", "delegation evidence status is not advisory")
        reason = payload.get("reason")
        require(isinstance(reason, str) and reason.strip(), "delegation selection reason missing")
        identity = (specialist_id, task_type, model)
        if payload["type"] == "research_delegation_started":
            pending.setdefault(identity, deque()).append(payload)
            continue
        require(pending.get(identity), "delegation completion has no matching prior start")
        pending[identity].popleft()
        if payload.get("is_error") or payload.get("error"):
            continue
        text = payload.get("text")
        require(isinstance(text, str) and text.strip(), "delegation completed without advisory response")
        successful.append({"specialist_id": specialist_id, "task_type": task_type, "model": model, "reason": reason})
    require(successful, "no successful SDK research delegation observed")
    observed = sorted({item["model"] for item in successful})
    models = set(observed)
    if primary_model:
        models.add(primary_model)
    return {"required": True, "successful_delegations": len(successful), "delegations": successful,
            "observed_specialist_models": observed, "primary_model": primary_model,
            "model_diversity": "distinct_models_observed" if len(models) > 1 else "same_model_specialists",
            "model_identity_scope": "configured model names only; underlying model weights are not verified",
            "evidence_status": "unverified_advisory"}


def verify_evidence(files, protocol, source, reference_data, reference_labels):
    def document(name):
        require(name in files, "missing export document: " + name)
        return strict_json(files[name])
    frozen = document("protocol.json")
    require(frozen == {"protocol": protocol, "sha256": digest(canonical(protocol))}, "frozen protocol changed")
    task, report = document("task.json"), document("report.json")
    require(task.get("state") == "completed", "task did not complete")
    require(isinstance(report, dict) and report.get("status") == "not_comparable"
            and report.get("verdict") == "not_comparable", "incomplete or misleading report")
    require(not report.get("missing_artifacts") and not report.get("missing_evidence_kinds"), "report has missing evidence")
    # Report digest uses the service's ASCII JSON convention, unlike protocol canonical JSON.
    report_raw = json.dumps({k: v for k, v in report.items() if k != "sha256"},
                            sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    require(report.get("sha256") == digest(report_raw), "report checksum mismatch")
    prefix = "artifacts/" + frozen["sha256"] + "/"
    provenance = document("provenance.json")
    active = {a["name"]: a for a in provenance if a.get("protocol_sha256") == frozen["sha256"]}
    require(set(EXPECTED) <= active.keys(), "expected registered artifacts missing")
    require(sum(a.get("kind") == "code" for a in active.values()) >= 2, "fewer than two code artifacts")
    for name, item in active.items():
        require(prefix + name in files and digest(files[prefix + name]) == item["sha256"], "artifact provenance checksum mismatch")
    for name in EXPECTED:
        require(files[prefix + name].strip(), "empty expected artifact: " + name)
    require(active["train.py"]["kind"] == active["data.py"]["kind"] == "code", "multi-file code not registered")
    data, split, origin = (document(prefix + n) for n in ("dataset.json", "split.json", "data-provenance.json"))
    np, _, _, _ = reference_dependencies()
    reference_x = np.asarray(reference_data, dtype=float)
    reference_y = np.asarray(reference_labels, dtype=float)
    artifact_x = np.asarray(data.get("data"), dtype=float)
    artifact_y = np.asarray(data.get("target"), dtype=float)
    require(artifact_x.shape == reference_x.shape == (442, 10)
            and artifact_y.shape == reference_y.shape == (442,)
            and np.isfinite(artifact_x).all() and np.isfinite(artifact_y).all()
            and np.allclose(artifact_x, reference_x, rtol=0, atol=1e-12)
            and np.array_equal(artifact_y, reference_y),
            "dataset does not match independently downloaded public diabetes source")
    require(all(split.get(k) == v for k, v in split_ids().items() if k != "algorithm"),
            "seed or split differs from frozen protocol")
    require(origin.get("loader") == "sklearn.datasets.load_diabetes" and origin.get("scaled") is False,
            "real dataset loader provenance missing")
    require({DATA_URL, LABEL_URL} <= set(origin.get("source_urls", [])) and origin.get("sklearn_version"), "data sources/version missing")
    require(origin.get("dataset_sha256") == digest(files[prefix + "dataset.json"])
            and origin.get("split_sha256") == digest(files[prefix + "split.json"]), "data provenance hash mismatch")
    predicted, labels = records(document(prefix + "predictions.json"), "prediction"), records(document(prefix + "labels.json"), "label")
    expected_ids = {str(i) for i in split_ids()["test"]}
    require(predicted.keys() == labels.keys() == expected_ids, "held-out sample IDs mismatch")
    require(all(labels[str(i)] == reference_labels[i] for i in split_ids()["test"]), "labels differ from independent public targets")
    method_check = verify_reference_method(predicted, reference_data, reference_labels)
    score = math.hypot(*(predicted[k] - labels[k] for k in sorted(labels))) / math.sqrt(len(labels))
    evaluation = report.get("evaluation", {})
    require(evaluation.get("verdict") == "not_comparable" and evaluation.get("comparison_eligible") is False,
            "report makes an unsupported comparison")
    metrics = evaluation.get("metrics", {})
    require(type(metrics.get("rmse")) in (int, float) and math.isfinite(score)
            and math.isclose(metrics["rmse"], score, rel_tol=1e-10, abs_tol=1e-10)
            and metrics.get("total") == len(labels), "independently recomputed RMSE disagrees with report")
    require(evaluation.get("evaluator_version") == "paper2run.builtin.v2", "trusted evaluator attribution missing")
    errors = [predicted[k] - labels[k] for k in sorted(labels)]
    rng = random.Random(20261104)
    size = len(errors)
    bootstrap = sorted(math.hypot(*(errors[rng.randrange(size)] for _ in range(size))) / math.sqrt(size)
                       for _ in range(2000))
    expected_ci = {"lower": bootstrap[int(.025 * 1999)], "upper": bootstrap[int(.975 * 1999)]}
    intervals = evaluation.get("confidence_intervals")
    interval = intervals.get("rmse_95") if isinstance(intervals, dict) else None
    require(isinstance(interval, dict) and set(interval) == {"lower", "upper"}
            and all(type(interval[key]) in (int, float) and math.isfinite(interval[key])
                    and math.isclose(interval[key], expected_ci[key], rel_tol=1e-10, abs_tol=1e-10)
                    for key in expected_ci), "independently recomputed confidence interval disagrees with report")
    for name in ("predictions.json", "labels.json"):
        require(evaluation.get("artifact_hashes", {}).get(name) == digest(files[prefix + name]), "evaluated artifact hash mismatch")
    events = document("logs/events.json")
    require(any(e.get("type") == "target_prepare" and e.get("payload", {}).get("ready") is True
                and e["payload"].get("container") for e in events), "no ready isolated Docker target observation")
    completed = [e["payload"] for e in events if e.get("type") == "tool_completed" and not e.get("payload", {}).get("is_error")]
    require({"register_artifact", "evaluate", "report", "background"} <= {e.get("name") for e in completed}, "required actual tools not observed")
    starts = {e["payload"]["id"]: e["payload"] for e in events if e.get("type") == "tool_started"}
    jobs = {}
    for event in completed:
        if event.get("name") in ("background", "execute"):
            result = event.get("result", {})
            if result.get("job_id"):
                jobs[result["job_id"]] = starts.get(event.get("id"), {}).get("arguments", {}).get("command", "")
    successful = [e.get("result", {}) for e in completed if e.get("name") in ("execute", "status")
                  and e.get("result", {}).get("exit_code") == 0
                  and e.get("result", {}).get("state") in ("completed", "succeeded", "exited")]
    require(any("train.py" in jobs.get(r.get("job_id"), "") for r in successful), "no successful real training command")
    require(any("pip" in jobs.get(r.get("job_id"), "") and "install" in jobs.get(r.get("job_id"), "") for r in successful), "no successful dependency install command")
    requirements = files[prefix + "requirements.txt"].decode()
    environment = files[prefix + "environment.txt"].decode()
    require(all(name + "==" in requirements and name + "==" in environment for name in ("numpy", "scikit-learn")), "pinned dependencies/environment missing")
    context = document("paper-context.json")
    require(context.get("paper_available") and context.get("text"), "real paper context missing")
    require(context.get("source_sha256") == source["uploaded_sha256"], "uploaded paper hash mismatch")
    return {"rmse": score, "confidence_intervals": {"rmse_95": expected_ci},
            "test_rows": len(labels), "dataset_rows": len(reference_labels),
            "code_files": sum(a["kind"] == "code" for a in active.values()), "zip_members": len(files),
            "verdict": "not_comparable", "independent_public_data_check": True,
            "reference_method_check": method_check}


class Runner:
    def configure_model_profiles(self, profile_id):
        self.api("POST", "/autonomous/profiles", json=build_profile_payload(
            profile_id, self.args.model_base_url, self.args.model, self.args.model_api, self.secret,
            anonymous=self.args.allow_unauthenticated_loopback))
        specialists = []
        for index, model in enumerate(self.args.specialist_model, 1):
            specialist_id = f"expert-{index}"
            specialist_profile = profile_id + "-" + specialist_id
            payload = {"id": specialist_profile, "base_url": self.args.model_base_url,
                       "model": model, "api": self.args.model_api, "max_tokens": 8192,
                       "auth_mode": "none" if self.args.allow_unauthenticated_loopback else "api-key"}
            if not self.args.allow_unauthenticated_loopback:
                payload["reuse_credentials_from"] = profile_id
            self.api("POST", "/autonomous/profiles", json=payload)
            specialists.append({"id": specialist_id,
                "description": f"Candidate model requested for research: {model}; main model chooses suitable subtasks.",
                "task_types": ["paper_analysis", "method_design", "coding", "data_analysis", "review"],
                "model": {"provider_profile": specialist_profile, "credential_ref": "profile:" + specialist_profile}})
        return specialists

    def __init__(self, args, secret):
        import httpx
        self.args, self.secret = args, secret
        root = args.output.resolve()
        require(root.is_relative_to(ROOT.resolve()), "--output must stay inside the workspace")
        self.output = root / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
        self.output.mkdir(parents=True, exist_ok=False)
        self.client = httpx.Client(base_url=args.base_url, timeout=args.request_timeout, trust_env=False,
                                   headers={"Origin": args.base_url, "Sec-Fetch-Site": "same-origin"})
        self.events = deque(maxlen=1000)
        self.logs = deque(maxlen=200)
        self.cursor, self.task_id, self.process = 0, None, None
        self.deadline = None
        self.leak = False
        self.summary = {"ok": False, "scenario": "diabetes-lars", "output": str(self.output), "stages": []}

    def save(self, name, data):
        value = clean(data, self.secret)
        (self.output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def stage(self, name):
        self.summary["stages"].append(name)
        print("[autonomous acceptance] " + name, flush=True)

    def raw(self, method, path, *, cap=MAX_DOWNLOAD, **kwargs):
        headers = kwargs.pop("headers", {})
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid4())
        deadline = min(time.monotonic() + self.args.request_timeout, self.deadline or float("inf"))
        remaining = deadline - time.monotonic()
        require(remaining > 0, "HTTP/task deadline exceeded")
        with self.client.stream(method, path, headers=headers, timeout=remaining, **kwargs) as response:
            pieces, size = [], 0
            for chunk in response.iter_bytes():
                require(time.monotonic() <= deadline, "HTTP/task deadline exceeded")
                size += len(chunk)
                require(size <= cap, "HTTP response exceeds bound")
                pieces.append(chunk)
            raw = b"".join(pieces)
            if contains_secret(raw, self.secret):
                self.leak = True
                raise AcceptanceError("credential detected in HTTP response; not saved")
            if response.is_error:
                # No response body printing: validation responses can echo inputs.
                code = "HTTP_ERROR"
                try:
                    body = strict_json(raw)
                    candidate = body.get("error", {}).get("code", "HTTP_ERROR")
                    if isinstance(candidate, str) and candidate.replace("_", "").isalnum() and len(candidate) < 80:
                        code = candidate
                except (ValueError, AttributeError, AcceptanceError):
                    pass
                hint = "; server allowed_origin must match actual service port" if code == "CSRF_FAILED" else ""
                raise AcceptanceError(f"{method} {path.split('?')[0]}: HTTP {response.status_code} {code}{hint}")
            return raw

    def api(self, method, path, **kwargs):
        body = strict_json(self.raw(method, "/api/v1" + path, **kwargs))
        require(isinstance(body, dict) and "data" in body, "API envelope missing data")
        return body["data"]

    def download(self, url):
        import httpx
        # Public downloads do not inherit API headers/cookies or provider keys.
        deadline = time.monotonic() + self.args.request_timeout
        with httpx.Client(timeout=self.args.request_timeout, follow_redirects=True, trust_env=False,
                          proxy=self.args.public_download_proxy) as client:
            with client.stream("GET", url) as response:
                require(response.status_code == 200, "public source download failed: HTTP " + str(response.status_code))
                pieces, size = [], 0
                for chunk in response.iter_bytes():
                    require(time.monotonic() <= deadline, "public download deadline exceeded")
                    size += len(chunk)
                    require(size <= MAX_DOWNLOAD, "public download exceeds limit")
                    pieces.append(chunk)
                raw = b"".join(pieces)
                require(not contains_secret(raw, self.secret), "credential detected in public source")
                return raw

    def start(self):
        if not self.args.spawn:
            self.stage("connecting to existing demo API")
            self.raw("GET", "/healthz")
            return
        self.stage("starting real create_demo_app uvicorn process")
        parsed = urlsplit(self.args.base_url)
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", parsed.port or 80))
            except OSError as exc:
                raise AcceptanceError("selected API port is occupied; use --base-url explicitly or choose --port") from exc
        env = os.environ.copy()
        env.pop("P2R_TEST_API_KEY", None)
        (self.output / "runtime").mkdir()
        env["P2R_DATABASE_URL"] = "sqlite:///" + str(self.output / "runtime/import.sqlite3")
        env["P2R_DEMO_DIR"] = str(self.output / "runtime")
        env["P2R_DOCPARSE_DIR"] = str(self.output / "runtime/docparse")
        env["P2R_AUTONOMOUS_DIR"] = str(self.output / "runtime/autonomous")
        # No subprocess command-line secret, request body logging, or raw log file.
        env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in
            ["services/api", *["packages/" + p for p in ("contracts", "artifacts", "persistence", "sandbox",
             "docparse", "evaluation", "codegen", "runner", "kernel", "orchestration")]])
        self.process = subprocess.Popen([sys.executable, "-m", "uvicorn", "p2r_api.main:create_demo_app",
            "--factory", "--host", "127.0.0.1", "--port", str(parsed.port or 80), "--no-access-log"],
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        def drain():
            while True:
                chunk = self.process.stdout.readline(16384)
                if not chunk:
                    break
                if contains_secret(chunk, self.secret):
                    self.leak = True
                if not chunk.endswith(b"\n") and len(chunk) == 16384:
                    # Do not retain fragments: a key might straddle the read boundary.
                    self.logs.append("[oversized server log line omitted]")
                    tail = chunk[-8192:]
                    while chunk and not chunk.endswith(b"\n"):
                        chunk = self.process.stdout.readline(16384)
                        combined = tail + chunk
                        if contains_secret(combined, self.secret):
                            self.leak = True
                        tail = combined[-8192:]
                else:
                    self.logs.append(clean(chunk.decode("utf-8", "replace"), self.secret))
        self.log_thread = threading.Thread(target=drain, daemon=True)
        self.log_thread.start()
        deadline = time.monotonic() + self.args.startup_timeout
        while time.monotonic() < deadline:
            require(self.process.poll() is None, "demo API exited during startup; see bounded server log")
            try:
                self.raw("GET", "/healthz")
                return
            except Exception:
                time.sleep(min(.5, max(0, deadline - time.monotonic())))
        raise AcceptanceError("demo API startup deadline exceeded")

    def collect_events(self):
        # Drain bounded pages; continuation is retained for the next poll.
        for _ in range(50):
            page = self.api("GET", f"/autonomous/tasks/{self.task_id}/events?after={self.cursor}")
            events, cursor = page["events"], page["next_cursor"]
            require(isinstance(events, list) and type(cursor) is int and cursor >= self.cursor, "invalid event cursor")
            previous = self.cursor
            for event in events:
                require(type(event.get("sequence")) is int and event["sequence"] > previous, "unordered event journal")
                previous = event["sequence"]
                encoded = canonical(event)
                if len(encoded) > 32768:
                    event = {"sequence": previous, "type": event.get("type"), "payload_truncated": True,
                             "payload_preview": encoded[:16000].decode("utf-8", "replace")}
                self.events.append(event)
            require(not events or cursor == previous, "event cursor does not match page")
            self.cursor = cursor
            if len(events) < 200:
                break
        self.save("events.json", {"after": self.cursor, "retained": len(self.events), "events": list(self.events)})

    def run(self):
        reference_dependencies()  # Fail before API/network activity if the oracle cannot run.
        self.start()
        self.stage("creating demo identity and a separate real-paper project")
        bootstrap = self.api("POST", "/demo/bootstrap", json={"fixture_id": "offline-smoke-v1", "source_processing_consent": True})
        self.summary["identity_bootstrap_project_id"] = bootstrap["project_id"]
        project = self.api("POST", "/projects", json={"name": "Autonomous real diabetes LARS acceptance",
            "description": "Real public paper/data; exploratory regression, not comparable to paper results."})
        project_id = project["id"]
        self.summary["project_id"] = project_id
        self.stage("downloading original public paper PDF and independent public dataset")
        pdf = self.download(PAPER_URL)
        require(pdf.startswith(b"%PDF-"), "paper download is not a PDF")
        (self.output / "paper.pdf").write_bytes(pdf)
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf))
        pdf_pages = len(reader.pages)
        require(pdf_pages > 0, "original PDF has no pages")
        reference_data_raw, reference_labels_raw = self.download(DATA_URL), self.download(LABEL_URL)
        reference_data = [[float(v) for v in line.split()] for line in gzip.decompress(reference_data_raw).decode().splitlines() if line.strip()]
        reference_labels = [float(v) for v in gzip.decompress(reference_labels_raw).decode().split()]
        require(len(reference_data) == len(reference_labels) == 442 and all(len(r) == 10 for r in reference_data), "unexpected public diabetes dataset shape")
        (self.output / "reference-data.csv.gz").write_bytes(reference_data_raw)
        (self.output / "reference-target.csv.gz").write_bytes(reference_labels_raw)
        source = {"title": "Least Angle Regression", "paper_url": PAPER_SOURCE, "pdf_url": PAPER_URL,
            "pdf_sha256": digest(pdf), "pages": pdf_pages, "ingestion": "product PDF upload + online MinerU" if self.args.pdf else "external pypdf full-text extraction -> product text upload (NOT product PDF parsing)",
            "reference_data": {"url": DATA_URL, "sha256": digest(reference_data_raw)},
            "reference_labels": {"url": LABEL_URL, "sha256": digest(reference_labels_raw)}}
        self.save("source.json", source)
        self.summary["source"] = source
        self.stage(source["ingestion"])
        if self.args.pdf:
            self.api("PUT", "/docparse/mode", json={"mode": "online"})
            paper = self.api("POST", f"/projects/{project_id}/papers", files={"file": ("least-angle-regression.pdf", pdf, "application/pdf")},
                data={"source_processing_consent": "true", "title_hint": source["title"]})
            source["uploaded_sha256"] = digest(pdf)
            metadata = self.api("GET", f"/papers/{paper['paper_id']}")
            self.save("uploaded-paper.json", metadata)
            verify_uploaded_paper(metadata, source["uploaded_sha256"], pdf_pages=pdf_pages)
            if paper.get("status") != "parsing":
                self.api("POST", f"/papers/{paper['paper_id']}/parse", json={})
            deadline = time.monotonic() + self.args.timeout
            while time.monotonic() < deadline:
                status = self.api("GET", f"/papers/{paper['paper_id']}/text")
                self.save("pdf-parse-status.json", status)
                if status["status"] == "ready":
                    verify_complete_pdf_parse(status, pdf_pages)
                    break
                require(status["status"] not in ("failed", "error"), "online PDF parse failed; no text fallback")
                time.sleep(self.args.poll_interval)
            else:
                raise AcceptanceError("online PDF parse deadline exceeded")
        else:
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
            require(len(text) > 10000 and "least angle" in text.lower(), "paper full-text extraction incomplete or unexpected")
            require(not contains_secret(text.encode(), self.secret), "credential detected in paper text")
            (self.output / "paper-external-extraction.txt").write_text(text, encoding="utf-8")
            source.update({"pages": len(reader.pages), "text_characters": len(text), "uploaded_sha256": digest(text.encode())})
            paper = self.api("POST", f"/projects/{project_id}/papers/text", json={"text": text,
                "source_processing_consent": True, "title_hint": source["title"]})
        paper = self.api("GET", f"/papers/{paper['paper_id']}")
        self.save("uploaded-paper.json", paper)
        verify_uploaded_paper(paper, source["uploaded_sha256"], pdf_pages=pdf_pages if self.args.pdf else None)
        source["product_source_sha256_verified"] = True
        source["paper_version_id"] = paper["paper_version_id"]
        self.save("source.json", source)
        self.summary["source"] = source
        protocol, objective = protocol_and_objective(self.args, source)
        self.save("requested-protocol.json", protocol)
        profile_id = "acceptance-" + uuid4().hex[:12]
        self.stage("selecting all eight enabled built-in research skills from live capabilities")
        selected_skills = select_bundled_skills(self.api("GET", "/autonomous/capabilities"))
        self.save("selected-skills.json", selected_skills)
        self.stage("configuring runtime-only model profile and frozen protocol")
        specialists = self.configure_model_profiles(profile_id)
        config = {"schema_version": "paper2run.project.v1", "project": {"id": project_id, "workspace": "experiment"},
            "model": {"provider_profile": profile_id, "credential_ref": "profile:" + profile_id},
            "specialists": specialists,
            "harness": {"adapter": "pi", "skills": [item["id"] for item in selected_skills]},
            "target": {"kind": "local", "profile": "local-docker", "allocation": "dedicated", "isolation": "required", "image": "paper2run-experiment:1"},
            "limits": protocol["budget"], "network": {"public_internet": True, "host_and_metadata_access": False},
            "experiment": {"objective": objective, "protocol_change_policy": "version_and_pause_if_outside_authorization"}}
        self.api("PUT", f"/autonomous/projects/{project_id}/config", json={"config": config})
        self.stage("starting actual model-controlled Docker experiment")
        task = self.api("POST", f"/autonomous/projects/{project_id}/tasks", json={"objective": objective,
            "paper_version_id": paper["paper_version_id"], "protocol": protocol, "authorization": {"confirmed": True}})
        self.task_id = task["id"]
        self.summary["task_id"] = self.task_id
        deadline, previous, heartbeat = time.monotonic() + self.args.timeout, None, 0
        self.deadline = deadline
        while time.monotonic() < deadline:
            self.collect_events()
            task = self.api("GET", f"/autonomous/tasks/{self.task_id}")
            self.save("task.json", task)
            state = task["state"]
            if state != previous or time.monotonic() - heartbeat >= 20:
                self.stage(f"task state={state}; observed event cursor={self.cursor}")
                previous, heartbeat = state, time.monotonic()
            if state == "completed":
                break
            require(state in ("queued", "running"), "task requires attention or ended unsuccessfully: " + state)
            time.sleep(self.args.poll_interval)
        else:
            raise AcceptanceError("task polling deadline exceeded")
        self.deadline = None
        self.collect_events()
        self.stage("exporting ZIP and verifying checksums, real data, commands and independent RMSE")
        raw = self.raw("GET", f"/api/v1/autonomous/tasks/{self.task_id}/export", cap=MAX_ZIP)
        files = read_verified_zip(raw, self.secret)
        (self.output / "evidence.zip").write_bytes(raw)
        self.summary["zip_sha256"] = digest(raw)
        self.summary["checks"] = verify_evidence(files, protocol, source, reference_data, reference_labels)
        self.summary["checks"]["verified_skills"] = verify_skill_snapshots(files, selected_skills)
        configured_specialists = [{"id": item["id"], "task_types": item["task_types"], "model": model}
                                  for item, model in zip(specialists, self.args.specialist_model)]
        self.summary["checks"]["delegations"] = verify_delegations(
            strict_json(files["logs/events.json"]), configured_specialists, primary_model=self.args.model)
        self.save("independent-evaluation.json", self.summary["checks"])
        require(not self.leak, "credential detected in server output")
        self.summary["ok"] = True

    def finish(self):
        self.deadline = None
        if self.task_id and not self.summary["ok"]:
            try:
                self.api("POST", f"/autonomous/tasks/{self.task_id}/control", json={"action": "stop"})
                self.collect_events()
                task = self.api("GET", f"/autonomous/tasks/{self.task_id}")
                self.save("task.json", task)
                # Preserve incomplete genuine evidence, but never an unchecked archive.
                raw = self.raw("GET", f"/api/v1/autonomous/tasks/{self.task_id}/export", cap=MAX_ZIP)
                read_verified_zip(raw, self.secret)
                (self.output / "failure-evidence.zip").write_bytes(raw)
            except Exception as exc:
                self.summary["failure_capture_error"] = type(exc).__name__
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.log_thread.join(timeout=2)
        self.client.close()
        self.save("server-log.json", list(self.logs))
        try:
            self.summary["local_secret_audit"] = scan_local_secrets(self.output, self.secret)
            self.summary["secret_audit_scope"] = ("export, HTTP responses, server logs and owned local runtime" if self.args.spawn
                else "export, HTTP responses and runner output; existing server filesystem not accessible through HTTP")
        except AcceptanceError as exc:
            self.summary.update(ok=False, secret_audit_error=str(exc))
        if self.leak:
            self.summary.update(ok=False, credential_leak_detected=True)
        self.save("result.json", self.summary)
        self.save("events.json", {"after": self.cursor, "retained": len(self.events), "events": list(self.events)})


def main(argv=None):
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(actual_argv)
    secret = os.environ.get("P2R_TEST_API_KEY", "")
    if not secret.strip():
        if not args.allow_unauthenticated_loopback:
            print("REFUSED: P2R_TEST_API_KEY is required; no run performed, no skip/pass reported.", file=sys.stderr)
            return 2
        secret = ""
    if len(secret) > 4096 or any(ord(c) < 32 for c in secret):
        print("REFUSED: invalid P2R_TEST_API_KEY format.", file=sys.stderr)
        return 2
    runner = None
    try:
        runner = Runner(args, secret)
        runner.run()
    except KeyboardInterrupt:
        if runner:
            runner.summary["error"] = "interrupted"
    except Exception as exc:
        if runner:
            runner.summary["error"] = clean(str(exc), secret)[:4000]
            runner.summary["error_type"] = type(exc).__name__
        print("[autonomous acceptance] failed: " + clean(str(exc), secret)[:1000], file=sys.stderr)
    finally:
        if runner:
            try:
                runner.finish()
            except Exception as exc:
                runner.summary.update(ok=False, cleanup_error=type(exc).__name__)
                runner.save("result.json", runner.summary)
    if runner:
        print("[autonomous acceptance] " + ("PASS" if runner.summary["ok"] else "FAIL") + "; report=" + str(runner.output / "result.json"))
        return 0 if runner.summary["ok"] else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
