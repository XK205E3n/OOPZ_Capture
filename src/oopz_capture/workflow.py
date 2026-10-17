from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .jsonio import iso_utc as _iso
from .vad import VADConfig


REQUEST_SCHEMA = "oopz.worker.request.v1"
EVENT_SCHEMA = "oopz.worker.event.v1"
_REPARSE_POINT = 0x0400


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WorkflowRequest:
    request_id: str
    area_id: str
    channel_id: str
    duration_seconds: float
    language: str = "auto"
    processing_deadline_seconds: int = 900
    retention_hours: int = 720
    poll_interval_seconds: float = 0.25
    retain_audio: bool = False
    rtc_uid: str | None = None
    requested_by: dict[str, Any] | None = None
    schema_version: str = REQUEST_SCHEMA
    command: str = "record_and_transcribe"

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
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise TimeoutError("transcription exceeded the remaining analysis deadline") from error
    output = stdout.decode("utf-8", errors="replace")
    errors = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise RuntimeError(f"transcription failed with exit code {process.returncode}: {errors.strip()}")
    return output, errors
