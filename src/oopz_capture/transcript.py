from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .asr import ASRBackend
from .audio_io import read_mono_pcm16, resample_audio
from .output import write_json, write_jsonl
from .readable import identity_label, readable_nickname


# SenseVoice uses ``yue`` for Cantonese; it is retained as Chinese text rather
# than treated as a foreign-language result.
AUTO_TRANSCRIPT_LANGUAGES = frozenset({"zh", "yue", "en"})


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                values.append(json.loads(line))
    return values


def _capture_origin(session_dir: Path, session: dict[str, Any]) -> datetime:
    configured = session.get("capture_clock_started_at")
    if configured:
        return datetime.fromisoformat(str(configured).replace("Z", "+00:00"))
    events_path = session_dir / "debug" / "agora_events.jsonl"
    if events_path.exists():
        for event in _read_jsonl(events_path):
            if event.get("type") == "capture_started" and event.get("at"):
                return datetime.fromisoformat(str(event["at"]).replace("Z", "+00:00"))
    return datetime.fromisoformat(str(session["started_at"]).replace("Z", "+00:00"))


def _iso_at(origin: datetime, offset_ms: int) -> str:
    return (origin + timedelta(milliseconds=offset_ms)).isoformat(timespec="milliseconds")


def _output_stem(value: str) -> str:
    if not re.fullmatch(r"transcript(?:\.[a-z0-9_-]+)?", value):
        raise ValueError("transcript output stem must be transcript or transcript.<name>")
    return value


def transcribe_session(
    session_dir: Path,
    backend: ASRBackend,
    *,
    language: str | None = None,
    output_stem: str = "transcript",
    allowed_languages: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    output_stem = _output_stem(output_stem)
    vad_path = session_dir / "vad" / "segments.jsonl"
    if not vad_path.exists():
        raise ValueError("VAD segments are missing. Run the vad command first.")
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    origin = _capture_origin(session_dir, session)
    segments = _read_jsonl(vad_path)
    allowed = (
        {str(item).strip().casefold() for item in allowed_languages if str(item).strip()}
        if allowed_languages is not None else None
    )
    audio_cache: dict[str, tuple[int, Any]] = {}
    transcript = []
    for index, segment in enumerate(segments, start=1):
        relative_audio = str(segment["audio_file"])
        if relative_audio not in audio_cache:
            audio_cache[relative_audio] = read_mono_pcm16(session_dir / relative_audio)
        source_rate, full_audio = audio_cache[relative_audio]
        start = int(segment["audio_start_sample"])
        end = int(segment["audio_end_sample"])
        samples = resample_audio(full_audio[start:end], source_rate, 16000)
        result = backend.transcribe(samples, 16000, language=language)
        detected_language = str(result.language or "").strip().casefold()
        if allowed is not None and detected_language not in allowed:
            continue
        text = result.text.strip()
        if not text:
            continue
        value = {
            "segment_id": segment["segment_id"], "session_id": segment["session_id"],
            "start_ms": int(segment["start_ms"]), "end_ms": int(segment["end_ms"]),
            "start_time": _iso_at(origin, int(segment["start_ms"])),
            "end_time": _iso_at(origin, int(segment["end_ms"])),
            "agora_uid": int(segment["agora_uid"]), "oopz_uid": str(segment["oopz_uid"]),
            "speaker": readable_nickname(str(segment["speaker"])), "text": text,
            "language": result.language, "asr_backend": backend.name,
            "overlap": bool(segment.get("overlap")), "audio_file": relative_audio,
            "audio_start_sample": start, "audio_end_sample": end,
        }
        if result.confidence is not None:
            value["confidence"] = result.confidence
        transcript.append(value)
        print(f"ASR {index}/{len(segments)} | Agora UID={value['agora_uid']} | {value['start_ms']}-{value['end_ms']} ms")
    transcript.sort(key=lambda item: (item["start_ms"], item["agora_uid"], item["end_ms"]))
    write_jsonl(session_dir / f"{output_stem}.jsonl", transcript)
    write_json(session_dir / f"{output_stem}_summary.json", {
        "session_id": session.get("session_id"), "capture_clock_started_at": origin.isoformat(),
        "asr_backend": backend.name, "segments": len(transcript),
        "language_request": language or "auto",
        "allowed_languages": sorted(allowed) if allowed is not None else None,
    })
    return transcript


def _clock(milliseconds: int) -> str:
    total_seconds, ms = divmod(milliseconds, 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{ms:03d}"


def render_transcript_markdown(
    session_dir: Path,
    segments: Iterable[dict[str, Any]] | None = None,
    *,
    output_stem: str = "transcript",
) -> Path:
    output_stem = _output_stem(output_stem)
    values = list(segments) if segments is not None else _read_jsonl(session_dir / f"{output_stem}.jsonl")
    values.sort(key=lambda item: (int(item["start_ms"]), int(item["agora_uid"]), int(item["end_ms"])))
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    lines = [
        "# OOPZ Voice Transcript", "", f"Session ID: {session.get('session_id', session_dir.name)}",
        f"Started: {session.get('started_at', 'unknown')}", f"Segments: {len(values)}", "",
    ]
    for item in values:
        label = identity_label(nickname=str(item.get("speaker") or ""), oopz_uid=str(item.get("oopz_uid") or ""), agora_uid=int(item["agora_uid"]))
        overlap = " | simultaneous speech" if item.get("overlap") else ""
        lines.extend([
            f"[{_clock(int(item['start_ms']))} -> {_clock(int(item['end_ms']))}] {label}{overlap}",
            str(item.get("text") or "").strip(), "",
        ])
    path = session_dir / f"{output_stem}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
