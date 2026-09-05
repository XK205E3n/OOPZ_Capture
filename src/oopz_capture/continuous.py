from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .browser_probe import AgoraBrowserProbe, PROBE_VERSION
from .identity import build_identity_mappings
from .models import ProbeSnapshot
from .output import write_json, write_jsonl
from .identifiers import new_session_id, validate_session_id
from .jsonio import iso_utc as _iso
from .recorder import CaptureRecorder
from .session import _resolve_participants
from .transcript import render_transcript_markdown
from .vad import VADConfig
from .workflow import (
    WorkflowRequest,
    _is_reparse_point,
    _run_transcription_process,
    emit_event as _emit_structured_event,
    utc_now,
    validate_transcript,
)


BEIJING_TZ = timezone(timedelta(hours=8))
CONTINUOUS_REQUEST_SCHEMA = "oopz.continuous.request.v1"
CONTINUOUS_STOP_SCHEMA = "oopz.continuous.stop.v1"
MAX_CHUNK_SECONDS = 300
LOGGER = logging.getLogger(__name__)


def no_text_marker(session_dir: Path) -> dict[str, Any]:
    """Represent an entirely silent chunk without relying on a model."""
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    duration_ms = max(1, round(float(session.get("duration_seconds") or 0) * 1000))
    started = datetime.fromisoformat(str(
        session.get("capture_clock_started_at") or session.get("started_at")
    ).replace("Z", "+00:00"))
    return {
        "segment_id": "no-speech",
        "session_id": str(session.get("session_id") or session_dir.name),
        "start_ms": 0,
        "end_ms": duration_ms,
        "start_time": started.isoformat(timespec="milliseconds"),
        "end_time": (started + timedelta(milliseconds=duration_ms)).isoformat(timespec="milliseconds"),
        "agora_uid": 0,
        "oopz_uid": "",
        "speaker": "系统",
        "text": "[该时间段未检测到有效语音文本]",
        "language": "none",
        "asr_backend": "sensevoice-small",
        "transcript_source": "no-speech-marker",
        "overlap": False,
    }


def emit_event(event: str, request_id: str, **fields: Any) -> dict[str, Any]:
    """Retain structured events in control flow without cluttering the console.

    Human-readable recording and transcription lines immediately surrounding
    each event are the sole continuous-capture console output.
    """
    return _emit_structured_event(event, request_id, stdout=False, **fields)


class VoiceConnectionLost(RuntimeError):
    """The browser/Agora connection stopped carrying capture data."""


class ReconnectWindowExpired(RuntimeError):
    """Voice could not be restored within the configured Session window."""


@dataclass(frozen=True)
class ContinuousRequest:
    request_id: str
    area_id: str
    channel_id: str
    consent_confirmed: bool
    chunk_seconds: int = MAX_CHUNK_SECONDS
    short_summary_seconds: int = 300
    long_summary_seconds: int = 3600
    cutoff_local_hour: int = 4
    language: str = "auto"
    processing_deadline_seconds: int = 900
    retention_hours: int = 360
    poll_interval_seconds: float = 0.25
    membership_refresh_seconds: float = 30.0
    membership_timeout_seconds: float = 10.0
    empty_channel_timeout_seconds: float = 300.0
    retain_audio: bool = False
    connection_check_seconds: float = 2.0
    disconnect_grace_seconds: float = 15.0
    browser_operation_timeout_seconds: float = 2.0
    reconnect_window_seconds: float = 300.0
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    reconnect_attempt_timeout_seconds: float = 30.0
    rtc_uid: str | None = None
    requested_by: dict[str, Any] | None = None
    max_runtime_seconds: float | None = None
    schema_version: str = CONTINUOUS_REQUEST_SCHEMA
    command: str = "start_continuous_capture"

    def validate(self) -> None:
        try:
            UUID(self.request_id)
        except ValueError as error:
            raise ValueError("request_id must be a UUID") from error
        if not self.area_id.strip() or not self.channel_id.strip():
            raise ValueError("area_id and channel_id are required")
        if self.consent_confirmed is not True:
            raise ValueError("consent_confirmed must be true")
        if not 30 <= self.chunk_seconds <= MAX_CHUNK_SECONDS:
            raise ValueError(f"chunk_seconds must be 30 to {MAX_CHUNK_SECONDS}; five minutes is the hard maximum")
        if self.short_summary_seconds != 300:
            raise ValueError("short_summary_seconds must be 300")
        if self.long_summary_seconds != 3600:
            raise ValueError("long_summary_seconds must be 3600")
        if not 0 <= self.cutoff_local_hour <= 23:
            raise ValueError("cutoff_local_hour must be 0 to 23")
        if not 60 <= self.processing_deadline_seconds <= 3600:
            raise ValueError("processing_deadline_seconds must be 60 to 3600")
        if not 1 <= self.retention_hours <= 360:
            raise ValueError("retention_hours must be 1 to 360")
        if not 0.05 <= self.poll_interval_seconds <= 5:
            raise ValueError("poll_interval_seconds must be 0.05 to 5")
        if not 5 <= self.membership_refresh_seconds <= 600:
            raise ValueError("membership_refresh_seconds must be 5 to 600")
        if not 1 <= self.membership_timeout_seconds <= 60:
            raise ValueError("membership_timeout_seconds must be 1 to 60")
        if not 5 <= self.empty_channel_timeout_seconds <= 3600:
            raise ValueError("empty_channel_timeout_seconds must be 5 to 3600")
        if not 0.5 <= self.connection_check_seconds <= 30:
            raise ValueError("connection_check_seconds must be 0.5 to 30")
        if not 3 <= self.disconnect_grace_seconds <= 120:
            raise ValueError("disconnect_grace_seconds must be 3 to 120")
        if not 0.5 <= self.browser_operation_timeout_seconds <= 15:
            raise ValueError("browser_operation_timeout_seconds must be 0.5 to 15")
        if not 30 <= self.reconnect_window_seconds <= 3600:
            raise ValueError("reconnect_window_seconds must be 30 to 3600")
        if not 0.25 <= self.reconnect_initial_delay_seconds <= 60:
            raise ValueError("reconnect_initial_delay_seconds must be 0.25 to 60")
        if not 1 <= self.reconnect_max_delay_seconds <= 300:
            raise ValueError("reconnect_max_delay_seconds must be 1 to 300")
        if self.reconnect_max_delay_seconds < self.reconnect_initial_delay_seconds:
            raise ValueError("reconnect maximum must not be smaller than reconnect initial delay")
        if not 5 <= self.reconnect_attempt_timeout_seconds <= 120:
            raise ValueError("reconnect_attempt_timeout_seconds must be 5 to 120")
        if self.language not in {"auto", "zh", "en", "yue", "ja", "ko"}:
            raise ValueError("unsupported language")
        if self.max_runtime_seconds is not None and self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def next_local_cutoff(now: datetime, hour: int = 4) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    candidate = datetime.combine(now.date(), time(hour=hour), tzinfo=now.tzinfo)
    if candidate <= now:
        candidate = datetime.combine(now.date() + timedelta(days=1), time(hour=hour), tzinfo=now.tzinfo)
    return candidate


