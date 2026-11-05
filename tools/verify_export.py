#!/usr/bin/env python3
"""Verify a Paper2Run export ZIP offline."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "artifacts"))
from p2r_artifacts.archive_verify import ArchiveVerificationError, verify_export_zip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--expected-archive-sha256")
    args = parser.parse_args()
    try:
        result = verify_export_zip(args.archive, expected_archive_sha256=args.expected_archive_sha256)
    except ArchiveVerificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
