#!/usr/bin/env python3
"""Verify a release ZIP against its .sha256 sidecar before anything uses it.

Tolerates CRLF checksum files (older builders wrote them). Standalone on
purpose: the OS-level updater runs this before it executes any code from the
newly downloaded package.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path


def verify(artifact: Path, checksum: Path) -> tuple[bool, str]:
    if not artifact.is_file():
        return False, f"artifact is missing: {artifact}"
    if not checksum.is_file():
        return False, f"checksum file is missing: {checksum}"
    expected: str | None = None
    try:
        text = checksum.read_text(encoding="utf-8-sig", errors="strict")
    except OSError as error:
        return False, f"checksum file is unreadable: {error}"
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([0-9A-Fa-f]{64})\s+\*?(.+)$", line)
        if not match:
            return False, f"malformed checksum line: {line[:80]}"
        digest, name = match.groups()
        if name.strip() == artifact.name:
            expected = digest.lower()
            break
        # Ignore lines describing other files in a multi-file checksum list.
    if expected is None:
        return False, f"checksum file has no entry for {artifact.name}"
    digest = hashlib.sha256()
    try:
        with artifact.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        return False, f"artifact is unreadable: {error}"
    actual = digest.hexdigest()
    if actual != expected:
        return False, f"SHA-256 mismatch for {artifact.name}: expected {expected}, got {actual}"
    return True, f"SHA-256 OK: {artifact.name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_artifact.py", description=__doc__.splitlines()[0])
    parser.add_argument("artifact", type=Path, help="release ZIP to verify")
    parser.add_argument("--checksum", type=Path, default=None,
                        help="checksum file (default: <artifact>.sha256)")
    args = parser.parse_args(argv)
    artifact = args.artifact
    checksum = args.checksum if args.checksum is not None else artifact.with_name(artifact.name + ".sha256")
    ok, message = verify(artifact, checksum)
    print(("VERIFY-OK " if ok else "VERIFY-FAIL ") + message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
