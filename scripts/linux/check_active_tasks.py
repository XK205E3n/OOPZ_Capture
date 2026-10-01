#!/usr/bin/env python3
"""Decide whether OOPZ Capture may safely stop/switch the deployed release.

Exit codes:
  0 - no running recording/analysis task detected
  3 - a task is running (or a lock is unsafe to judge); refuse auto-switch
  2 - cannot reliably determine (e.g. corrupt controller state); refuse
      unless the operator explicitly passes --force at the CALLER level

Signals, mirroring src/oopz_capture/reports.py and controller.py semantics:
  * controller.json active            -> recording/transcription in flight
  * controller.json last_job=analyzing-> background analysis in flight
    (both only while the gateway service is running; after a stop they are
    persisted interruptible states, not running tasks)
  * analysis locks with a live oopz/python owner process -> an analyzer run
    (including the standalone CLI) must not be interrupted, regardless of
    the service state.  A lock whose PID is dead - or, on Linux, alive but
    whose /proc cmdline is neither python nor oopz (PID coincidence after a
    cross-machine migration) - is stale and never blocks maintenance.
  * unreadable/symlinked lock files and corrupt controller.json -> refuse.

Standalone on purpose: it runs from the OS installer before any release
virtualenv exists, so it must not import oopz_capture.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

BUSY_EXIT = 3
UNDETERMINED_EXIT = 2


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":  # test environments; production target is Linux
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False


def _cmdline_mentions_oopz(pid: int, proc_root: Path) -> bool | None:
    """True/False when /proc is available; None when the check is impossible."""
    cmdline = proc_root / str(pid) / "cmdline"
    try:
        raw = cmdline.read_bytes()
    except OSError:
        return None
    text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").lower()
    return ("oopz" in text) or ("python" in text)


def _lock_reason(lock_path: Path, proc_root: Path) -> str | None:
    """Return a busy reason for this lock, or None when it is stale."""
    try:
        exists = lock_path.exists()
    except OSError:
        return f"unreadable lock: {lock_path}"
    if not exists:
        return None
    if lock_path.is_symlink() or not lock_path.is_file():
        return f"unsafe lock (not a regular file): {lock_path}"
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(value.get("pid", 0)) if isinstance(value, dict) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Mirrors reports.py: an unparseable lock is treated as dead-owner
        # and reclaimed by the application, so it must not block maintenance.
        return None
    if not _pid_is_running(pid):
        return None
    oopz_owner = _cmdline_mentions_oopz(pid, proc_root)
    if oopz_owner is False:
        return None  # same PID, different program: stale after migration
    owner = f"pid {pid}"
    if oopz_owner is None:
        owner += " (owner identity unverifiable)"
    return f"live analysis lock {lock_path} held by {owner}"


def _session_lock_reasons(session_dir: Path, proc_root: Path) -> list[str]:
    reasons: list[str] = []
    candidates = [
        session_dir / "analysis" / ".prepare.lock",
        session_dir / "analysis" / ".run.lock",
    ]
    variants = session_dir / "analysis_variants"
    try:
        if variants.is_dir() and not variants.is_symlink():
            candidates.extend(sorted(variant / ".run.lock" for variant in variants.iterdir()))
    except OSError:
        reasons.append(f"unreadable variants directory: {variants}")
    for lock_path in candidates:
        reason = _lock_reason(lock_path, proc_root)
        if reason:
            reasons.append(reason)
    return reasons


def evaluate(state_root: Path, output_root: Path, *, service_running: bool, proc_root: Path) -> tuple[int, list[str]]:
    reasons: list[str] = []
    controller_path = state_root / "controller.json"
    if controller_path.exists():
        try:
            value = json.loads(controller_path.read_text(encoding="utf-8-sig"))
            if not isinstance(value, dict):
                raise ValueError("controller state is not an object")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return UNDETERMINED_EXIT, [f"controller state is unreadable ({error}); refusing to guess"]
        if service_running:
            active = value.get("active")
            if isinstance(active, dict) and active:
                reasons.append("controller reports an active recording/transcription session")
            last_job = value.get("last_job")
            if isinstance(last_job, dict) and str(last_job.get("status", "")).lower() == "analyzing":
                reasons.append("controller reports a background analysis (last_job.status=analyzing)")
    if output_root.is_dir():
        try:
            sessions = sorted(item for item in output_root.iterdir() if item.is_dir())
        except OSError as error:
            return UNDETERMINED_EXIT, [f"output root is unreadable ({error}); refusing to guess"]
        for session_dir in sessions:
            reasons.extend(_session_lock_reasons(session_dir, proc_root))
    else:
        reasons.append(f"note: output root does not exist yet: {output_root}")
    if reasons and any(not reason.startswith("note:") for reason in reasons):
        return BUSY_EXIT, [reason for reason in reasons if not reason.startswith("note:")]
    return 0, reasons


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="check_active_tasks.py", description=__doc__.splitlines()[0])
    parser.add_argument("--state-root", type=Path, required=True, help="Feishu state directory (controller.json)")
    parser.add_argument("--output-root", type=Path, required=True, help="Session output directory")
    parser.add_argument("--service-running", choices=["yes", "no"], required=True,
                        help="whether the gateway systemd service is currently active")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"),
                        help="virtual /proc tree for owner-cmdline checks (tests)")
    parser.add_argument("--json", action="store_true", help="print a JSON verdict instead of text")
    args = parser.parse_args(argv)
    code, reasons = evaluate(
        args.state_root, args.output_root,
        service_running=args.service_running == "yes", proc_root=args.proc_root,
    )
    verdict = {0: "safe", BUSY_EXIT: "busy", UNDETERMINED_EXIT: "undetermined"}[code]
    if args.json:
        print(json.dumps({"verdict": verdict, "reasons": reasons}, ensure_ascii=False))
    else:
        print(f"active-task check: {verdict}")
        for reason in reasons:
            print(f"  - {reason}")
    return code


if __name__ == "__main__":
    sys.exit(main())
