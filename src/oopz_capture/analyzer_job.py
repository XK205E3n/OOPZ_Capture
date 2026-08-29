from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .analysis_windows import (
    LONG_WINDOW_MS,
    WINDOW_PLANNER_VERSION,
    SHORT_WINDOW_MS,
    determine_session_duration_ms,
    plan_windows,
    write_window_plan,
)
from .jsonio import iso_utc as _iso, read_json as _read_json
from .output import write_json
from .identifiers import validate_session_id
from .workflow import _is_reparse_point, utc_now


LOGGER = logging.getLogger(__name__)


def _release_lock(lock_path: Path) -> None:
    """Best-effort lock release; a stuck lock must not mask finished work."""
    if not lock_path.is_file() or _is_reparse_point(lock_path):
        return
    try:
        lock_path.unlink()
    except OSError:
        LOGGER.warning("could not release analysis lock: %s", lock_path, exc_info=True)


def _aware_time(value: Any, field: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _uuid(value: Any, field: str) -> str:
    text = str(value or "")
    try:
        UUID(text)
    except ValueError as error:
        raise ValueError(f"{field} must be a UUID") from error
    return text


def _safe_session_file(session_dir: Path, relative: Any, field: str) -> Path:
    value = str(relative or "")
    path_value = Path(value)
    if not value or path_value.is_absolute() or ".." in path_value.parts:
        raise ValueError(f"{field} must be a safe relative Session path")
    session_dir = session_dir.resolve()
    lexical_path = session_dir / path_value
    current = session_dir
    for part in path_value.parts:
        current = current / part
        if current.exists() and _is_reparse_point(current):
            raise ValueError(f"{field} may not use a symlink or reparse point")
    path = lexical_path.resolve()
    try:
        path.relative_to(session_dir)
    except ValueError as error:
        raise ValueError(f"{field} escapes the Session directory") from error
    if not path.is_file():
        raise ValueError(f"{field} is missing: {value}")
    return path


def _load_transcript(path: Path, session_id: str) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    segment_ids: set[str] = set()
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"transcript line {line_number} must be an object")
            required = {"segment_id", "session_id", "start_ms", "end_ms", "agora_uid", "oopz_uid", "speaker", "text"}
            missing = required.difference(value)
            if missing:
                raise ValueError(f"transcript line {line_number} is missing {sorted(missing)}")
            segment_id = _uuid(value["segment_id"], f"transcript line {line_number} segment_id")
            if segment_id in segment_ids:
                raise ValueError(f"duplicate transcript segment_id: {segment_id}")
            segment_ids.add(segment_id)
            if str(value["session_id"]) != session_id:
                raise ValueError(f"transcript line {line_number} has the wrong session_id")
            start_ms = int(value["start_ms"])
            end_ms = int(value["end_ms"])
            if start_ms < 0 or end_ms <= start_ms:
                raise ValueError(f"transcript line {line_number} has an invalid time range")
            if not str(value["text"]).strip():
                raise ValueError(f"transcript line {line_number} has empty text")
            values.append(value)
    values.sort(key=lambda item: (int(item["start_ms"]), int(item["agora_uid"]), int(item["end_ms"])))
    return values


@dataclass(frozen=True)
class AnalyzerInput:
    handoff_path: Path
    session_dir: Path
    request: dict[str, Any]
    session: dict[str, Any]
    users: list[dict[str, Any]]
    transcript: list[dict[str, Any]]
    transcript_path: Path
    short_summary_seconds: int
    long_summary_seconds: int
    fingerprint: str

    @property
    def request_id(self) -> str:
        return str(self.request["request_id"])

    @property
    def session_id(self) -> str:
        return str(self.request["session_id"])


