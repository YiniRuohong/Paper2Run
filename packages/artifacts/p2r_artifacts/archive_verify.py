"""Bounded, read-only verification of autonomous and guided ZIP exports.

Design reference: https://github.com/PKU-YuanGroup/OpenAI4S/blob/0bc84e6/openai4s/evidence.py.
This implementation is independent and does not establish authorship: an
attacker can rewrite both payload and manifest. Use an independently trusted
archive SHA-256 as an external anchor when authenticity matters.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import struct
import zlib
import zipfile
from pathlib import Path
from typing import Any, BinaryIO

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MANIFEST = "manifest.json"
CHECKSUMS = "checksums.json"


class ArchiveVerificationError(ValueError):
    """The ZIP is unsafe, malformed, or internally inconsistent."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ArchiveVerificationError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ArchiveVerificationError(f"non-standard JSON numeric constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ArchiveVerificationError("JSON number exceeds finite numeric range")
    return number


def _json(data: bytes, label: str) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=_reject_constant, parse_float=_finite_float)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveVerificationError(f"invalid {label}: {exc}") from exc


def _path(name: str) -> None:
    if (not name or name.startswith("/") or "\\" in name or ":" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)):
        raise ArchiveVerificationError(f"non-canonical archive path: {name!r}")
    if any(part in ("", ".", "..") for part in name.split("/")):
        raise ArchiveVerificationError(f"non-canonical archive path: {name!r}")


def _stream_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int, digest=None) -> tuple[bytes | None, int]:
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ArchiveVerificationError(f"unsupported compression method: {info.filename}")
    source = archive.fp
    if source is None:
        raise ArchiveVerificationError("ZIP archive is closed")
    # The declared size is an additional ceiling, never evidence of actual
    # output size. Read at most one extra byte to detect a lying header.
    limit = min(limit, MAX_FILE_BYTES, info.file_size)
    chunks = []
    count = 0
    crc = 0
    remaining = info.compress_size

    def consume(part: bytes) -> None:
        nonlocal count, crc
        count += len(part)
        if count > limit:
            raise ArchiveVerificationError(f"member exceeds streamed size limit: {info.filename}")
        crc = zlib.crc32(part, crc)
        if digest is None:
            chunks.append(part)
        else:
            digest.update(part)

    try:
        # Keep zipfile's filename, unsupported-flag and overlap safeguards, but
        # never read ZipExtFile: it truncates output to the untrusted file_size.
        with archive.open(info, "r"):
            pass
        source.seek(info.header_offset)
        header = source.read(30)
        if len(header) != 30 or header[:4] != b"PK\x03\x04":
            raise ArchiveVerificationError(f"invalid local header: {info.filename}")
        flags, method = struct.unpack_from("<HH", header, 6)
        if flags != info.flag_bits or method != info.compress_type:
            raise ArchiveVerificationError(f"local header mismatch: {info.filename}")
        name_len, extra_len = struct.unpack_from("<HH", header, 26)
        source.seek(name_len + extra_len, 1)
        decoder = zlib.decompressobj(-15) if method == zipfile.ZIP_DEFLATED else None
        while remaining:
            read_size = min(64 * 1024, remaining)
            if decoder is None:
                read_size = min(read_size, limit + 1 - count)
            compressed = source.read(read_size)
            if not compressed:
                raise ArchiveVerificationError(f"truncated compressed data: {info.filename}")
            remaining -= len(compressed)
            if decoder is None:
                consume(compressed)
                continue
            pending = compressed
            while True:
                output_limit = min(64 * 1024, limit + 1 - count)
                part = decoder.decompress(pending, output_limit)
                consume(part)
                pending = decoder.unconsumed_tail
                if decoder.eof:
                    if decoder.unused_data or pending or remaining:
                        raise ArchiveVerificationError(f"extra data after compressed stream: {info.filename}")
                    break
                if not pending and len(part) < output_limit:
                    break
                # Drain buffered output with another bounded decompress call,
                # including empty input if needed. flush(length) is NOT bounded.
        if decoder is not None and not decoder.eof:
            raise ArchiveVerificationError(f"incomplete compressed stream: {info.filename}")
    except ArchiveVerificationError:
        raise
    except Exception as exc:
        raise ArchiveVerificationError(f"cannot read {info.filename}: {exc}") from exc
    if count != info.file_size:
        raise ArchiveVerificationError(f"streamed size mismatch: {info.filename}")
    if crc != info.CRC:
        raise ArchiveVerificationError(f"CRC mismatch: {info.filename}")
    return (b"".join(chunks) if digest is None else None), count


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> tuple[bytes, int]:
    data, count = _stream_member(archive, info, limit)
    return data or b"", count


def _digest_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    _, count = _stream_member(archive, info, limit, digest)
    return digest.hexdigest(), count


