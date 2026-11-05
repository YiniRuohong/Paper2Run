import hashlib
import io
import json
import sys
import zipfile
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packages" / "artifacts"))
from p2r_artifacts.archive_verify import ArchiveVerificationError, verify_export_zip


def archive(files, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as zf:
        for name, data in files:
            zf.writestr(name, data)
    return output.getvalue()


def test_autonomous_manifest_verifies_and_returns_metadata():
    payload = b"result"
    manifest = {"result.txt": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload)}}
    report = verify_export_zip(archive([("result.txt", payload), ("manifest.json", json.dumps(manifest).encode())]))
    assert report["files"]["result.txt"]["bytes"] == len(payload)
    assert len(report["manifest_sha256"]) == 64


def test_guided_checksums_verifies_without_hashing_manifest_or_checksums():
    payloads = [("report.md", b"ok"), ("manifest.json", b"{}")]
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in payloads}
    blob = archive(payloads + [("checksums.json", json.dumps(checksums).encode())])
    assert set(verify_export_zip(blob)["files"]) == {"report.md", "manifest.json"}


@pytest.mark.parametrize("entries", [
    [("a", b"1"), ("a", b"2")],
    [("../a", b"x")],
    [("a//b", b"x")],
    [("a\\b", b"x")],
    [("a:b", b"x")],
])
def test_rejects_duplicate_or_noncanonical_names(entries):
    payloads = [(name, data) for name, data in entries if name != "../a"]
    manifest = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in payloads}
    blob = archive(entries + [("manifest.json", json.dumps(manifest).encode())])
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(blob)


def test_rejects_duplicate_json_keys_and_hash_mismatch():
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(archive([("manifest.json", b'{"a":{"sha256":"x","bytes":1},"a":{"sha256":"x","bytes":1}}'), ("a", b"x")]))
    payload = b"changed"
    manifest = {"a": {"sha256": "0" * 64, "bytes": len(payload)}}
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(archive([("a", payload), ("manifest.json", json.dumps(manifest).encode())]))


def valid_blob():
    payload = b"x"
    manifest = {"a": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": 1}}
    return archive([("a", payload), ("manifest.json", json.dumps(manifest).encode())])


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e309", "-1e309"])
def test_rejects_nonstandard_json_constants(constant):
    payloads = [("manifest.json", ('{"value":' + constant + '}').encode())]
    checksums = {name: hashlib.sha256(data).hexdigest() for name, data in payloads}
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(archive(payloads + [("checksums.json", json.dumps(checksums).encode())]))


def test_path_input_does_not_use_read_bytes(tmp_path, monkeypatch):
    path = tmp_path / "export.zip"
    path.write_bytes(valid_blob())
    def forbidden(*args):
        raise AssertionError("unbounded read_bytes")
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert verify_export_zip(path)["files"]["a"]["bytes"] == 1


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_MEMBERS", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES"])
@pytest.mark.parametrize("as_path", [False, True])
def test_limits_rejected_for_bytes_and_paths(tmp_path, monkeypatch, limit, as_path):
    import p2r_artifacts.archive_verify as verifier
    blob = valid_blob()
    path = tmp_path / "export.zip"
    path.write_bytes(blob)
    monkeypatch.setattr(verifier, limit, 0)
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(path if as_path else blob)


@pytest.mark.parametrize("kind", [0o120000, 0o010000, 0o060000, 0o040000])
def test_rejects_special_file_modes(kind):
    info = zipfile.ZipInfo("a")
    info.create_system = 3
    info.external_attr = (kind | 0o644) << 16
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(archive([(info, b"x"), ("manifest.json", b"{}")]))


def test_rejects_encryption_flag():
    import struct
    blob = bytearray(valid_blob())
    for marker, offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
        pos = blob.index(marker)
        struct.pack_into("<H", blob, pos + offset, 1)
    with pytest.raises(ArchiveVerificationError, match="encrypted"):
        verify_export_zip(blob)


@pytest.mark.parametrize("corruption", ["crc", "compression"])
def test_manifest_read_errors_are_normalized(corruption):
    import struct
    blob = bytearray(archive([("manifest.json", b"{}")]))
    local = blob.index(b"PK\x03\x04")
    central = blob.index(b"PK\x01\x02")
    if corruption == "compression":
        struct.pack_into("<H", blob, local + 8, 99)
        struct.pack_into("<H", blob, central + 10, 99)
    else:
        blob[local + 30 + len("manifest.json")] ^= 1
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(blob)