def estimate_browser_clock_origin(chunk: dict[str, Any], session_elapsed_ms: float) -> float:
    sample_rate = int(chunk.get("sampleRate") or 0)
    frame_count = int(chunk.get("frameCount") or 0)
    if sample_rate <= 0 or frame_count < 0:
        raise ValueError("invalid browser PCM metadata")
    frame_ms = frame_count * 1000.0 / sample_rate
    browser_chunk_start_ms = float(chunk.get("sessionOffsetMs") or 0) - frame_ms
    return browser_chunk_start_ms - session_elapsed_ms


def rebase_browser_chunk(chunk: dict[str, Any], base_offset_ms: float) -> dict[str, Any]:
    value = dict(chunk)
    sample_rate = int(value.get("sampleRate") or 0)
    frame_count = int(value.get("frameCount") or 0)
    if sample_rate <= 0 or frame_count < 0:
        raise ValueError("invalid browser PCM metadata")
    frame_ms = frame_count * 1000.0 / sample_rate
    value["sessionOffsetMs"] = max(frame_ms, float(value.get("sessionOffsetMs") or 0) - base_offset_ms)
    return value


def _direct_session(output_root: Path, session_id: str) -> Path:
    session_id = validate_session_id(session_id)
    root = output_root.resolve()
    session = (root / session_id).resolve()
    if session.parent != root or session == root:
        raise ValueError("unsafe session path")
    return session


def request_stop(
    output_root: Path,
    session_id: str,
    *,
    requested_by: dict[str, Any] | None = None,
    reason: str = "operator_stop_command",
) -> Path:
    session = _direct_session(output_root, session_id)
    lifecycle_path = session / "lifecycle.json"
    if not lifecycle_path.is_file() or _is_reparse_point(session):
        raise ValueError(f"active managed Session not found: {session_id}")
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    if lifecycle.get("managed_by") != "oopz-worker-v1" or lifecycle.get("mode") != "continuous":
        raise ValueError("refusing to stop a non-continuous or unmanaged Session")
    if lifecycle.get("status") not in {"connecting", "recording", "reconnecting", "stopping"}:
        raise ValueError(f"Session is not active; current status={lifecycle.get('status')}")
    value = {
        "schema_version": CONTINUOUS_STOP_SCHEMA,
        "session_id": session_id,
        "requested_at": _iso(utc_now()),
        "reason": reason,
        "requested_by": requested_by or {"source": "local_cli"},
    }
    path = session / "control" / "stop.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _purge_chunk_audio(session_dir: Path, chunk_dir: Path) -> list[str]:
    session_dir = session_dir.resolve()
    chunk_dir = chunk_dir.resolve()
    chunks_root = (session_dir / "chunks").resolve()
    if chunk_dir.parent != chunks_root or chunks_root.parent != session_dir:
        raise ValueError("refusing unsafe chunk path")
    audio_dir = chunk_dir / "audio"
    deleted: list[str] = []
    if audio_dir.exists():
        if not audio_dir.is_dir() or _is_reparse_point(audio_dir):
            raise ValueError("refusing unsafe chunk audio directory")
        targets = list(audio_dir.iterdir())
        for target in targets:
            if not target.is_file() or _is_reparse_point(target):
                raise ValueError(f"refusing unexpected chunk audio target: {target}")
        deleted = [str(path.relative_to(session_dir)).replace("\\", "/") for path in targets]
        for target in targets:
            target.unlink()
        audio_dir.rmdir()
    manifest_path = chunk_dir / "audio_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        deleted_at = _iso(utc_now())
        for item in manifest:
            raw_path = str(item.get("path") or "").strip()
            if raw_path:
                recorded_path = Path(raw_path).resolve()
                if recorded_path.parent != audio_dir.resolve():
                    raise ValueError(f"refusing audio manifest path outside managed directory: {recorded_path}")
                if recorded_path.exists():
                    raise RuntimeError(f"audio cleanup did not remove recorded file: {recorded_path}")
            item["audio_deleted"] = True
            item["audio_deleted_at"] = deleted_at
        write_json(manifest_path, manifest)
    return deleted


def _enforce_chunk_audio_policy(session_dir: Path, chunk_dir: Path, *, retain_audio: bool) -> list[str]:
    """Reconcile disk, manifest and lifecycle after a normal or repaired transcript."""
    if retain_audio:
        return []
    deleted = _purge_chunk_audio(session_dir, chunk_dir)
    lifecycle_path = chunk_dir / "lifecycle.json"
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    deleted_at = _iso(utc_now())
    prior_deleted = [str(item) for item in lifecycle.get("deleted_audio_files", [])]
    lifecycle.update({
        "audio_deleted": True,
        "audio_retained_for_testing": False,
        "audio_deleted_at": lifecycle.get("audio_deleted_at") or deleted_at,
        "deleted_audio_files": list(dict.fromkeys(prior_deleted + deleted)),
    })
    write_json(lifecycle_path, lifecycle)
    return deleted


def _empty_transcript(chunk_dir: Path, language: str) -> None:
    marker = no_text_marker(chunk_dir)
    write_jsonl(chunk_dir / "transcript.jsonl", [marker])
    session = json.loads((chunk_dir / "session.json").read_text(encoding="utf-8"))
    write_json(chunk_dir / "transcript_summary.json", {
        "session_id": session["session_id"],
        "capture_clock_started_at": session["capture_clock_started_at"],
        "asr_backend": "sensevoice-small",
        "segments": 1,
        "language_request": language,
        "no_remote_audio": True,
        "no_speech": True,
    })
    render_transcript_markdown(chunk_dir, [marker])


