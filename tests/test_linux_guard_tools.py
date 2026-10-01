"""Unit behavior tests for the Linux deployment guard/verify/node helpers."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
GUARD = PROJECT_ROOT / "scripts" / "linux" / "check_active_tasks.py"
VERIFY = PROJECT_ROOT / "scripts" / "linux" / "verify_artifact.py"
CHECK_NODE = PROJECT_ROOT / "scripts" / "linux" / "check_node.py"


def run_helper(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )


# --- check_active_tasks ------------------------------------------------------


def make_state(tmp_path: Path, controller: dict | str | None) -> Path:
    state = tmp_path / "feishu_state"
    state.mkdir(parents=True, exist_ok=True)
    if controller is not None:
        payload = controller if isinstance(controller, str) else json.dumps(controller)
        (state / "controller.json").write_text(payload, encoding="utf-8")
    return state


def make_output(tmp_path: Path, locks: dict[str, int | str | None] | None = None) -> Path:
    output = tmp_path / "output"
    output.mkdir(parents=True, exist_ok=True)
    for name, pid in (locks or {}).items():
        session = output / name
        analysis = session / "analysis"
        analysis.mkdir(parents=True, exist_ok=True)
        if pid is None:
            (analysis / ".run.lock").symlink_to("/etc/passwd")
        elif pid == "dir":
            (analysis / ".run.lock").mkdir()
        else:
            (analysis / ".run.lock").write_text(json.dumps({"pid": pid}), encoding="utf-8")
    return output


IDLE = {"schema_version": "x", "active": None, "last_job": None}
ACTIVE = {"schema_version": "x", "active": {"session_id": "s"}, "last_job": None}
ANALYZING = {"schema_version": "x", "active": None, "last_job": {"status": "analyzing"}}


@pytest.mark.parametrize("controller,running,expected", [
    (IDLE, "yes", 0),
    (IDLE, "no", 0),
    (None, "yes", 0),          # no controller file yet: fresh install
    (ANALYZING, "yes", 3),     # background analysis must block (R4)
    (ACTIVE, "yes", 3),
    (ANALYZING, "no", 0),      # service stopped: persisted interruptible state
    (ACTIVE, "no", 0),
])
def test_guard_controller_matrix(tmp_path: Path, controller, running: str, expected: int) -> None:
    state = make_state(tmp_path, controller)
    output = make_output(tmp_path)
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", running)
    assert result.returncode == expected, result.stdout + result.stderr


def test_guard_corrupt_controller_is_undetermined_not_safe(tmp_path: Path) -> None:
    state = make_state(tmp_path, "{not json")
    output = make_output(tmp_path)
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "no")
    assert result.returncode == 2
    assert "unreadable" in result.stdout


def test_guard_live_lock_blocks_even_with_service_stopped(tmp_path: Path) -> None:
    state = make_state(tmp_path, IDLE)
    output = make_output(tmp_path, {"2026-01-01_s": os.getpid()})  # a real, live pid
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "no")
    assert result.returncode == 3
    assert ".run.lock" in result.stdout


def test_guard_stale_lock_does_not_block(tmp_path: Path) -> None:
    state = make_state(tmp_path, IDLE)
    output = make_output(tmp_path, {"2026-01-01_s": 99999999})  # dead pid
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "yes")
    assert result.returncode == 0, result.stdout


def test_guard_migrated_lock_same_pid_other_program_does_not_block(tmp_path: Path) -> None:
    """A live PID whose /proc cmdline is unrelated (migration coincidence) is stale."""
    state = make_state(tmp_path, IDLE)
    output = make_output(tmp_path, {"2026-01-01_s": os.getpid()})
    proc_root = tmp_path / "proc"
    cmdline = proc_root / str(os.getpid()) / "cmdline"
    cmdline.parent.mkdir(parents=True)
    cmdline.write_bytes(b"/usr/sbin/nginx\x00-worker\x00")
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output),
                        "--service-running", "no", "--proc-root", str(proc_root))
    assert result.returncode == 0, result.stdout


def test_guard_unsafe_locks_block(tmp_path: Path) -> None:
    state = make_state(tmp_path, IDLE)
    locks: dict[str, int | str | None] = {"b_s": "dir"}
    try:
        output = make_output(tmp_path, {"a_s": None, **locks})
    except OSError:  # symlink creation needs privileges on some Windows hosts
        output = make_output(tmp_path, locks)
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "no")
    assert result.returncode == 3
    assert "unsafe" in result.stdout


def test_guard_variant_locks_are_scanned(tmp_path: Path) -> None:
    state = make_state(tmp_path, IDLE)
    output = make_output(tmp_path)
    variant = output / "2026-01-01_s" / "analysis_variants" / "configured-api"
    variant.mkdir(parents=True)
    (variant / ".run.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "no")
    assert result.returncode == 3


def test_guard_prepare_locks_are_scanned(tmp_path: Path) -> None:
    state = make_state(tmp_path, IDLE)
    output = make_output(tmp_path)
    analysis = output / "2026-01-01_s" / "analysis"
    analysis.mkdir(parents=True)
    (analysis / ".prepare.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    result = run_helper(GUARD, "--state-root", str(state), "--output-root", str(output), "--service-running", "no")
    assert result.returncode == 3


# --- verify_artifact ----------------------------------------------------------


def make_artifact(tmp_path: Path, payload: bytes = b"fixture-zip") -> tuple[Path, Path]:
    artifact = tmp_path / "pkg.zip"
    artifact.write_bytes(payload)
    import hashlib
    digest = hashlib.sha256(payload).hexdigest()
    return artifact, digest


def test_verify_ok_and_crlf_tolerated(tmp_path: Path) -> None:
    artifact, digest = make_artifact(tmp_path)
    checksum = tmp_path / "pkg.zip.sha256"
    checksum.write_bytes(f"{digest}  pkg.zip\r\n".encode())
    result = run_helper(VERIFY, str(artifact))
    assert result.returncode == 0 and "VERIFY-OK" in result.stdout


def test_verify_tampered_zip_fails(tmp_path: Path) -> None:
    artifact, digest = make_artifact(tmp_path, b"original")
    (tmp_path / "pkg.zip").write_bytes(b"tampered!!")
    (tmp_path / "pkg.zip.sha256").write_text(f"{digest}  pkg.zip\n", encoding="utf-8")
    result = run_helper(VERIFY, str(artifact))
    assert result.returncode == 1 and "mismatch" in result.stdout


def test_verify_missing_or_unpaired_checksum_fails(tmp_path: Path) -> None:
    artifact, _ = make_artifact(tmp_path)
    (tmp_path / "pkg.zip.sha256").unlink(missing_ok=True)
    assert run_helper(VERIFY, str(artifact)).returncode == 1
    (tmp_path / "pkg.zip.sha256").write_text("0" * 64 + "  other-file.zip\n", encoding="utf-8")
    result = run_helper(VERIFY, str(artifact))
    assert result.returncode == 1 and "no entry" in result.stdout


# --- check_node ----------------------------------------------------------------


def fake_node(tmp_path: Path, version: str) -> Path:
    if sys.platform == "win32":
        shim = tmp_path / f"node-{version}.bat"
        shim.write_text(f"@echo {version}\r\n", encoding="ascii")
    else:
        shim = tmp_path / f"node-{version}"
        shim.write_text(f"#!/bin/sh\necho {version}\n", encoding="ascii")
        shim.chmod(0o755)
    return shim


@pytest.mark.parametrize("version,ok", [("v22.12.0", True), ("v22.14.0", True), ("v24.1.0", True), ("v18.20.4", False), ("v22.11.1", False)])
def test_check_node_version_gate(tmp_path: Path, version: str, ok: bool) -> None:
    result = run_helper(CHECK_NODE, "--node-bin", str(fake_node(tmp_path, version)))
    assert result.returncode == (0 if ok else 1), result.stdout
    if not ok:
        assert "22.12.0" in result.stdout


def test_check_node_minimum_matches_lockfile_contract() -> None:
    lockfile = (PROJECT_ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    helper = CHECK_NODE.read_text(encoding="utf-8")
    assert ">=22.12.0" in lockfile, "lockfile contract changed; update check_node.py DEFAULT_MINIMUM"
    assert 'DEFAULT_MINIMUM = "22.12.0"' in helper
    prereq = (PROJECT_ROOT / "scripts/linux/install_prerequisites.sh").read_text(encoding="utf-8")
    assert 'NODE_MINIMUM="22.12.0"' in prereq