@pytest.mark.parametrize("method", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_rejects_payload_larger_than_forged_central_directory_size(method):
    import struct
    payload = b"x" * (16 * 1024 * 1024)
    manifest = {"a": {"sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 1}}
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=method) as zf:
        zf.writestr("a", payload)
        zf.writestr("manifest.json", json.dumps(manifest).encode())
    blob = bytearray(output.getvalue())
    central = blob.index(b"PK\x01\x02")
    struct.pack_into("<I", blob, central + 16, zlib.crc32(b"x"))
    struct.pack_into("<I", blob, central + 24, 1)
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(blob)


@pytest.mark.parametrize("field", ["name", "compression", "flags"])
def test_rejects_local_header_mismatch(field):
    import struct
    blob = bytearray(valid_blob())
    local = blob.index(b"PK\x03\x04")
    if field == "name":
        blob[local + 30] ^= 1
    elif field == "compression":
        struct.pack_into("<H", blob, local + 8, zipfile.ZIP_DEFLATED)
    else:
        struct.pack_into("<H", blob, local + 6, 8)
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(blob)


def test_rejects_payload_over_stream_limit_before_materializing(monkeypatch):
    import p2r_artifacts.archive_verify as verifier
    payload = b"x" * 10000
    manifest = {"a": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": 4096}}
    blob = bytearray(archive([("a", payload), ("manifest.json", json.dumps(manifest).encode())]))
    central = blob.index(b"PK\x01\x02")
    import struct
    struct.pack_into("<I", blob, central + 24, 4096)
    monkeypatch.setattr(verifier, "MAX_FILE_BYTES", 4096)
    with pytest.raises(ArchiveVerificationError, match="size limit"):
        verify_export_zip(blob)


def test_streamed_size_enforced_even_when_metadata_lies(monkeypatch):
    import p2r_artifacts.archive_verify as verifier
    original = verifier._digest_member
    def forged(archive, info, limit):
        digest, _ = original(archive, info, limit)
        return digest, info.file_size + 1
    monkeypatch.setattr(verifier, "_digest_member", forged)
    with pytest.raises(ArchiveVerificationError, match="streamed size"):
        verify_export_zip(valid_blob())


def test_cli_failure_is_json_and_nonzero(tmp_path):
    import subprocess
    path = tmp_path / "broken.zip"
    path.write_bytes(b"invalid zip")
    result = subprocess.run([sys.executable, str(ROOT / "tools/verify_export.py"), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["ok"] is False
    assert not result.stderr


def _forged_deflate_blob(payload, declared, *, member="a", suffix=b"", truncate=0):
    # Construct a STORED container, then replace its member bytes with raw
    # DEFLATE and patch both headers. No local/central mismatch can mask a bug.
    import struct
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(payload) + compressor.flush()
    compressed = compressed[:-truncate] if truncate else compressed + suffix
    visible = payload[:declared]
    manifest = {member: {"sha256": hashlib.sha256(visible).hexdigest(), "bytes": declared}}
    files = [(member, compressed)]
    if member != "manifest.json":
        files.append(("manifest.json", json.dumps(manifest).encode()))
    blob = bytearray(archive(files))
    central = blob.index(b"PK\x01\x02")
    for base, method_offset, crc_offset, size_offset in [(0, 8, 14, 22), (central, 10, 16, 24)]:
        struct.pack_into("<H", blob, base + method_offset, zipfile.ZIP_DEFLATED)
        struct.pack_into("<I", blob, base + crc_offset, zlib.crc32(visible))
        struct.pack_into("<I", blob, base + size_offset, declared)
    return bytes(blob)


@pytest.mark.parametrize("member", ["a", "manifest.json"])
@pytest.mark.parametrize("declared", [1, 4096])
def test_deflate_real_output_is_bounded_with_matching_forged_headers(monkeypatch, member, declared):
    import tracemalloc
    import p2r_artifacts.archive_verify as verifier
    blob = _forged_deflate_blob(b"x" * (16 * 1024 * 1024), declared, member=member)
    monkeypatch.setattr(verifier, "MAX_FILE_BYTES", 4096)
    # ZipExtFile sees only the forged prefix, demonstrating why counting its
    # output (even when both headers match) cannot establish the true length.
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert zf.read(member) == b"x" * declared
    tracemalloc.start()
    try:
        with pytest.raises(ArchiveVerificationError, match="streamed size"):
            verify_export_zip(blob)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1024 * 1024, f"decoder allocated {peak} bytes for a 4 KiB budget"


@pytest.mark.parametrize("method", [zipfile.ZIP_LZMA, zipfile.ZIP_BZIP2])
def test_rejects_unbounded_compression_methods_before_open(monkeypatch, method):
    import p2r_artifacts.archive_verify as verifier
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=method) as zf:
        zf.writestr("manifest.json", b"{}")
    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported decoder must not be instantiated")
    monkeypatch.setattr(zipfile.ZipFile, "open", forbidden)
    monkeypatch.setattr(verifier, "MAX_FILE_BYTES", 4096)
    with pytest.raises(ArchiveVerificationError, match="unsupported compression"):
        verify_export_zip(output.getvalue())


@pytest.mark.parametrize("corruption", ["truncated", "second_stream", "trailing_bytes"])
def test_rejects_incomplete_or_extra_deflate_stream(corruption):
    kwargs = {"truncate": 1} if corruption == "truncated" else {"suffix": b"\x03\x00" if corruption == "second_stream" else b"junk"}
    blob = _forged_deflate_blob(b"x", 1, **kwargs)
    with pytest.raises(ArchiveVerificationError, match="compressed stream"):
        verify_export_zip(blob)


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("guided", [False, True])
def test_supported_exports_with_multichunk_and_empty_members(compression, guided):
    import random
    payloads = [("large.bin", random.Random(42).randbytes(200000)), ("empty", b"")]
    if guided:
        payloads.append(("manifest.json", b"{}"))
        checksums = {name: hashlib.sha256(data).hexdigest() for name, data in payloads}
        files = payloads + [("checksums.json", json.dumps(checksums).encode())]
    else:
        manifest = {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)} for name, data in payloads}
        files = payloads + [("manifest.json", json.dumps(manifest).encode())]
    report = verify_export_zip(archive(files, compression))
    assert report["files"]["large.bin"]["bytes"] == 200000
    assert report["files"]["empty"]["bytes"] == 0


@pytest.mark.parametrize("member", ["a", "manifest.json"])
def test_rejects_member_span_overlapping_next_header_or_directory(member):
    import struct
    blob = bytearray(valid_blob())
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        infos = zf.infolist()
        info = zf.getinfo(member)
    central = blob.index(b"PK\x01\x02")
    if member == "manifest.json":
        central += 46 + len(infos[0].filename)
    struct.pack_into("<I", blob, central + 20, info.compress_size + 1)
    struct.pack_into("<I", blob, info.header_offset + 18, info.compress_size + 1)
    with pytest.raises(ArchiveVerificationError, match="Overlapped"):
        verify_export_zip(blob)


def test_deflate_declared_size_larger_than_true_output_is_rejected():
    with pytest.raises(ArchiveVerificationError, match="streamed size mismatch"):
        verify_export_zip(_forged_deflate_blob(b"x", 2))


def test_remaining_total_budget_is_passed_and_guided_manifest_is_read_once(monkeypatch):
    import p2r_artifacts.archive_verify as verifier
    payloads = [("manifest.json", b"{}"), ("a", b"ab"), ("b", b"cde")]
    checksums = json.dumps({name: hashlib.sha256(data).hexdigest() for name, data in payloads}).encode()
    total = sum(len(data) for _, data in payloads) + len(checksums)
    monkeypatch.setattr(verifier, "MAX_TOTAL_BYTES", total)
    original = verifier._digest_member
    budgets = []
    def checked(archive, info, limit):
        budgets.append((info.filename, limit))
        return original(archive, info, limit)
    monkeypatch.setattr(verifier, "_digest_member", checked)
    verify_export_zip(archive(payloads + [("checksums.json", checksums)], zipfile.ZIP_DEFLATED))
    assert budgets == [("a", 5), ("b", 3)]
    blob = _forged_deflate_blob(b"x" * 10000, 4096)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        with pytest.raises(ArchiveVerificationError, match="streamed size limit"):
            original(zf, zf.getinfo("a"), 32)


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
@pytest.mark.parametrize("force_zip64", [False, True])
def test_supported_unseekable_writer_and_zip64(compression, force_zip64):
    class Unseekable(io.BytesIO):
        def seek(self, *args):
            raise io.UnsupportedOperation("not seekable")
    output = Unseekable()
    manifest = {"a": {"sha256": hashlib.sha256(b"x").hexdigest(), "bytes": 1}}
    with zipfile.ZipFile(output, "w", compression=compression) as zf:
        for name, data in [("a", b"x"), ("manifest.json", json.dumps(manifest).encode())]:
            with zf.open(name, "w", force_zip64=force_zip64) as member:
                member.write(data)
    assert verify_export_zip(output.getvalue())["files"]["a"]["bytes"] == 1


def test_optional_external_archive_digest_is_trust_anchor():
    payload = b"x"
    manifest = {"a": {"sha256": hashlib.sha256(payload).hexdigest(), "bytes": 1}}
    blob = archive([("a", payload), ("manifest.json", json.dumps(manifest).encode())])
    verify_export_zip(blob, expected_archive_sha256=hashlib.sha256(blob).hexdigest())
    with pytest.raises(ArchiveVerificationError):
        verify_export_zip(blob, expected_archive_sha256="0" * 64)