async def _process_chunk(
    session_dir: Path,
    chunk_dir: Path,
    request: ContinuousRequest,
    vad_config: VADConfig,
    device: str,
    reset_deadline: bool = False,
) -> dict[str, Any]:
    metadata_path = chunk_dir / "chunk.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    closed_at = datetime.fromisoformat(str(metadata["closed_at"]).replace("Z", "+00:00"))
    deadline_base = utc_now() if reset_deadline else closed_at
    deadline_at = deadline_base + timedelta(seconds=request.processing_deadline_seconds)
    lifecycle_path = chunk_dir / "lifecycle.json"
    state = {
        "schema_version": "oopz.chunk.lifecycle.v1",
        "chunk_id": metadata["chunk_id"],
        "chunk_index": metadata["chunk_index"],
        "status": "transcribing",
        "processing_deadline_at": _iso(deadline_at),
        "audio_deleted": False,
        "audio_retained_for_testing": request.retain_audio,
        "failure": None,
        "connection_episode": 0,
        "successful_connections": 0,
        "reconnect_count": 0,
        "connection_attempts": 0,
        "reconnect_attempts": 0,
    }
    write_json(lifecycle_path, state)
    emit_event("chunk.processing_started", request.request_id, session_id=session_dir.name, chunk_id=metadata["chunk_id"], chunk_index=metadata["chunk_index"])
    print(
        f"[转写进度] 分片 {metadata['chunk_index']}：开始转写（语言={request.language}）。",
        flush=True,
    )
    try:
        manifest = json.loads((chunk_dir / "audio_manifest.json").read_text(encoding="utf-8"))
        if manifest:
            remaining = (deadline_at - utc_now()).total_seconds()
            if remaining <= 0:
                raise TimeoutError("chunk waited past its processing deadline")
            adapter = WorkflowRequest(
                request_id=request.request_id,
                area_id=request.area_id,
                channel_id=request.channel_id,
                duration_seconds=float(request.chunk_seconds),
                consent_confirmed=True,
                language=request.language,
                processing_deadline_seconds=request.processing_deadline_seconds,
                retention_hours=request.retention_hours,
                poll_interval_seconds=request.poll_interval_seconds,
                rtc_uid=request.rtc_uid,
                requested_by=request.requested_by,
            )
            stdout, stderr = await _run_transcription_process(chunk_dir, adapter, vad_config, device, remaining)
            (chunk_dir / "transcription.log").write_text(stdout + stderr, encoding="utf-8")
        else:
            _empty_transcript(chunk_dir, request.language)
        validated = validate_transcript(chunk_dir)
        deleted = [] if request.retain_audio else _purge_chunk_audio(session_dir, chunk_dir)
        completed_at = utc_now()
        state.update({
            "status": "transcribed",
            "completed_at": _iso(completed_at),
            "transcript_segments": validated["segment_count"],
            "audio_deleted": not request.retain_audio,
            "audio_retained_for_testing": request.retain_audio,
            "audio_deleted_at": _iso(completed_at) if not request.retain_audio else None,
            "deleted_audio_files": deleted,
            "deadline_met": completed_at <= deadline_at,
        })
        write_json(lifecycle_path, state)
        emit_event("chunk.transcribed", request.request_id, session_id=session_dir.name, chunk_id=metadata["chunk_id"], chunk_index=metadata["chunk_index"], transcript_segments=validated["segment_count"], audio_deleted=not request.retain_audio, audio_retained_for_testing=request.retain_audio)
        print(
            f"[转写进度] 分片 {metadata['chunk_index']}：完成；本分片段落={validated['segment_count']}；"
            f"音频{'已删除' if not request.retain_audio else '已保留'}。",
            flush=True,
        )
        return {"chunk_dir": chunk_dir, "ok": True, "segments": validated["segment_count"]}
    except Exception as error:
        state.update({
            "status": "failed",
            "failed_at": _iso(utc_now()),
            "failure": {"type": type(error).__name__, "message": str(error)},
            "audio_deleted": False,
        })
        write_json(lifecycle_path, state)
        emit_event("chunk.failed", request.request_id, session_id=session_dir.name, chunk_id=metadata["chunk_id"], chunk_index=metadata["chunk_index"], error_type=type(error).__name__, error=str(error), audio_retained=True)
        print(
            f"[转写进度] 分片 {metadata['chunk_index']}：失败；{type(error).__name__}: {str(error)[:300]}",
            flush=True,
        )
        return {"chunk_dir": chunk_dir, "ok": False, "error": str(error)}


