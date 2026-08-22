from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from .capture_session import run_capture
from .identifiers import new_session_id
from .output import write_json
from .vad import VADConfig


REQUEST_SCHEMA = "oopz.worker.request.v1"
EVENT_SCHEMA = "oopz.worker.event.v1"
ANALYZER_SCHEMA = "oopz.analyzer.request.v1"
_REPARSE_POINT = 0x0400


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class WorkflowRequest:
    request_id: str
    area_id: str
    channel_id: str
    duration_seconds: float
    consent_confirmed: bool
    language: str = "auto"
    processing_deadline_seconds: int = 900
    retention_hours: int = 168
    poll_interval_seconds: float = 0.25
    retain_audio: bool = False
    rtc_uid: str | None = None
    requested_by: dict[str, Any] | None = None
    schema_version: str = REQUEST_SCHEMA
    command: str = "record_and_transcribe"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkflowRequest":
        if value.get("schema_version") != REQUEST_SCHEMA:
            raise ValueError(f"schema_version must be {REQUEST_SCHEMA}")
        if value.get("command") != "record_and_transcribe":
            raise ValueError("command must be record_and_transcribe")
        request_id = str(value.get("request_id") or "")
        try:
            UUID(request_id)
        except ValueError as error:
            raise ValueError("request_id must be a UUID") from error
        area_id = str(value.get("area_id") or "").strip()
        channel_id = str(value.get("channel_id") or "").strip()
        if not area_id or not channel_id:
            raise ValueError("area_id and channel_id are required")
        duration = float(value.get("duration_seconds", 0))
        deadline = int(value.get("processing_deadline_seconds", 900))
        retention = int(value.get("retention_hours", 168))
        poll = float(value.get("poll_interval_seconds", 0.25))
        language = str(value.get("language", "auto"))
        if duration <= 0:
            raise ValueError("duration_seconds must be greater than zero for an automatic job")
        if not 60 <= deadline <= 3600:
            raise ValueError("processing_deadline_seconds must be 60 to 3600")
        if not 1 <= retention <= 168:
            raise ValueError("retention_hours must be 1 to 168")
        if not 0.05 <= poll <= 5:
            raise ValueError("poll_interval_seconds must be 0.05 to 5")
        if language not in {"auto", "zh", "en", "yue", "ja", "ko"}:
            raise ValueError("unsupported language")
        if value.get("consent_confirmed") is not True:
            raise ValueError("consent_confirmed must be true")
        requested_by = value.get("requested_by")
        if requested_by is not None and not isinstance(requested_by, dict):
            raise ValueError("requested_by must be an object")
        return cls(
            request_id=request_id,
            area_id=area_id,
            channel_id=channel_id,
            duration_seconds=duration,
            consent_confirmed=True,
            language=language,
            processing_deadline_seconds=deadline,
            retention_hours=retention,
            poll_interval_seconds=poll,
            retain_audio=value.get("retain_audio") is True,
            rtc_uid=str(value["rtc_uid"]) if value.get("rtc_uid") is not None else None,
            requested_by=requested_by,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def emit_event(
    event: str, request_id: str, *, stdout: bool = True, **fields: Any,
) -> dict[str, Any]:
    """Build a structured event, optionally writing its JSON representation.

    Worker CLIs keep their machine-readable stdout contract.  The continuous
    recorder disables it because it also emits concise human progress lines.
    """
    value = {
        "schema_version": EVENT_SCHEMA,
        "event": event,
        "at": _iso(utc_now()),
        "request_id": request_id,
        **fields,
    }
    if stdout:
        print(json.dumps(value, ensure_ascii=False), flush=True)
    return value


def _resolved_direct_child(root: Path, child: Path) -> tuple[Path, Path]:
    root = root.resolve()
    child = child.resolve()
    if child.parent != root or child == root:
        raise ValueError(f"refusing unsafe session path: {child}")
    return root, child


def _is_reparse_point(path: Path) -> bool:
    stat = os.lstat(path)
    return path.is_symlink() or bool(getattr(stat, "st_file_attributes", 0) & _REPARSE_POINT)


def _validate_tree_no_links(root: Path) -> None:
    if _is_reparse_point(root):
        raise ValueError(f"refusing reparse point: {root}")
    for current, directories, files in os.walk(root, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current) / name
            if _is_reparse_point(path):
                raise ValueError(f"refusing linked retention target: {path}")


def validate_transcript(session_dir: Path) -> dict[str, Any]:
    session_path = session_dir / "session.json"
    jsonl_path = session_dir / "transcript.jsonl"
    markdown_path = session_dir / "transcript.md"
    summary_path = session_dir / "transcript_summary.json"
    for path in (session_path, jsonl_path, markdown_path, summary_path):
        if not path.is_file():
            raise ValueError(f"required transcript output is missing: {path.name}")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session_id = str(session.get("session_id") or "")
    records: list[dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            required = {"segment_id", "session_id", "start_ms", "end_ms", "agora_uid", "oopz_uid", "speaker", "text"}
            missing = required.difference(item)
            if missing:
                raise ValueError(f"transcript line {line_number} is missing: {sorted(missing)}")
            if str(item["session_id"]) != session_id:
                raise ValueError(f"transcript line {line_number} has the wrong session_id")
            str(item["text"]).encode("utf-8")
            records.append(item)
    for previous, current in zip(records, records[1:]):
        if (int(current["start_ms"]), int(current["agora_uid"])) < (int(previous["start_ms"]), int(previous["agora_uid"])):
            raise ValueError("transcript records are not time ordered")
    markdown = markdown_path.read_text(encoding="utf-8")
    if f"Session ID: {session_id}" not in markdown:
        raise ValueError("transcript.md does not label the Session ID")
    return {"session_id": session_id, "segment_count": len(records)}


def write_analyzer_handoff(
    session_dir: Path,
    request: WorkflowRequest,
    *,
    analysis_deadline_at: datetime,
    delete_after: datetime,
    segment_count: int,
) -> Path:
    now = utc_now()
    path = session_dir / "handoff" / "analyzer_request.json"
    write_json(path, {
        "schema_version": ANALYZER_SCHEMA,
        "request_id": request.request_id,
        "session_id": session_dir.name,
        "created_at": _iso(now),
        "analysis_deadline_at": _iso(analysis_deadline_at),
        "remaining_seconds_at_handoff": max(0, int((analysis_deadline_at - now).total_seconds())),
        "encoding": "UTF-8",
        "delivery_mode": "final_only",
        "summary_windows": {
            "short_summary_seconds": 300,
            "long_summary_seconds": 3600,
        },
        "inputs": {
            "transcript_jsonl": "transcript.jsonl",
            "transcript_markdown": "transcript.md",
            "transcript_summary": "transcript_summary.json",
            "users": "users.json",
            "session": "session.json",
            "segment_count": segment_count,
        },
        "required_outputs": {
            "analysis_result": "analysis/result.json",
            "human_summary": "analysis/summary.md",
            "qq_messages": "handoff/qq_messages.jsonl",
        },
        "retention": {
            "delete_after": _iso(delete_after),
            "maximum_hours": request.retention_hours,
            "audio_retained_for_testing": request.retain_audio,
        },
    })
    return path


def purge_session_audio(output_root: Path, session_dir: Path, *, deleted_at: datetime | None = None) -> list[str]:
    _, session_dir = _resolved_direct_child(output_root, session_dir)
    audio_dir = session_dir / "audio"
    if not audio_dir.exists():
        return []
    if not audio_dir.is_dir() or _is_reparse_point(audio_dir):
        raise ValueError(f"refusing unsafe audio directory: {audio_dir}")
    targets = list(audio_dir.iterdir())
    for target in targets:
        if not target.is_file() or _is_reparse_point(target):
            raise ValueError(f"refusing unexpected audio target: {target}")
    deleted = [str(path.relative_to(session_dir)).replace("\\", "/") for path in targets]
    for target in targets:
        target.unlink()
    audio_dir.rmdir()
    manifest_path = session_dir / "audio_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        timestamp = _iso(deleted_at or utc_now())
        for item in manifest:
            item["audio_deleted"] = True
            item["audio_deleted_at"] = timestamp
        write_json(manifest_path, manifest)
    return deleted


def cleanup_expired(output_root: Path, *, now: datetime | None = None, dry_run: bool = False) -> list[Path]:
    root = output_root.resolve()
    if not root.exists():
        return []
    now = (now or utc_now()).astimezone(timezone.utc)
    expired: list[Path] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or _is_reparse_point(candidate):
            continue
        lifecycle_path = candidate / "lifecycle.json"
        if not lifecycle_path.is_file():
            continue
        try:
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
            if lifecycle.get("managed_by") != "oopz-worker-v1":
                continue
            delete_after = _parse_time(str(lifecycle["delete_after"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if delete_after > now:
            continue
        _, resolved = _resolved_direct_child(root, candidate)
        _validate_tree_no_links(resolved)
        expired.append(resolved)
    if not dry_run:
        for target in expired:
            _delete_archived_reports(root, target)
            shutil.rmtree(target)
    return expired


def _delete_archived_reports(output_root: Path, session_dir: Path) -> None:
    """Delete only PDFs explicitly associated with an expired managed Session.

    New reports carry a manifest. For historical reports, derive the stable
    filename prefix used by the renderer. No other Report files are touched.
    """
    report_root = (output_root / "Report").resolve()
    targets: list[Path] = []
    manifest_path = session_dir / "report_archive.json"
    if manifest_path.is_file() and not _is_reparse_point(manifest_path):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = manifest.get("files") if isinstance(manifest, dict) else []
        except (OSError, ValueError, TypeError):
            entries = []
        if isinstance(entries, list):
            for entry in entries:
                candidate = (output_root / str(entry)).resolve()
                try:
                    candidate.relative_to(report_root)
                except ValueError:
                    continue
                targets.append(candidate)
    if not targets and report_root.is_dir() and not _is_reparse_point(report_root):
        try:
            from .pdf_reports import session_report_stamp
            date_folder, stamp = session_report_stamp(session_dir)
            date_root = (report_root / date_folder).resolve()
            if date_root.parent == report_root and date_root.is_dir() and not _is_reparse_point(date_root):
                targets.extend(date_root.glob(f"{stamp}_*.pdf"))
        except (OSError, ValueError, TypeError):
            pass
    parent_dirs: set[Path] = set()
    for target in targets:
        if not target.is_file() or _is_reparse_point(target):
            continue
        try:
            target.relative_to(report_root)
        except ValueError:
            continue
        parent_dirs.add(target.parent)
        target.unlink()
    for directory in sorted(parent_dirs, key=lambda item: len(item.parts), reverse=True):
        if directory != report_root:
            try:
                directory.rmdir()
            except OSError:
                pass


async def _run_transcription_process(
    session_dir: Path,
    request: WorkflowRequest,
    vad_config: VADConfig,
    device: str,
    timeout_seconds: float,
) -> tuple[str, str]:
    command = [
        sys.executable, "-m", "oopz_capture.speech_cli", "process", str(session_dir),
        "--device", device, "--language", request.language,
        "--vad-threshold", str(vad_config.threshold),
        "--min-speech-ms", str(vad_config.min_speech_ms),
        "--min-silence-ms", str(vad_config.min_silence_ms),
        "--speech-pad-ms", str(vad_config.speech_pad_ms),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise TimeoutError("transcription exceeded the remaining analysis deadline")
    output = stdout.decode("utf-8", errors="replace")
    errors = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"transcription failed with exit code {process.returncode}: {errors.strip()}")
    return output, errors


async def run_workflow(
    config: Any,
    request: WorkflowRequest,
    *,
    output_root: Path,
    show_browser: bool = False,
    device: str = "cpu",
    vad_config: VADConfig | None = None,
    capture_runner: Callable[..., Awaitable[Path]] = run_capture,
    transcription_runner: Callable[[Path, WorkflowRequest, VADConfig, str, float], Awaitable[tuple[str, str]]] = _run_transcription_process,
) -> Path:
    del show_browser  # Browser visibility is already represented by the supplied OOPZ config.
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    removed = cleanup_expired(output_root)
    emit_event("retention.completed", request.request_id, deleted_session_ids=[path.name for path in removed])
    emit_event("capture.started", request.request_id, area_id=request.area_id, channel_id=request.channel_id)
    session_dir: Path | None = output_root / new_session_id(output_root)
    try:
        session_dir = await capture_runner(
            config,
            area=request.area_id,
            channel=request.channel_id,
            duration_seconds=request.duration_seconds,
            poll_interval=request.poll_interval_seconds,
            output_root=output_root,
            rtc_uid=request.rtc_uid,
            session_id=session_dir.name,
        )
        session_dir = session_dir.resolve()
        capture_finished_at = utc_now()
        analysis_deadline_at = capture_finished_at + timedelta(seconds=request.processing_deadline_seconds)
        delete_after = capture_finished_at + timedelta(hours=request.retention_hours)
        lifecycle = {
            "schema_version": "oopz.lifecycle.v1",
            "managed_by": "oopz-worker-v1",
            "request_id": request.request_id,
            "session_id": session_dir.name,
            "status": "transcribing",
            "capture_finished_at": _iso(capture_finished_at),
            "analysis_deadline_at": _iso(analysis_deadline_at),
            "delete_after": _iso(delete_after),
            "audio_deleted": False,
            "failure": None,
        }
        write_json(session_dir / "request.json", request.to_dict())
        write_json(session_dir / "lifecycle.json", lifecycle)
        emit_event("capture.completed", request.request_id, session_id=session_dir.name, analysis_deadline_at=_iso(analysis_deadline_at))

        remaining = (analysis_deadline_at - utc_now()).total_seconds()
        if remaining <= 0:
            raise TimeoutError("no processing time remains after capture")
        stdout, stderr = await transcription_runner(
            session_dir, request, vad_config or VADConfig(), device, remaining,
        )
        (session_dir / "transcription.log").write_text(stdout + stderr, encoding="utf-8")
        validated = validate_transcript(session_dir)
        handoff = write_analyzer_handoff(
            session_dir,
            request,
            analysis_deadline_at=analysis_deadline_at,
            delete_after=delete_after,
            segment_count=int(validated["segment_count"]),
        )
        deleted_files = [] if request.retain_audio else purge_session_audio(output_root, session_dir)
        completed_at = utc_now()
        lifecycle.update({
            "status": "ready_for_analysis",
            "transcription_completed_at": _iso(completed_at),
            "transcript_segments": validated["segment_count"],
            "audio_deleted": not request.retain_audio,
            "audio_retained_for_testing": request.retain_audio,
            "audio_deleted_at": _iso(completed_at) if not request.retain_audio else None,
            "deleted_audio_files": deleted_files,
            "transcription_handoff_before_analysis_deadline": completed_at <= analysis_deadline_at,
            "analyzer_handoff": str(handoff.relative_to(session_dir)).replace("\\", "/"),
        })
        write_json(session_dir / "lifecycle.json", lifecycle)
        emit_event(
            "session.ready_for_analysis",
            request.request_id,
            session_id=session_dir.name,
            transcript_jsonl=str(session_dir / "transcript.jsonl"),
            transcript_markdown=str(session_dir / "transcript.md"),
            analyzer_request=str(handoff),
            audio_deleted=not request.retain_audio,
            audio_retained_for_testing=request.retain_audio,
            analysis_deadline_at=_iso(analysis_deadline_at),
        )
        return session_dir
    except BaseException as error:
        if session_dir is not None and session_dir.exists():
            lifecycle_path = session_dir / "lifecycle.json"
            try:
                lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8")) if lifecycle_path.exists() else {}
                lifecycle.update({
                    "schema_version": "oopz.lifecycle.v1",
                    "managed_by": "oopz-worker-v1",
                    "request_id": request.request_id,
                    "session_id": session_dir.name,
                    "status": "failed",
                    "failed_at": _iso(utc_now()),
                    "failure": {"type": type(error).__name__, "message": str(error)},
                    "audio_deleted": False,
                })
                lifecycle.setdefault("delete_after", _iso(utc_now() + timedelta(hours=request.retention_hours)))
                write_json(lifecycle_path, lifecycle)
            except Exception:
                pass
        emit_event("session.failed", request.request_id, session_id=session_dir.name if session_dir else None, error_type=type(error).__name__, error=str(error), audio_retained=True)
        raise


def new_request(**values: Any) -> WorkflowRequest:
    return WorkflowRequest.from_dict({
        "schema_version": REQUEST_SCHEMA,
        "command": "record_and_transcribe",
        "request_id": str(uuid4()),
        **values,
    })