def _archive_hash(stream: BinaryIO) -> str:
    digest = hashlib.sha256()
    total = 0
    stream.seek(0)
    while chunk := stream.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_ARCHIVE_BYTES:
            raise ArchiveVerificationError("archive size exceeds limit")
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def verify_export_zip(raw: bytes | bytearray | memoryview | str | Path, *, expected_archive_sha256: str | None = None) -> dict[str, Any]:
    """Verify archive consistency without extracting or executing members."""
    import io

    if isinstance(raw, (str, Path)):
        try:
            path = Path(raw)
            if path.stat().st_size > MAX_ARCHIVE_BYTES:
                raise ArchiveVerificationError("archive size exceeds limit")
            source: BinaryIO = path.open("rb")
        except ArchiveVerificationError:
            raise
        except OSError as exc:
            raise ArchiveVerificationError(f"cannot open archive: {exc}") from exc
    else:
        data = bytes(raw)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise ArchiveVerificationError("archive size exceeds limit")
        source = io.BytesIO(data)

    try:
        archive_digest = _archive_hash(source)
        if expected_archive_sha256 is not None:
            if not isinstance(expected_archive_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_archive_sha256):
                raise ArchiveVerificationError("expected archive SHA-256 must be 64 hex characters")
            if archive_digest != expected_archive_sha256.lower():
                raise ArchiveVerificationError("archive SHA-256 does not match external anchor")
        try:
            archive = zipfile.ZipFile(source)
        except Exception as exc:
            raise ArchiveVerificationError(f"not a readable ZIP: {exc}") from exc
        with archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise ArchiveVerificationError("archive member count exceeds limit")
            by_name = {}
            total = 0
            for info in infos:
                _path(info.orig_filename)
                if info.orig_filename != info.filename:
                    raise ArchiveVerificationError("ZIP member name was normalized")
                if info.filename in by_name:
                    raise ArchiveVerificationError(f"duplicate ZIP member: {info.filename}")
                by_name[info.filename] = info
                if info.flag_bits & 1:
                    raise ArchiveVerificationError(f"encrypted member: {info.filename}")
                kind = stat.S_IFMT(info.external_attr >> 16)
                if kind not in (0, stat.S_IFREG):
                    raise ArchiveVerificationError(f"special file member: {info.filename}")
                if info.is_dir():
                    raise ArchiveVerificationError(f"directory member is not permitted: {info.filename}")
                if info.file_size < 0 or info.file_size > MAX_FILE_BYTES:
                    raise ArchiveVerificationError(f"member size exceeds limit: {info.filename}")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise ArchiveVerificationError("total expanded size exceeds limit")
            if MANIFEST not in by_name:
                raise ArchiveVerificationError("missing manifest.json")
            manifest_data, expanded = _read_member(archive, by_name[MANIFEST], MAX_TOTAL_BYTES)
            verified = {MANIFEST: (hashlib.sha256(manifest_data).hexdigest(), expanded)}
            manifest = _json(manifest_data, MANIFEST)
            if not isinstance(manifest, dict):
                raise ArchiveVerificationError("manifest.json must be an object")
            if CHECKSUMS in by_name:
                checksums_data, size = _read_member(archive, by_name[CHECKSUMS], MAX_TOTAL_BYTES - expanded)
                expanded += size
                checksums = _json(checksums_data, CHECKSUMS)
                if not isinstance(checksums, dict) or not all(isinstance(k, str) and isinstance(v, str) and re.fullmatch(r"[0-9a-f]{64}", v) for k, v in checksums.items()):
                    raise ArchiveVerificationError("checksums.json must map paths to SHA-256 strings")
                listed = checksums
                metadata = {name: {"sha256": value, "bytes": by_name[name].file_size} for name, value in checksums.items() if name in by_name}
                excluded = {CHECKSUMS}
            else:
                listed = manifest
                metadata = {}
                excluded = {MANIFEST}
                for name, entry in manifest.items():
                    if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != {"sha256", "bytes"}:
                        raise ArchiveVerificationError(f"invalid manifest entry: {name!r}")
                    if not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]):
                        raise ArchiveVerificationError(f"invalid SHA-256 for {name!r}")
                    if isinstance(entry["bytes"], bool) or not isinstance(entry["bytes"], int) or entry["bytes"] < 0:
                        raise ArchiveVerificationError(f"invalid byte count for {name!r}")
                    metadata[name] = dict(entry)
            for name in listed:
                _path(name)
                if name in excluded or name not in by_name:
                    raise ArchiveVerificationError(f"listed file missing or reserved: {name}")
            actual_names = set(by_name) - excluded
            if actual_names != set(listed):
                raise ArchiveVerificationError(f"archive file set mismatch; unlisted={sorted(actual_names - set(listed))}")
            for name, entry in listed.items():
                info = by_name[name]
                expected_digest = entry if isinstance(entry, str) else entry["sha256"]
                if isinstance(entry, dict) and entry["bytes"] != info.file_size:
                    raise ArchiveVerificationError(f"size mismatch: {name}")
                if name in verified:
                    digest, actual_size = verified[name]
                else:
                    digest, actual_size = _digest_member(archive, info, MAX_TOTAL_BYTES - expanded)
                    expanded += actual_size
                    if expanded > MAX_TOTAL_BYTES:
                        raise ArchiveVerificationError("total expanded size exceeds limit")
                if isinstance(entry, dict) and entry["bytes"] != actual_size:
                    raise ArchiveVerificationError(f"streamed size mismatch: {name}")
                if digest != expected_digest:
                    raise ArchiveVerificationError(f"SHA-256 mismatch: {name}")
            return {"files": metadata, "manifest_sha256": hashlib.sha256(manifest_data).hexdigest(), "archive_sha256": archive_digest}
    except ArchiveVerificationError:
        raise
    except Exception as exc:
        raise ArchiveVerificationError(f"archive verification failed: {exc}") from exc
    finally:
        source.close()


__all__ = ["ArchiveVerificationError", "verify_export_zip"]
