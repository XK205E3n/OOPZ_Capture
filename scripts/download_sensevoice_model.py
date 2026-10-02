from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys


MODEL_ID = "iic/SenseVoiceSmall"
MODEL_REVISION = "7bf452403abd7353a300cd760f7adae7701c92c1"
MODEL_URL = "https://modelscope.cn/models/iic/SenseVoiceSmall"
EXPECTED_SHA256 = {
    "model.pt": "833ca2dcfdf8ec91bd4f31cfac36d6124e0c459074d5e909aec9cabe6204a3ea",
    "am.mvn": "29b3c740a2c0cfc6b308126d31d7f265fa2be74f3bb095cd2f143ea970896ae5",
    "config.yaml": "f71e239ba36705564b5bf2d2ffd07eece07b8e3f2bbf6d2c99d8df856339ac19",
    "configuration.json": "02810a7f8e9e8aee10370a265f7e799728ce25b4c00cdbf4602b303ee395a38e",
    "chn_jpn_yue_eng_ko_spectok.bpe.model": "aa87f86064c3730d799ddf7af3c04659151102cba548bce325cf06ba4da4e6a8",
}


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(target: Path) -> dict[str, str]:
    actual: dict[str, str] = {}
    for relative, expected in EXPECTED_SHA256.items():
        path = target / relative
        if not path.is_file():
            raise RuntimeError(f"model file is missing: {relative}")
        digest = _file_sha256(path)
        if digest != expected:
            raise RuntimeError(
                f"model checksum mismatch: {relative}; expected {expected}, got {digest}"
            )
        actual[relative] = digest
    return actual


def write_source_manifest(target: Path, hashes: dict[str, str]) -> None:
    manifest = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "source_url": MODEL_URL,
        "license": "Apache-2.0",
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "files_sha256": hashes,
    }
    temporary = target / "MODEL_SOURCE.json.tmp"
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(target / "MODEL_SOURCE.json")


def download_model(target: Path) -> str:
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if not target.is_dir():
            raise RuntimeError(f"model target is not a directory: {target}")
        hashes = verify_model(target)
        write_source_manifest(target, hashes)
        return "already-present-and-verified"

    staging = target.with_name(f".{target.name}.download-{MODEL_REVISION[:12]}")
    staging.mkdir(parents=True, exist_ok=True)
    from modelscope import snapshot_download

    snapshot_download(MODEL_ID, revision=MODEL_REVISION, local_dir=str(staging))
    hashes = verify_model(staging)
    write_source_manifest(staging, hashes)
    staging.replace(target)
    return "downloaded-and-verified"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download the pinned SenseVoiceSmall model from ModelScope and verify it."
    )
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args()
    try:
        status = download_model(args.target)
    except Exception as error:
        print(f"SenseVoiceSmall setup failed: {error}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": status,
                "model_id": MODEL_ID,
                "model_revision": MODEL_REVISION,
                "target": str(args.target.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
