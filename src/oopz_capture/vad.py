from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from .audio_io import read_mono_pcm16, resample_audio
from .output import write_json, write_jsonl
from .readable import readable_nickname


VAD_SAMPLE_RATE = 16000


@dataclass(slots=True)
class VADConfig:
    threshold: float = 0.5
    min_speech_ms: int = 250
    min_silence_ms: int = 300
    speech_pad_ms: int = 200

    def validate(self) -> None:
        if not 0 < self.threshold < 1:
            raise ValueError("VAD threshold must be between 0 and 1")
        if min(self.min_speech_ms, self.min_silence_ms, self.speech_pad_ms) < 0:
            raise ValueError("VAD duration settings must be non-negative")


class VADBackend(Protocol):
    name: str
    def detect(self, samples, sample_rate: int, config: VADConfig) -> list[dict[str, Any]]: ...


class SileroVADBackend:
    name = "silero-vad-onnx"

    def __init__(self):
        try:
            from silero_vad import load_silero_vad
        except ModuleNotFoundError as error:
            raise RuntimeError("Silero VAD is not installed. Run: pip install -e \".[speech]\"") from error
        self._model = load_silero_vad(onnx=True)

    def detect(self, samples, sample_rate: int, config: VADConfig) -> list[dict[str, Any]]:
        if sample_rate != VAD_SAMPLE_RATE:
            raise ValueError(f"Silero input must be {VAD_SAMPLE_RATE} Hz")
        import torch
        from silero_vad import get_speech_timestamps
        return list(get_speech_timestamps(
            torch.from_numpy(samples), self._model, sampling_rate=sample_rate,
            threshold=config.threshold, min_speech_duration_ms=config.min_speech_ms,
            min_silence_duration_ms=config.min_silence_ms,
            speech_pad_ms=config.speech_pad_ms, return_seconds=False,
        ))


def _load_users(session_dir: Path) -> dict[str, dict[str, Any]]:
    path = session_dir / "users.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return {str(item.get("agora_uid")): item for item in values if item.get("agora_uid") is not None}


def _mark_overlaps(segments: list[dict[str, Any]]) -> None:
    for segment in segments:
        segment["overlap"] = any(
            other is not segment and other["agora_uid"] != segment["agora_uid"]
            and other["start_ms"] < segment["end_ms"]
            and segment["start_ms"] < other["end_ms"]
            for other in segments
        )


def run_vad_session(session_dir: Path, backend: VADBackend, config: VADConfig) -> list[dict[str, Any]]:
    config.validate()
    session = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    session_id = str(session.get("session_id") or session_dir.name)
    users = _load_users(session_dir)
    audio_files = sorted(
        (item for item in (session_dir / "audio").glob("*.wav") if item.stem.isdigit()),
        key=lambda item: int(item.stem),
    )
    if not audio_files:
        raise ValueError(f"no WAV tracks found in {session_dir / 'audio'}")
    segments: list[dict[str, Any]] = []
    track_summaries = []
    for path in audio_files:
        agora_uid = path.stem
        source_rate, source = read_mono_pcm16(path)
        samples = resample_audio(source, source_rate, VAD_SAMPLE_RATE)
        detections = backend.detect(samples, VAD_SAMPLE_RATE, config)
        user = users.get(agora_uid, {})
        accepted = 0
        for detection in detections:
            start_sample = max(0, int(detection["start"]))
            end_sample = min(len(samples), int(detection["end"]))
            if end_sample <= start_sample:
                continue
            start_ms = round(start_sample * 1000 / VAD_SAMPLE_RATE)
            end_ms = round(end_sample * 1000 / VAD_SAMPLE_RATE)
            key = f"{session_id}:{agora_uid}:{start_ms}:{end_ms}"
            segments.append({
                "segment_id": str(uuid5(NAMESPACE_URL, key)), "session_id": session_id,
                "start_ms": start_ms, "end_ms": end_ms, "agora_uid": int(agora_uid),
                "oopz_uid": str(user.get("oopz_uid") or ""),
                "speaker": readable_nickname(str(user.get("nickname") or "")),
                "audio_file": str(path.relative_to(session_dir)).replace("\\", "/"),
                "audio_start_sample": round(start_ms * source_rate / 1000),
                "audio_end_sample": round(end_ms * source_rate / 1000),
                "sample_rate": source_rate, "vad_backend": backend.name,
                "vad_threshold": config.threshold, "overlap": False,
            })
            accepted += 1
        track_summaries.append({"agora_uid": int(agora_uid), "segments": accepted})
    segments.sort(key=lambda item: (item["start_ms"], item["agora_uid"], item["end_ms"]))
    _mark_overlaps(segments)
    write_jsonl(session_dir / "vad" / "segments.jsonl", segments)
    write_json(session_dir / "vad" / "summary.json", {
        "session_id": session_id, "backend": backend.name, "config": asdict(config),
        "segment_count": len(segments), "tracks": track_summaries,
    })
    return segments
