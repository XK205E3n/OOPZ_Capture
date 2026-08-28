"""Discovery of recent final reports for Feishu selection cards."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import atomic_json as _atomic_json
from .process_utils import pid_is_running
from .workflow import _is_reparse_point


def _iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="seconds")


def _report_id(summary_dir: Path) -> str | None:
    result_path = summary_dir / "result.json"
    if not result_path.is_file():
        return None
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
        report_id = str(value.get("report_id") or "") if isinstance(value, dict) else ""
        return report_id or None
    except (ValueError, OSError):
        return None


def _candidate_reports(session_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    default_md = session_dir / "analysis" / "summary.md"
    if default_md.is_file():
        candidates.append(default_md)
    variants = session_dir / "analysis_variants"
    if variants.is_dir():
        for variant_dir in sorted(variants.iterdir()):
            if variant_dir.is_dir():
                md = variant_dir / "summary.md"
                if md.is_file():
                    candidates.append(md)
    return candidates


def find_recent_reports(output_root: Path, limit: int = 7) -> list[dict[str, Any]]:
    """Return the newest final reports (default or variant) with public/full paths."""
    output_root = output_root.resolve()
    if not output_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for session_dir in output_root.iterdir():
        if not session_dir.is_dir() or _is_reparse_point(session_dir):
            continue
        candidates = _candidate_reports(session_dir)
        if not candidates:
            continue
        def modified_time(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        # A Session can contain historical/experimental variants. Report
        # selection must represent the most recently generated result, not the
        # first directory returned by lexical traversal.
        full_path = max(candidates, key=modified_time)
        public_path = full_path.with_name("summary.public.md")
        if not public_path.is_file():
            public_path = full_path
        modified = modified_time(full_path)
        records.append({
            "session_id": session_dir.name,
            "summary_path": public_path,
            "full_summary_path": full_path,
            "public_summary_path": public_path,
            "report_id": _report_id(full_path.parent),
            "modified": _iso_utc(modified),
            "modified_ts": modified,
        })
    records.sort(key=lambda item: item["modified_ts"], reverse=True)
    return records[:limit]


def report_text(summary_path: Path) -> str:
    return summary_path.read_text(encoding="utf-8")


def split_text(text: str, max_chars: int = 3000) -> list[str]:
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            pieces = [paragraph[index:index + max_chars] for index in range(0, len(paragraph), max_chars)]
        else:
            pieces = [paragraph]
        for piece in pieces:
            candidate = piece if not current else current + "\n\n" + piece
            if len(candidate) > max_chars and current:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks or [text]


def _has_analysis(session_dir: Path) -> bool:
    if (session_dir / "analysis" / "result.json").is_file():
        return True
    variants = session_dir / "analysis_variants"
    if not variants.is_dir():
        return False
    for variant_dir in variants.iterdir():
        if variant_dir.is_dir() and (variant_dir / "result.json").is_file():
            return True
    return False


def _analysis_lock_paths(session_dir: Path) -> list[Path]:
    paths = [session_dir / "analysis" / ".run.lock"]
    variants = session_dir / "analysis_variants"
    if variants.is_dir() and not _is_reparse_point(variants):
        paths.extend(
            variant / ".run.lock"
            for variant in variants.iterdir()
            if variant.is_dir() and not _is_reparse_point(variant)
        )
    return paths


def _lock_is_active(lock_path: Path) -> bool:
    """Return true only for a safe lock whose owning process is still alive."""
    if not lock_path.exists():
        return False
    if _is_reparse_point(lock_path) or not lock_path.is_file():
        # An unsafe lock must not be deleted or offered for concurrent recovery.
        return True
    try:
        value = json.loads(lock_path.read_text(encoding="utf-8"))
        pid = int(value.get("pid", 0)) if isinstance(value, dict) else 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    return pid_is_running(pid)


def _has_active_analysis_lock(session_dir: Path) -> bool:
    return any(_lock_is_active(path) for path in _analysis_lock_paths(session_dir))


def _has_interrupted_analysis(session_dir: Path) -> bool:
    for lock_path in _analysis_lock_paths(session_dir):
        lifecycle_path = lock_path.parent / "lifecycle.json"
        if not lifecycle_path.is_file() or _is_reparse_point(lifecycle_path):
            continue
        try:
            value = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("status") == "interrupted":
            return True
    return False


def recover_interrupted_analysis_sessions(output_root: Path) -> list[dict[str, Any]]:
    """Release dead analysis locks and persist an explicit resumable checkpoint state.

    A live lock is never touched.  The analysis pipeline already persists every
    completed window, so a later retry reuses valid summaries instead of making
    duplicate API calls.
    """
    output_root = output_root.resolve()
    if not output_root.is_dir():
        return []
    recovered: list[dict[str, Any]] = []
    for session_dir in output_root.iterdir():
        if not session_dir.is_dir() or _is_reparse_point(session_dir):
            continue
        handoff = session_dir / "handoff" / "analyzer_request.json"
        if not handoff.is_file() or _is_reparse_point(handoff) or _has_analysis(session_dir):
            continue
        locks = [path for path in _analysis_lock_paths(session_dir) if path.exists()]
        if not locks or _has_active_analysis_lock(session_dir):
            continue
        safe_locks = [path for path in locks if path.is_file() and not _is_reparse_point(path)]
        if len(safe_locks) != len(locks):
            continue
        for lock_path in safe_locks:
            lock_path.unlink()
            lifecycle_path = lock_path.parent / "lifecycle.json"
            try:
                value = json.loads(lifecycle_path.read_text(encoding="utf-8")) if lifecycle_path.is_file() else {}
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                value = {}
            if not isinstance(value, dict):
                value = {}
            previous_status = str(value.get("status") or "unknown")
            value.update({
                "schema_version": "oopz.analysis.lifecycle.v1",
                "session_id": session_dir.name,
                "status": "interrupted",
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "interrupted_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "recovery_reason": "analysis_process_not_running",
                "previous_status": previous_status,
            })
            _atomic_json(lifecycle_path, value)
        try:
            modified = session_dir.stat().st_mtime
        except OSError:
            modified = 0.0
        recovered.append({"session_id": session_dir.name, "modified_ts": modified, "recovered_locks": len(safe_locks)})
    recovered.sort(key=lambda item: item["modified_ts"], reverse=True)
    return recovered


def find_pending_sessions(output_root: Path) -> list[dict[str, Any]]:
    """Sessions with an analyzer handoff but no completed analysis yet."""
    output_root = output_root.resolve()
    if not output_root.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for session_dir in output_root.iterdir():
        if not session_dir.is_dir() or _is_reparse_point(session_dir):
            continue
        handoff = session_dir / "handoff" / "analyzer_request.json"
        if not handoff.is_file():
            continue
        if _has_analysis(session_dir):
            continue
        # A lock is only authoritative while its owner PID is alive.  A dead
        # lock is recoverable by run_analysis and must remain visible in the
        # bot instead of making the session disappear after a process crash.
        if _has_active_analysis_lock(session_dir):
            continue
        try:
            modified = session_dir.stat().st_mtime
        except OSError:
            modified = 0.0
        records.append({
            "session_id": session_dir.name,
            "modified_ts": modified,
            "interrupted": _has_interrupted_analysis(session_dir),
        })
    records.sort(key=lambda item: item["modified_ts"], reverse=True)
    return records
