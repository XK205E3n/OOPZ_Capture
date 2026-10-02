from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "download_sensevoice_model.py"
SPEC = importlib.util.spec_from_file_location("download_sensevoice_model", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_existing_model_is_verified_and_source_manifest_is_written(tmp_path, monkeypatch):
    target = tmp_path / "SenseVoiceSmall"
    target.mkdir()
    contents = {"model.pt": b"model", "config.yaml": b"config"}
    for name, value in contents.items():
        (target / name).write_bytes(value)
    monkeypatch.setattr(
        module,
        "EXPECTED_SHA256",
        {name: sha256(value).hexdigest() for name, value in contents.items()},
    )

    assert module.download_model(target) == "already-present-and-verified"
    manifest = json.loads((target / "MODEL_SOURCE.json").read_text(encoding="utf-8"))
    assert manifest["model_id"] == "iic/SenseVoiceSmall"
    assert manifest["model_revision"] == module.MODEL_REVISION
    assert manifest["files_sha256"] == module.EXPECTED_SHA256


def test_checksum_mismatch_is_rejected(tmp_path, monkeypatch):
    target = tmp_path / "SenseVoiceSmall"
    target.mkdir()
    (target / "model.pt").write_bytes(b"unexpected")
    monkeypatch.setattr(module, "EXPECTED_SHA256", {"model.pt": "0" * 64})

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        module.download_model(target)


def test_fresh_model_is_downloaded_to_staging_then_promoted(tmp_path, monkeypatch):
    target = tmp_path / "SenseVoiceSmall"
    contents = {"model.pt": b"model", "config.yaml": b"config"}
    monkeypatch.setattr(
        module,
        "EXPECTED_SHA256",
        {name: sha256(value).hexdigest() for name, value in contents.items()},
    )
    calls = []
    fake_modelscope = ModuleType("modelscope")

    def fake_snapshot_download(model_id, *, revision, local_dir):
        calls.append((model_id, revision, Path(local_dir)))
        for name, value in contents.items():
            (Path(local_dir) / name).write_bytes(value)
        return local_dir

    fake_modelscope.snapshot_download = fake_snapshot_download
    monkeypatch.setitem(sys.modules, "modelscope", fake_modelscope)

    assert module.download_model(target) == "downloaded-and-verified"
    assert calls == [
        (
            "iic/SenseVoiceSmall",
            module.MODEL_REVISION,
            target.with_name(f".SenseVoiceSmall.download-{module.MODEL_REVISION[:12]}").resolve(),
        )
    ]
    assert (target / "MODEL_SOURCE.json").is_file()
    assert not calls[0][2].exists()
