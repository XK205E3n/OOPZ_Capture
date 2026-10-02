from __future__ import annotations

from pathlib import Path

from oopz_capture.jsonio import atomic_json, read_json


def test_atomic_json_retries_replace_when_a_reader_holds_the_target(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "data.json"
    atomic_json(target, {"n": 1})
    original_replace = Path.replace
    calls = {"denied": 0}

    def flaky_replace(self: Path, other: Path) -> None:
        if self.name.startswith("data.json") and calls["denied"] == 0:
            calls["denied"] += 1
            raise PermissionError(5, "Access is denied")
        return original_replace(self, other)

    monkeypatch.setattr(Path, "replace", flaky_replace)

    atomic_json(target, {"n": 2})

    assert calls["denied"] == 1
    assert read_json(target) == {"n": 2}