def load_analyzer_input(handoff_path: Path) -> AnalyzerInput:
    raw_handoff_path = handoff_path.absolute()
    if not raw_handoff_path.is_file() or raw_handoff_path.name != "analyzer_request.json" or raw_handoff_path.parent.name != "handoff":
        raise ValueError("handoff path must be <SESSION>/handoff/analyzer_request.json")
    for path in (raw_handoff_path, raw_handoff_path.parent, raw_handoff_path.parent.parent):
        if _is_reparse_point(path):
            raise ValueError("handoff and Session paths may not be links")
    handoff_path = raw_handoff_path.resolve()
    session_dir = handoff_path.parent.parent.resolve()
    request = _read_json(handoff_path)
    if not isinstance(request, dict) or request.get("schema_version") != "oopz.analyzer.request.v1":
        raise ValueError("unsupported analyzer request schema")
    request_id = _uuid(request.get("request_id"), "request_id")
    session_id = validate_session_id(request.get("session_id"), "session_id")
    if session_id != session_dir.name:
        raise ValueError("handoff session_id does not match its directory")
    if request.get("encoding") != "UTF-8":
        raise ValueError("analyzer input encoding must be UTF-8")
    _aware_time(request.get("created_at"), "created_at")
    _aware_time(request.get("analysis_deadline_at"), "analysis_deadline_at")
    retention = request.get("retention")
    if not isinstance(retention, dict):
        raise ValueError("retention must be an object")
    _aware_time(retention.get("delete_after"), "retention.delete_after")
    if int(retention.get("maximum_hours", 0)) > 360:
        raise ValueError("retention.maximum_hours may not exceed 360")

    inputs = request.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    transcript_path = _safe_session_file(session_dir, inputs.get("transcript_jsonl"), "inputs.transcript_jsonl")
    session_path = _safe_session_file(session_dir, inputs.get("session"), "inputs.session")
    users_path = _safe_session_file(session_dir, inputs.get("users"), "inputs.users")
    _safe_session_file(session_dir, inputs.get("transcript_markdown"), "inputs.transcript_markdown")
    _safe_session_file(session_dir, inputs.get("transcript_summary"), "inputs.transcript_summary")
    session = _read_json(session_path)
    users = _read_json(users_path)
    if not isinstance(session, dict) or str(session.get("session_id")) != session_id:
        raise ValueError("session.json has the wrong session_id")
    if not isinstance(users, list):
        raise ValueError("users.json must contain an array")
    transcript = _load_transcript(transcript_path, session_id)
    declared_count = int(inputs.get("segment_count", -1))
    if declared_count != len(transcript):
        raise ValueError(f"segment_count mismatch: declared={declared_count}, actual={len(transcript)}")

    windows = request.get("summary_windows") or {}
    short_seconds = int(windows.get("short_summary_seconds", SHORT_WINDOW_MS // 1000))
    long_seconds = int(windows.get("long_summary_seconds", LONG_WINDOW_MS // 1000))
    if short_seconds != SHORT_WINDOW_MS // 1000:
        raise ValueError("short_summary_seconds must be 300")
    if long_seconds != LONG_WINDOW_MS // 1000:
        raise ValueError("long_summary_seconds must be 3600")

    digest = hashlib.sha256()
    for path in (handoff_path, transcript_path, session_path, users_path):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    digest.update(request_id.encode("ascii"))
    return AnalyzerInput(
        handoff_path=handoff_path,
        session_dir=session_dir,
        request=request,
        session=session,
        users=users,
        transcript=transcript,
        transcript_path=transcript_path,
        short_summary_seconds=short_seconds,
        long_summary_seconds=long_seconds,
        fingerprint=digest.hexdigest(),
    )


def _acquire_lock(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), "created_at": _iso(utc_now())}) + "\n")
    except FileExistsError as error:
        raise RuntimeError(f"analysis preparation is already locked: {path}") from error


def prepare_analysis(handoff_path: Path) -> dict[str, Any]:
    value = load_analyzer_input(handoff_path)
    analysis_dir = value.session_dir / "analysis"
    lifecycle_path = analysis_dir / "lifecycle.json"
    job_path = analysis_dir / "job.json"
    windows_path = analysis_dir / "windows.json"
    lock_path = analysis_dir / ".prepare.lock"
    _acquire_lock(lock_path)
    try:
        existing_job = _read_json(job_path) if job_path.is_file() else None
        if isinstance(existing_job, dict) and existing_job.get("input_fingerprint") == value.fingerprint and windows_path.is_file():
            windows = _read_json(windows_path)
            if windows.get("schema_version") == "oopz.analysis.windows.v1" and windows.get("planner_version") == WINDOW_PLANNER_VERSION and windows.get("session_id") == value.session_id:
                return {"session_dir": value.session_dir, "job": existing_job, "windows": windows, "reused": True}

        previous_lifecycle = _read_json(lifecycle_path) if lifecycle_path.is_file() else {}
        attempts = int(previous_lifecycle.get("prepare_attempts", 0)) + 1 if isinstance(previous_lifecycle, dict) else 1
        lifecycle = {
            "schema_version": "oopz.analysis.lifecycle.v1",
            "request_id": value.request_id,
            "session_id": value.session_id,
            "status": "preparing_windows",
            "prepare_attempts": attempts,
            "updated_at": _iso(utc_now()),
            "input_fingerprint": value.fingerprint,
            "failure": None,
        }
        write_json(lifecycle_path, lifecycle)
        duration_ms = determine_session_duration_ms(value.session_dir, value.session, value.transcript)
        windows = plan_windows(
            value.session_id,
            value.transcript,
            duration_ms,
            short_window_ms=value.short_summary_seconds * 1000,
            long_window_ms=value.long_summary_seconds * 1000,
        )
        json_path, markdown_path = write_window_plan(value.session_dir, windows)
        deadline = _aware_time(value.request["analysis_deadline_at"], "analysis_deadline_at")
        job = {
            "schema_version": "oopz.analysis.job.v1",
            "request_id": value.request_id,
            "session_id": value.session_id,
            "created_at": _iso(utc_now()),
            "input_fingerprint": value.fingerprint,
            "window_planner_version": WINDOW_PLANNER_VERSION,
            "analysis_deadline_at": _iso(deadline),
            "deadline_already_passed": utc_now() > deadline,
            "short_summary_seconds": value.short_summary_seconds,
            "long_summary_seconds": value.long_summary_seconds,
            "transcript_segments": len(value.transcript),
            "duration_ms": duration_ms,
            "short_window_count": windows["short_window_count"],
            "long_window_count": windows["long_window_count"],
            "paths": {
                "handoff": str(value.handoff_path.relative_to(value.session_dir)).replace("\\", "/"),
                "transcript": str(value.transcript_path.relative_to(value.session_dir)).replace("\\", "/"),
                "windows_json": str(json_path.relative_to(value.session_dir)).replace("\\", "/"),
                "windows_markdown": str(markdown_path.relative_to(value.session_dir)).replace("\\", "/"),
            },
        }
        write_json(job_path, job)
        write_json(analysis_dir / "checkpoint.json", {
            "schema_version": "oopz.analysis.checkpoint.v1",
            "request_id": value.request_id,
            "session_id": value.session_id,
            "input_fingerprint": value.fingerprint,
            "completed_short_window_ids": [],
            "completed_long_window_ids": [],
            "final_report_completed": False,
            "updated_at": _iso(utc_now()),
        })
        lifecycle.update({
            "status": "windows_ready",
            "updated_at": _iso(utc_now()),
            "short_window_count": windows["short_window_count"],
            "long_window_count": windows["long_window_count"],
        })
        write_json(lifecycle_path, lifecycle)
        return {"session_dir": value.session_dir, "job": job, "windows": windows, "reused": False}
    except Exception as error:
        write_json(lifecycle_path, {
            "schema_version": "oopz.analysis.lifecycle.v1",
            "request_id": value.request_id,
            "session_id": value.session_id,
            "status": "failed",
            "updated_at": _iso(utc_now()),
            "input_fingerprint": value.fingerprint,
            "failure": {"type": type(error).__name__, "message": str(error)},
        })
        raise
    finally:
        _release_lock(lock_path)