def _merge_transcripts(session_dir: Path, chunk_results: list[dict[str, Any]]) -> tuple[int, Path]:
    records: list[dict[str, Any]] = []
    for result in chunk_results:
        if not result["ok"]:
            continue
        chunk_dir = Path(result["chunk_dir"])
        metadata = json.loads((chunk_dir / "chunk.json").read_text(encoding="utf-8"))
        offset_ms = int(metadata["session_offset_ms"])
        transcript_path = chunk_dir / "transcript.jsonl"
        with transcript_path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                item = json.loads(line)
                local_start = int(item["start_ms"])
                local_end = int(item["end_ms"])
                source_audio = item.pop("audio_file", None)
                item.pop("audio_start_sample", None)
                item.pop("audio_end_sample", None)
                item.update({
                    "session_id": session_dir.name,
                    "chunk_id": metadata["chunk_id"],
                    "chunk_index": metadata["chunk_index"],
                    "chunk_start_ms": local_start,
                    "chunk_end_ms": local_end,
                    "start_ms": offset_ms + local_start,
                    "end_ms": offset_ms + local_end,
                    "source_audio_deleted": True,
                    "source_audio_file": source_audio,
                })
                for key, local_ms in (("start_time", local_start), ("end_time", local_end)):
                    if key not in item:
                        continue
                    try:
                        local_dt = datetime.fromisoformat(str(item[key]).replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    item[key] = (local_dt + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds")
                records.append(item)
    records.sort(key=lambda item: (int(item["start_ms"]), int(item["agora_uid"]), int(item["end_ms"])))
    write_jsonl(session_dir / "transcript.jsonl", records)
    write_json(session_dir / "transcript_summary.json", {
        "session_id": session_dir.name,
        "segments": len(records),
        "chunks_total": len(chunk_results),
        "chunks_transcribed": sum(1 for item in chunk_results if item["ok"]),
        "chunks_failed": sum(1 for item in chunk_results if not item["ok"]),
        "asr_backend": "sensevoice-small",
    })
    markdown = render_transcript_markdown(session_dir, records)
    return len(records), markdown


def _write_final_handoff(
    session_dir: Path,
    request: ContinuousRequest,
    *,
    stopped_at: datetime,
    delete_after: datetime,
    segment_count: int,
    chunk_results: list[dict[str, Any]],
    analysis_requested_at: datetime | None = None,
) -> Path:
    path = session_dir / "handoff" / "analyzer_request.json"
    write_json(path, {
        "schema_version": "oopz.analyzer.request.v1",
        "request_id": request.request_id,
        "session_id": session_dir.name,
        "created_at": _iso(utc_now()),
        "analysis_deadline_at": _iso((analysis_requested_at or stopped_at) + timedelta(seconds=request.processing_deadline_seconds)),
        "encoding": "UTF-8",
        "delivery_mode": "final_only",
        "summary_windows": {
            "short_summary_seconds": request.short_summary_seconds,
            "long_summary_seconds": request.long_summary_seconds,
        },
        "inputs": {
            "transcript_jsonl": "transcript.jsonl",
            "transcript_markdown": "transcript.md",
            "transcript_summary": "transcript_summary.json",
            "users": "users.json",
            "session": "session.json",
            "segment_count": segment_count,
            "chunks_total": len(chunk_results),
            "failed_chunk_ids": [Path(item["chunk_dir"]).name for item in chunk_results if not item["ok"]],
        },
        "required_outputs": {
            "analysis_result": "analysis/result.json",
            "human_summary": "analysis/summary.md",
            "report_messages": "handoff/report_messages.jsonl",
        },
        "retention": {
            "delete_after": _iso(delete_after),
            "maximum_hours": request.retention_hours,
            "audio_retained_for_testing": request.retain_audio,
        },
    })
    return path


def _saved_continuous_request(value: dict[str, Any]) -> ContinuousRequest:
    allowed = set(ContinuousRequest.__dataclass_fields__)
    request = ContinuousRequest(**{key: item for key, item in value.items() if key in allowed})
    request.validate()
    return request


async def repair_continuous_session(
    output_root: Path,
    session_id: str,
    *,
    device: str = "cpu",
    vad_config: VADConfig | None = None,
    chunk_processor: Any = _process_chunk,
) -> Path:
    session_dir = _direct_session(output_root, session_id)
    if not session_dir.is_dir() or _is_reparse_point(session_dir):
        raise ValueError(f"managed continuous Session not found: {session_id}")
    lifecycle_path = session_dir / "lifecycle.json"
    request_path = session_dir / "request.json"
    if not lifecycle_path.is_file() or not request_path.is_file():
        raise ValueError("Session lifecycle or request is missing")
    lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
    if lifecycle.get("managed_by") != "oopz-worker-v1" or lifecycle.get("mode") != "continuous":
        raise ValueError("refusing to repair a non-continuous or unmanaged Session")
    if lifecycle.get("status") in {"connecting", "recording", "reconnecting", "stopping"}:
        raise ValueError("cannot repair an active Session")
    request = _saved_continuous_request(json.loads(request_path.read_text(encoding="utf-8")))
    chunks_root = session_dir / "chunks"
    if not chunks_root.is_dir() or _is_reparse_point(chunks_root):
        raise ValueError("unsafe or missing chunks directory")
    results: list[dict[str, Any]] = []
    for chunk_dir in sorted(chunks_root.iterdir()):
        if not chunk_dir.is_dir() or _is_reparse_point(chunk_dir):
            raise ValueError(f"unsafe chunk entry: {chunk_dir}")
        chunk_lifecycle_path = chunk_dir / "lifecycle.json"
        chunk_lifecycle: dict[str, Any]
        if chunk_lifecycle_path.is_file():
            try:
                loaded = json.loads(chunk_lifecycle_path.read_text(encoding="utf-8"))
                chunk_lifecycle = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                # A half-written chunk lifecycle from a hard kill is treated as a
                # failed chunk below, so repair still converges on the session.
                chunk_lifecycle = {}
        else:
            # Chunks queued but never processed before the crash have no
            # lifecycle yet; the audio branch below retries or records failure.
            chunk_lifecycle = {}
        if chunk_lifecycle.get("status") == "transcribed":
            try:
                validated = validate_transcript(chunk_dir)
                _enforce_chunk_audio_policy(
                    session_dir, chunk_dir, retain_audio=request.retain_audio,
                )
                results.append({"chunk_dir": chunk_dir, "ok": True, "segments": validated["segment_count"]})
            except Exception as error:
                results.append({"chunk_dir": chunk_dir, "ok": False, "error": str(error)})
            continue
        audio_dir = chunk_dir / "audio"
        if not audio_dir.is_dir() or _is_reparse_point(audio_dir):
            results.append({"chunk_dir": chunk_dir, "ok": False, "error": "retained audio is missing or unsafe"})
            continue
        result = await chunk_processor(
            session_dir, chunk_dir, request, vad_config or VADConfig(), device,
            reset_deadline=True,
        )
        if result.get("ok"):
            try:
                _enforce_chunk_audio_policy(
                    session_dir, chunk_dir, retain_audio=request.retain_audio,
                )
            except Exception as error:
                result = {"chunk_dir": chunk_dir, "ok": False, "error": str(error)}
        results.append(result)
    segment_count, markdown = _merge_transcripts(session_dir, results)
    # Hard-killed sessions were retired as "interrupted" and never wrote a
    # stopped_at; fall back to the interrupted/started markers so repair can
    # still converge instead of crashing after all transcription work.
    stopped_at_text = str(lifecycle.get("stopped_at") or lifecycle.get("interrupted_at") or lifecycle.get("started_at") or "").strip()
    if not stopped_at_text:
        raise ValueError("Session lifecycle lacks a usable stop time for repair")
    stopped_at = datetime.fromisoformat(stopped_at_text.replace("Z", "+00:00"))
    delete_after_text = str(lifecycle.get("delete_after") or "").strip()
    if delete_after_text:
        delete_after = datetime.fromisoformat(delete_after_text.replace("Z", "+00:00"))
    else:
        # Keep a recovered session for the maximum retention horizon when the
        # crash happened before the deadline was persisted.
        delete_after = stopped_at + timedelta(hours=360)
    handoff = _write_final_handoff(
        session_dir, request, stopped_at=stopped_at, delete_after=delete_after,
        segment_count=segment_count, chunk_results=results, analysis_requested_at=utc_now(),
    )
    failed = [item for item in results if not item["ok"]]
    lifecycle.update({
        "status": "ready_for_analysis" if not failed else "ready_for_analysis_with_errors",
        "repair_completed_at": _iso(utc_now()),
        "chunks_total": len(results),
        "chunks_transcribed": len(results) - len(failed),
        "chunks_failed": len(failed),
        "transcript_segments": segment_count,
        "audio_deleted": not request.retain_audio and not failed,
        "audio_retained_for_testing": request.retain_audio,
        "analyzer_handoff": str(handoff.relative_to(session_dir)).replace("\\", "/"),
        "repair_failures": [str(item.get("error") or "unknown") for item in failed],
    })
    write_json(lifecycle_path, lifecycle)
    emit_event(
        "continuous.repair_completed", request.request_id, session_id=session_id,
        chunks_total=len(results), chunks_failed=len(failed), transcript_segments=segment_count,
        transcript_markdown=str(markdown), analyzer_request=str(handoff),
        audio_deleted=not request.retain_audio and not failed,
        audio_retained_for_testing=request.retain_audio,
    )
    return session_dir


def reconnect_delay(attempt: int, initial_seconds: float, maximum_seconds: float) -> float:
    """Return bounded exponential backoff for a one-based reconnect attempt."""
    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    # Cap the exponent so long-lived sessions with a sub-second max delay do
    # not overflow float conversion (2**1024) inside the reconnect handler.
    return min(maximum_seconds, initial_seconds * (2 ** min(attempt - 1, 62)))


def _append_connectivity_event(session_dir: Path, event: str, **fields: Any) -> None:
    path = session_dir / "debug" / "connectivity_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    value = {"at": _iso(utc_now()), "event": event, **fields}
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


async def refresh_participants_safely(
    bot: Any,
    request: ContinuousRequest,
    participants_by_uid: dict[str, Any],
    current_participants: list[Any] | None = None,
) -> tuple[bool, str | None]:
    """Refresh identity metadata without allowing REST failures to stop audio."""
    try:
        participants = await asyncio.wait_for(
            _resolve_participants(bot, request.area_id, request.channel_id),
            timeout=request.membership_timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        return False, f"{type(error).__name__}: {str(error)[:500]}"
    if current_participants is not None:
        current_participants[:] = participants
    for participant in participants:
        participants_by_uid[participant.oopz_uid] = participant
    return True, None


def count_other_members(participants: list[Any], *, self_oopz_uid: str = "") -> int:
    """Count current channel members other than this recorder/bot."""
    normalized_self = str(self_oopz_uid or "").strip()
    return sum(
        1
        for participant in participants
        if not bool(getattr(participant, "is_bot", False))
        and str(getattr(participant, "oopz_uid", "") or "").strip() != normalized_self
    )


async def run_continuous_capture(
    config: Any,
    request: ContinuousRequest,
    *,
    output_root: Path,
    device: str = "cpu",
    vad_config: VADConfig | None = None,
    session_id: str | None = None,
) -> Path:
    from oopz_sdk import OopzBot

    request.validate()
    output_root = output_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    # Feishu owns retention deletion so it can remove the remote document and
    # Base record before deleting the matching local Session and PDFs.

    started_at = utc_now()
    session_id = validate_session_id(session_id) if session_id else new_session_id(
        output_root, started_at=started_at
    )
    session_dir = output_root / session_id
    if session_dir.exists():
        raise ValueError(f"Session already exists: {session_id}")
    chunks_dir = session_dir / "chunks"
    chunks_dir.mkdir(parents=True)
    local_now = datetime.now(BEIJING_TZ)
    cutoff_local = next_local_cutoff(local_now, request.cutoff_local_hour)
    delete_after = started_at + timedelta(hours=request.retention_hours)
    lifecycle = {
        "schema_version": "oopz.lifecycle.v1",
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "request_id": request.request_id,
        "session_id": session_id,
        "status": "connecting",
        "started_at": _iso(started_at),
        "cutoff_local": cutoff_local.isoformat(timespec="seconds"),
        "cutoff_at": _iso(cutoff_local.astimezone(timezone.utc)),
        "chunk_seconds": request.chunk_seconds,
        "empty_channel_timeout_seconds": request.empty_channel_timeout_seconds,
        "delete_after": _iso(delete_after),
        "audio_deleted": False,
        "failure": None,
    }
    write_json(session_dir / "request.json", request.to_dict())
    write_json(session_dir / "lifecycle.json", lifecycle)
    write_json(session_dir / "session.json", {
        "session_id": session_id,
        "mode": "continuous",
        "started_at": _iso(started_at),
        "capture_clock_started_at": _iso(started_at),
        "area": request.area_id,
        "channel": request.channel_id,
        "chunk_seconds": request.chunk_seconds,
        "short_summary_seconds": request.short_summary_seconds,
        "long_summary_seconds": request.long_summary_seconds,
        "resilience": {
            "membership_refresh_seconds": request.membership_refresh_seconds,
            "empty_channel_timeout_seconds": request.empty_channel_timeout_seconds,
            "audio_retained_for_testing": request.retain_audio,
            "disconnect_grace_seconds": request.disconnect_grace_seconds,
            "reconnect_window_seconds": request.reconnect_window_seconds,
            "reconnect_initial_delay_seconds": request.reconnect_initial_delay_seconds,
            "reconnect_max_delay_seconds": request.reconnect_max_delay_seconds,
        },
    })
    emit_event("continuous.connecting", request.request_id, session_id=session_id, chunk_seconds=request.chunk_seconds, cutoff_local=lifecycle["cutoff_local"])

    write_json(session_dir / "users.json", [])
    bot = OopzBot(config)
    probe = AgoraBrowserProbe(bot.voice.backend)
    queue: asyncio.Queue[Path | None] = asyncio.Queue()
    results: list[dict[str, Any]] = []
    processing_config = vad_config or VADConfig()

    async def consumer() -> None:
        while True:
            chunk_dir = await queue.get()
            try:
                if chunk_dir is None:
                    return
                results.append(await _process_chunk(session_dir, chunk_dir, request, processing_config, device))
            finally:
                queue.task_done()

    consumer_task = asyncio.create_task(consumer())
    recorder: CaptureRecorder | None = None
    current_chunk_dir: Path | None = None
    current_chunk_id: str | None = None
    current_chunk_index = 0
    current_chunk_started_at = started_at
    current_chunk_started_monotonic = 0.0
    current_base_offset_ms = 0.0
    browser_clock_origin_ms: float | None = None
    participants_by_uid: dict[str, Any] = {}
    joined = False
    capture_started = False
    connected = False
    ever_connected = False
    stop_reason = "unknown"
    failure: BaseException | None = None
    final_snapshot: ProbeSnapshot | None = None
    self_agora_uid: Any = None
    sign: Any = None
    capture_started_wall = started_at
    capture_stopped_at = started_at
    current_connection_episode = 0
    connection_attempts = 0
    reconnect_attempts = 0
    successful_connections = 0
    total_disconnected_seconds = 0.0
    membership_refresh_successes = 0
    membership_refresh_failures = 0
    membership_consecutive_failures = 0
    current_membership: list[Any] = []
    empty_channel_since: float | None = None
    empty_channel_since_wall: datetime | None = None
    membership_task: asyncio.Task[tuple[bool, str | None]] | None = None
    health_task: asyncio.Task[ProbeSnapshot] | None = None
    next_membership_refresh = 0.0
    next_connection_check = 0.0
    unhealthy_since: float | None = None
    last_connection_state = "unknown"
    debug_events: list[dict[str, Any]] = []
    debug_event_keys: set[str] = set()

    def begin_chunk(loop_time: float, wall_time: datetime, base_offset_ms: float) -> None:
        nonlocal recorder, current_chunk_dir, current_chunk_id, current_chunk_index
        nonlocal current_chunk_started_at, current_chunk_started_monotonic, current_base_offset_ms
        current_chunk_index += 1
        current_chunk_id = str(uuid4())
        current_chunk_dir = chunks_dir / f"{current_chunk_index:06d}-{current_chunk_id}"
        current_chunk_dir.mkdir()
        recorder = CaptureRecorder(current_chunk_dir)
        current_chunk_started_at = wall_time
        current_chunk_started_monotonic = loop_time
        current_base_offset_ms = base_offset_ms
        emit_event("chunk.started", request.request_id, session_id=session_id, chunk_id=current_chunk_id, chunk_index=current_chunk_index)
        print(f"[录音进度] 分片 {current_chunk_index}：开始录音。", flush=True)

    def ingest_chunk(chunk: dict[str, Any], loop_time: float) -> None:
        nonlocal browser_clock_origin_ms, current_base_offset_ms
        if recorder is None:
            return
        if browser_clock_origin_ms is None:
            elapsed_ms = max(0.0, (loop_time - capture_started_monotonic) * 1000.0)
            browser_clock_origin_ms = estimate_browser_clock_origin(chunk, elapsed_ms)
            chunk_start_elapsed_ms = max(0.0, (current_chunk_started_monotonic - capture_started_monotonic) * 1000.0)
            current_base_offset_ms = browser_clock_origin_ms + chunk_start_elapsed_ms
        recorder.ingest(rebase_browser_chunk(chunk, current_base_offset_ms))

    def remember_snapshot(snapshot: ProbeSnapshot) -> None:
        nonlocal final_snapshot
        final_snapshot = snapshot
        for event in snapshot.events:
            key = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if key in debug_event_keys:
                continue
            debug_event_keys.add(key)
            debug_events.append(event)
        if len(debug_events) > 20000:
            removed = debug_events[:-20000]
            del debug_events[:-20000]
            for event in removed:
                debug_event_keys.discard(json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    async def safe_snapshot() -> ProbeSnapshot:
        try:
            snapshot = await asyncio.wait_for(
                probe.snapshot(), timeout=request.browser_operation_timeout_seconds,
            )
        except Exception as error:
            _append_connectivity_event(
                session_dir, "probe_snapshot_failed",
                error_type=type(error).__name__, error=str(error)[:500],
            )
            return ProbeSnapshot(connection_state="unavailable")
        remember_snapshot(snapshot)
        return snapshot

    async def checked_snapshot() -> ProbeSnapshot:
        return await asyncio.wait_for(
            probe.snapshot(), timeout=request.browser_operation_timeout_seconds,
        )

    async def close_chunk(
        closed_at: datetime,
        elapsed_session_ms: int,
        snapshot: ProbeSnapshot | None = None,
    ) -> None:
        nonlocal recorder, current_chunk_dir, current_chunk_id
        if recorder is None or current_chunk_dir is None or current_chunk_id is None:
            return
        manifest = recorder.close()
        snapshot = snapshot or await safe_snapshot()
        mappings = build_identity_mappings(
            list(participants_by_uid.values()), snapshot,
            self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
            self_agora_uid=self_agora_uid,
        )
        duration = max(0.0, (closed_at - current_chunk_started_at).total_seconds())
        chunk_session = {
            "session_id": current_chunk_id,
            "parent_session_id": session_id,
            "chunk_id": current_chunk_id,
            "chunk_index": current_chunk_index,
            "started_at": _iso(current_chunk_started_at),
            "finished_at": _iso(closed_at),
            "capture_clock_started_at": _iso(current_chunk_started_at),
            "duration_seconds": duration,
            "session_offset_ms": max(0, elapsed_session_ms - round(duration * 1000)),
            "area": request.area_id,
            "channel": request.channel_id,
            "agora_room_id": str(getattr(sign, "rtc_channel_name", "") or ""),
            "probe_version": PROBE_VERSION,
            "track_count": len(manifest),
            "connection_episode": current_connection_episode,
        }
        write_json(current_chunk_dir / "session.json", chunk_session)
        write_json(current_chunk_dir / "chunk.json", {
            "chunk_id": current_chunk_id,
            "chunk_index": current_chunk_index,
            "parent_session_id": session_id,
            "started_at": _iso(current_chunk_started_at),
            "closed_at": _iso(closed_at),
            "duration_seconds": duration,
            "session_offset_ms": chunk_session["session_offset_ms"],
            "connection_episode": current_connection_episode,
        })
        write_json(current_chunk_dir / "users.json", [item.to_dict() for item in mappings])
        write_json(current_chunk_dir / "audio_manifest.json", manifest)
        write_json(session_dir / "users.json", [item.to_dict() for item in mappings])
        await queue.put(current_chunk_dir)
        emit_event("chunk.closed", request.request_id, session_id=session_id, chunk_id=current_chunk_id, chunk_index=current_chunk_index, duration_seconds=round(duration, 3), track_count=len(manifest))
        print(
            f"[录音进度] 分片 {current_chunk_index}：录音结束，时长={duration:.1f}秒，"
            f"音轨={len(manifest)}；已加入转写队列。",
            flush=True,
        )
        recorder = None
        current_chunk_dir = None
        current_chunk_id = None

    def terminal_reason(loop_time: float) -> str | None:
        stop_path = session_dir / "control" / "stop.json"
        if stop_path.is_file():
            stop_value = json.loads(stop_path.read_text(encoding="utf-8"))
            return str(stop_value.get("reason") or "operator_stop_command")
        if datetime.now(BEIJING_TZ) >= cutoff_local:
            return "automatic_03_00_cutoff"
        if (
            ever_connected
            and request.max_runtime_seconds is not None
            and loop_time - capture_started_monotonic >= request.max_runtime_seconds
        ):
            return "diagnostic_max_runtime"
        return None

    def update_empty_channel_state(loop_time: float, wall_time: datetime) -> bool:
        """Return true once a successful membership snapshot stayed empty long enough."""
        nonlocal empty_channel_since, empty_channel_since_wall
        other_count = count_other_members(
            current_membership,
            self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
        )
        if other_count > 0:
            if empty_channel_since is not None:
                _append_connectivity_event(
                    session_dir,
                    "empty_channel_cleared",
                    other_member_count=other_count,
                    empty_seconds=round(loop_time - empty_channel_since, 3),
                )
            empty_channel_since = None
            empty_channel_since_wall = None
            lifecycle.pop("empty_channel_since", None)
            lifecycle["last_other_member_count"] = other_count
            write_json(session_dir / "lifecycle.json", lifecycle)
            return False
        if empty_channel_since is None:
            empty_channel_since = loop_time
            empty_channel_since_wall = wall_time
            lifecycle["empty_channel_since"] = _iso(wall_time)
            lifecycle["last_other_member_count"] = 0
            write_json(session_dir / "lifecycle.json", lifecycle)
            _append_connectivity_event(
                session_dir,
                "empty_channel_started",
                timeout_seconds=request.empty_channel_timeout_seconds,
            )
            return False
        elapsed = loop_time - empty_channel_since
        if elapsed < request.empty_channel_timeout_seconds:
            return False
        _append_connectivity_event(
            session_dir,
            "empty_channel_timeout",
            timeout_seconds=request.empty_channel_timeout_seconds,
            verified_empty_seconds=round(elapsed, 3),
            empty_since=_iso(empty_channel_since_wall or wall_time),
        )
        emit_event(
            "continuous.empty_channel_timeout",
            request.request_id,
            session_id=session_id,
            timeout_seconds=request.empty_channel_timeout_seconds,
            verified_empty_seconds=round(elapsed, 3),
        )
        return True

    async def stop_voice_episode() -> ProbeSnapshot:
        nonlocal capture_started, joined, connected, capture_stopped_at
        snapshot = ProbeSnapshot(connection_state="unavailable")
        if capture_started:
            try:
                await asyncio.wait_for(
                    probe.stop_audio_capture(), timeout=request.browser_operation_timeout_seconds,
                )
            except Exception as error:
                _append_connectivity_event(
                    session_dir, "audio_capture_stop_failed",
                    error_type=type(error).__name__, error=str(error)[:500],
                )
            while True:
                try:
                    chunks = await asyncio.wait_for(
                        probe.drain_audio(256), timeout=request.browser_operation_timeout_seconds,
                    )
                except Exception:
                    break
                if not chunks:
                    break
                loop_time = asyncio.get_running_loop().time()
                for chunk in chunks:
                    try:
                        ingest_chunk(chunk, loop_time)
                    except Exception:
                        # Teardown draining must not fail the session on one bad chunk.
                        continue
            snapshot = await safe_snapshot()
            capture_stopped_at = utc_now()
            elapsed_ms = round(max(0.0, (capture_stopped_at - capture_started_wall).total_seconds()) * 1000)
            await close_chunk(capture_stopped_at, elapsed_ms, snapshot)
        if joined:
            try:
                await asyncio.wait_for(
                    bot.voice.leave(), timeout=min(10.0, request.reconnect_attempt_timeout_seconds),
                )
            except Exception as error:
                _append_connectivity_event(
                    session_dir, "voice_leave_failed",
                    error_type=type(error).__name__, error=str(error)[:500],
                )
        capture_started = False
        joined = False
        connected = False
        return snapshot

    async def connect_voice_episode(*, rebuild_backend: bool) -> None:
        nonlocal probe, capture_started, joined, connected, sign, self_agora_uid
        nonlocal browser_clock_origin_ms, current_connection_episode
        if rebuild_backend:
            try:
                await asyncio.wait_for(
                    bot.voice.close(), timeout=min(10.0, request.reconnect_attempt_timeout_seconds),
                )
            except Exception as error:
                _append_connectivity_event(
                    session_dir, "voice_backend_close_failed",
                    error_type=type(error).__name__, error=str(error)[:500],
                )
        await asyncio.wait_for(bot.voice.start(), timeout=request.reconnect_attempt_timeout_seconds)
        probe = AgoraBrowserProbe(bot.voice.backend)
        await asyncio.wait_for(probe.install(), timeout=request.browser_operation_timeout_seconds)
        await asyncio.wait_for(probe.start_audio_capture(), timeout=request.browser_operation_timeout_seconds)
        capture_started = True
        try:
            sign = await asyncio.wait_for(
                bot.voice.join(
                    area=request.area_id,
                    channel=request.channel_id,
                    rtc_uid=request.rtc_uid,
                ),
                timeout=request.reconnect_attempt_timeout_seconds,
            )
        except BaseException:
            try:
                await asyncio.wait_for(
                    probe.stop_audio_capture(), timeout=request.browser_operation_timeout_seconds,
                )
            except Exception:
                LOGGER.debug("audio capture stop failed during connect cleanup", exc_info=True)
            capture_started = False
            raise
        joined = True
        connected = True
        current_connection_episode += 1
        browser_clock_origin_ms = None
        try:
            backend_status = await asyncio.wait_for(
                bot.voice.backend.get_status(), timeout=request.browser_operation_timeout_seconds,
            )
            self_agora_uid = backend_status.get("currentAgoraUid")
        except Exception:
            self_agora_uid = None

    try:
        loop = asyncio.get_running_loop()
        capture_started_monotonic = loop.time()
        outage_started = loop.time()
        next_connect_at = loop.time()
        reconnect_attempt_in_outage = 0
        rebuild_backend = False

        while True:
            loop_time = loop.time()
            requested_stop = terminal_reason(loop_time)
            if requested_stop is not None:
                stop_reason = requested_stop
                break
            if not connected:
                if loop_time - outage_started >= request.reconnect_window_seconds:
                    if ever_connected:
                        total_disconnected_seconds += max(0.0, loop_time - outage_started)
                    failure = ReconnectWindowExpired(
                        f"voice could not reconnect within {request.reconnect_window_seconds:g} seconds"
                    )
                    stop_reason = "reconnect_window_expired"
                    break
                if loop_time < next_connect_at:
                    await asyncio.sleep(min(request.poll_interval_seconds, next_connect_at - loop_time))
                    continue
                reconnect_attempt_in_outage += 1
                connection_attempts += 1
                if ever_connected:
                    reconnect_attempts += 1
                _append_connectivity_event(
                    session_dir, "connect_attempt",
                    attempt=reconnect_attempt_in_outage,
                    reconnect=ever_connected,
                )
                try:
                    await connect_voice_episode(rebuild_backend=rebuild_backend)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    delay = reconnect_delay(
                        reconnect_attempt_in_outage,
                        request.reconnect_initial_delay_seconds,
                        request.reconnect_max_delay_seconds,
                    )
                    next_connect_at = loop.time() + delay
                    rebuild_backend = True
                    lifecycle.update({
                        "status": "reconnecting" if ever_connected else "connecting",
                        "last_connection_error": {
                            "type": type(error).__name__, "message": str(error)[:500],
                        },
                        "connection_attempts": connection_attempts,
                        "reconnect_attempts": reconnect_attempts,
                    })
                    write_json(session_dir / "lifecycle.json", lifecycle)
                    _append_connectivity_event(
                        session_dir, "connect_failed",
                        attempt=reconnect_attempt_in_outage,
                        error_type=type(error).__name__, error=str(error)[:500],
                        retry_in_seconds=delay,
                    )
                    LOGGER.warning(
                        "voice connect failed; Session ID=%s attempt=%s retry_in=%.1fs: %s",
                        session_id, reconnect_attempt_in_outage, delay, error,
                    )
                    continue
                connected_at = utc_now()
                successful_connections += 1
                if ever_connected:
                    disconnected_seconds = max(0.0, loop.time() - outage_started)
                    total_disconnected_seconds += disconnected_seconds
                else:
                    capture_started_monotonic = loop.time()
                    capture_started_wall = connected_at
                    ever_connected = True
                    session_metadata = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
                    session_metadata["capture_clock_started_at"] = _iso(capture_started_wall)
                    session_metadata["connected_at"] = _iso(capture_started_wall)
                    write_json(session_dir / "session.json", session_metadata)
                session_elapsed_ms = round(max(0.0, (connected_at - capture_started_wall).total_seconds()) * 1000)
                begin_chunk(loop.time(), connected_at, 0.0)
                # Membership from a previous connection episode is stale. Require
                # a fresh successful snapshot before starting the empty-channel clock.
                current_membership.clear()
                empty_channel_since = None
                empty_channel_since_wall = None
                lifecycle.pop("empty_channel_since", None)
                next_membership_refresh = loop.time()
                reconnect_attempt_in_outage = 0
                rebuild_backend = False
                unhealthy_since = None
                last_connection_state = "CONNECTED"
                next_connection_check = loop.time()
                lifecycle.update({
                    "status": "recording",
                    "capture_started_at": _iso(capture_started_wall),
                    "connection_episode": current_connection_episode,
                    "successful_connections": successful_connections,
                    "reconnect_count": max(0, successful_connections - 1),
                    "connection_attempts": connection_attempts,
                    "reconnect_attempts": reconnect_attempts,
                    "last_connected_at": _iso(connected_at),
                    "last_connection_error": None,
                })
                write_json(session_dir / "lifecycle.json", lifecycle)
                _append_connectivity_event(
                    session_dir, "connected",
                    connection_episode=current_connection_episode,
                    reconnect=successful_connections > 1,
                    session_offset_ms=session_elapsed_ms,
                )
                if successful_connections == 1:
                    emit_event(
                        "continuous.recording", request.request_id,
                        session_id=session_id,
                        stop_command=f'oopz-continuous stop --session "{session_id}"',
                    )
                else:
                    emit_event(
                        "continuous.reconnected", request.request_id,
                        session_id=session_id,
                        connection_episode=current_connection_episode,
                        reconnect_count=successful_connections - 1,
                    )
                continue

            if loop_time - current_chunk_started_monotonic >= request.chunk_seconds:
                elapsed_ms = round((loop_time - capture_started_monotonic) * 1000)
                await close_chunk(utc_now(), elapsed_ms)
                begin_chunk(loop_time, utc_now(), float(elapsed_ms) + float(browser_clock_origin_ms or 0.0))

            disconnect_error: BaseException | None = None
            try:
                drained = await asyncio.wait_for(
                    probe.drain_audio(), timeout=request.browser_operation_timeout_seconds,
                )
                for chunk in drained:
                    try:
                        ingest_chunk(chunk, loop_time)
                    except (KeyError, ValueError, TypeError) as error:
                        # One malformed browser chunk (bad base64, unknown UID,
                        # rate change) is data corruption, not a connection
                        # loss: skip it instead of forcing a full reconnect.
                        _append_connectivity_event(
                            session_dir, "audio_chunk_skipped",
                            error=f"{type(error).__name__}: {error}",
                        )
            except Exception as error:
                disconnect_error = VoiceConnectionLost(
                    f"browser audio drain failed: {type(error).__name__}: {error}"
                )

            if membership_task is not None and membership_task.done():
                try:
                    ok, membership_error = membership_task.result()
                except asyncio.CancelledError:
                    raise
                membership_task = None
                if ok:
                    membership_refresh_successes += 1
                    membership_consecutive_failures = 0
                    next_membership_refresh = loop.time() + request.membership_refresh_seconds
                    _append_connectivity_event(
                        session_dir,
                        "membership_refreshed",
                        other_member_count=count_other_members(
                            current_membership,
                            self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
                        ),
                    )
                    if update_empty_channel_state(loop.time(), utc_now()):
                        stop_reason = "empty_channel_timeout"
                        break
                else:
                    membership_refresh_failures += 1
                    membership_consecutive_failures += 1
                    # A failed refresh is not evidence that the channel is empty.
                    # Restart the verified-empty clock after the next successful refresh.
                    empty_channel_since = None
                    empty_channel_since_wall = None
                    lifecycle.pop("empty_channel_since", None)
                    retry_delay = min(
                        120.0,
                        reconnect_delay(membership_consecutive_failures, 5.0, 120.0),
                    )
                    next_membership_refresh = loop.time() + retry_delay
                    _append_connectivity_event(
                        session_dir, "membership_refresh_failed",
                        consecutive_failures=membership_consecutive_failures,
                        error=membership_error,
                        retry_in_seconds=retry_delay,
                    )
                    LOGGER.warning(
                        "OOPZ membership refresh failed; audio capture continues; "
                        "Session ID=%s retry_in=%.1fs: %s",
                        session_id, retry_delay, membership_error,
                    )
            if membership_task is None and loop_time >= next_membership_refresh:
                _append_connectivity_event(session_dir, "membership_refresh_started")
                membership_task = asyncio.create_task(
                    refresh_participants_safely(
                        bot, request, participants_by_uid, current_membership,
                    ),
                    name="oopz_membership_refresh",
                )
            # If a refresh is due at the threshold, let it complete first so a
            # user who just joined is not missed by the empty-channel stop.
            if (
                membership_task is None
                and empty_channel_since is not None
                and loop.time() - empty_channel_since >= request.empty_channel_timeout_seconds
            ):
                if update_empty_channel_state(loop.time(), utc_now()):
                    stop_reason = "empty_channel_timeout"
                    break

            if health_task is not None and health_task.done():
                try:
                    snapshot = health_task.result()
                    remember_snapshot(snapshot)
                    state = snapshot.connection_state.strip().upper()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    state = "PROBE_ERROR"
                    _append_connectivity_event(
                        session_dir, "connection_check_failed",
                        error_type=type(error).__name__, error=str(error)[:500],
                    )
                health_task = None
                next_connection_check = loop.time() + request.connection_check_seconds
                if state == "CONNECTED":
                    if unhealthy_since is not None:
                        _append_connectivity_event(
                            session_dir, "connection_recovered_without_rejoin",
                            previous_state=last_connection_state,
                            unhealthy_seconds=round(loop.time() - unhealthy_since, 3),
                        )
                    unhealthy_since = None
                else:
                    if unhealthy_since is None:
                        unhealthy_since = loop.time()
                        _append_connectivity_event(session_dir, "connection_unhealthy", state=state)
                    elif loop.time() - unhealthy_since >= request.disconnect_grace_seconds:
                        disconnect_error = VoiceConnectionLost(
                            f"Agora state remained {state} for at least "
                            f"{request.disconnect_grace_seconds:g} seconds"
                        )
                last_connection_state = state
            if health_task is None and loop_time >= next_connection_check:
                health_task = asyncio.create_task(
                    checked_snapshot(),
                    name="oopz_connection_health",
                )

            if disconnect_error is not None:
                _append_connectivity_event(
                    session_dir, "connection_lost",
                    connection_episode=current_connection_episode,
                    error_type=type(disconnect_error).__name__, error=str(disconnect_error)[:500],
                )
                emit_event(
                    "continuous.connection_lost", request.request_id,
                    session_id=session_id,
                    connection_episode=current_connection_episode,
                    error=str(disconnect_error)[:500],
                )
                LOGGER.warning(
                    "voice connection lost; keeping Session ID=%s and reconnecting: %s",
                    session_id, disconnect_error,
                )
                if health_task is not None:
                    health_task.cancel()
                    await asyncio.gather(health_task, return_exceptions=True)
                    health_task = None
                await stop_voice_episode()
                current_membership.clear()
                empty_channel_since = None
                empty_channel_since_wall = None
                lifecycle.pop("empty_channel_since", None)
                outage_started = loop.time()
                next_connect_at = outage_started + request.reconnect_initial_delay_seconds
                reconnect_attempt_in_outage = 0
                rebuild_backend = True
                unhealthy_since = None
                lifecycle.update({
                    "status": "reconnecting",
                    "last_disconnected_at": _iso(utc_now()),
                    "last_connection_error": {
                        "type": type(disconnect_error).__name__,
                        "message": str(disconnect_error)[:500],
                    },
                    "reconnect_deadline_at": _iso(
                        utc_now() + timedelta(seconds=request.reconnect_window_seconds)
                    ),
                })
                write_json(session_dir / "lifecycle.json", lifecycle)
                continue
            await asyncio.sleep(request.poll_interval_seconds)
    except BaseException as error:
        failure = error
        stop_reason = "capture_failure"
    finally:
        for task in (membership_task, health_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (membership_task, health_task) if task is not None),
            return_exceptions=True,
        )
        lifecycle.update({"status": "stopping", "stop_reason": stop_reason})
        write_json(session_dir / "lifecycle.json", lifecycle)
        try:
            if capture_started or joined:
                await stop_voice_episode()
            else:
                capture_stopped_at = utc_now()
        except Exception as error:
            if failure is None:
                failure = error
        try:
            await bot.stop()
        except Exception:
            LOGGER.debug("bot.stop failed during final cleanup", exc_info=True)
        await queue.put(None)
        await queue.join()
        await consumer_task

    if debug_events:
        write_jsonl(session_dir / "debug" / "agora_events.jsonl", debug_events)
    if final_snapshot is not None:
        mappings = build_identity_mappings(
            list(participants_by_uid.values()), final_snapshot,
            self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
            self_agora_uid=self_agora_uid,
        )
        write_json(session_dir / "users.json", [item.to_dict() for item in mappings])
    segment_count, markdown = _merge_transcripts(session_dir, results)
    print(
        f"[转写进度] 全部合并完成：成功分片={sum(1 for item in results if item['ok'])}/{len(results)}；"
        f"失败分片={sum(1 for item in results if not item['ok'])}；总段落={segment_count}。",
        flush=True,
    )
    handoff = _write_final_handoff(
        session_dir, request, stopped_at=capture_stopped_at, delete_after=delete_after,
        segment_count=segment_count, chunk_results=results,
    )
    failed_chunks = [item for item in results if not item["ok"]]
    lifecycle.update({
        "status": "ready_for_analysis" if not failed_chunks and failure is None else "ready_for_analysis_with_errors",
        "stopped_at": _iso(capture_stopped_at),
        "stop_reason": stop_reason,
        "chunks_total": len(results),
        "chunks_transcribed": len(results) - len(failed_chunks),
        "chunks_failed": len(failed_chunks),
        "transcript_segments": segment_count,
        "audio_deleted": not request.retain_audio and not failed_chunks,
        "audio_retained_for_testing": request.retain_audio,
        "analyzer_handoff": str(handoff.relative_to(session_dir)).replace("\\", "/"),
        "connection_episode": current_connection_episode,
        "successful_connections": successful_connections,
        "reconnect_count": max(0, successful_connections - 1),
        "connection_attempts": connection_attempts,
        "reconnect_attempts": reconnect_attempts,
        "total_disconnected_seconds": round(total_disconnected_seconds, 3),
        "last_connection_state": last_connection_state,
        "membership_refresh_successes": membership_refresh_successes,
        "membership_refresh_failures": membership_refresh_failures,
        "empty_channel_timeout_seconds": request.empty_channel_timeout_seconds,
        "last_other_member_count": count_other_members(
            current_membership,
            self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
        ),
        "connectivity_log": "debug/connectivity_events.jsonl",
        "failure": {"type": type(failure).__name__, "message": str(failure)} if failure else None,
    })
    write_json(session_dir / "lifecycle.json", lifecycle)
    emit_event(
        "continuous.ready_for_analysis", request.request_id,
        session_id=session_id, stop_reason=stop_reason, chunks_total=len(results),
        chunks_failed=len(failed_chunks), transcript_segments=segment_count,
        transcript_markdown=str(markdown), analyzer_request=str(handoff),
        audio_deleted=not request.retain_audio and not failed_chunks,
        audio_retained_for_testing=request.retain_audio,
    )
    return session_dir
