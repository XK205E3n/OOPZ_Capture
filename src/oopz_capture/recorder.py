from __future__ import annotations

import base64
import math
import struct
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TrackStats:
    agora_uid: str
    sample_rate: int
    chunks: int = 0
    captured_frames: int = 0
    silence_frames: int = 0
    peak: int = 0
    square_sum: float = 0.0
    generations: set[int] = field(default_factory=set)

    def to_dict(self, path: Path) -> dict[str, Any]:
        total_frames = self.captured_frames + self.silence_frames
        rms = math.sqrt(self.square_sum / self.captured_frames) if self.captured_frames else 0
        value = asdict(self)
        value["generations"] = sorted(self.generations)
        value.update({
            "path": str(path),
            "duration_seconds": total_frames / self.sample_rate,
            "captured_seconds": self.captured_frames / self.sample_rate,
            "silence_inserted_seconds": self.silence_frames / self.sample_rate,
            "peak_dbfs": _dbfs(self.peak),
            "rms_dbfs": _dbfs(rms),
        })
        return value


def _dbfs(amplitude: float) -> float | None:
    if amplitude <= 0:
        return None
    return 20 * math.log10(float(amplitude) / 32768.0)


class WavTrackWriter:
    """Append one Agora user's mono PCM16 chunks to an aligned WAV file."""

    def __init__(self, audio_dir: Path, agora_uid: str, sample_rate: int):
        if not agora_uid.isdigit():
            raise ValueError(f"invalid numeric Agora UID: {agora_uid!r}")
        if sample_rate <= 0:
            raise ValueError("sample rate must be positive")
        audio_dir.mkdir(parents=True, exist_ok=True)
        self.final_path = audio_dir / f"{agora_uid}.wav"
        self.part_path = audio_dir / f"{agora_uid}.part.wav"
        self._stream = wave.open(str(self.part_path), "wb")
        self._stream.setnchannels(1)
        self._stream.setsampwidth(2)
        self._stream.setframerate(sample_rate)
        self.stats = TrackStats(agora_uid=agora_uid, sample_rate=sample_rate)
        self._written_frames = 0
        self._last_generation: int | None = None
        self._closed = False

    def append(self, pcm16: bytes, *, session_offset_ms: float, frame_count: int, generation: int) -> None:
        if self._closed:
            raise RuntimeError("cannot append to a closed WAV writer")
        if len(pcm16) != frame_count * 2:
            raise ValueError("PCM byte length does not match frame count")

        # Wall-clock alignment is applied only when a publication generation
        # starts. Continuous frame delivery is appended without timer-jitter
        # gaps; a new generation after mute/leave is aligned with real silence.
        if self._last_generation != generation:
            chunk_start_ms = max(0.0, float(session_offset_ms) - frame_count * 1000.0 / self.stats.sample_rate)
            desired_start = round(chunk_start_ms * self.stats.sample_rate / 1000.0)
            missing = desired_start - self._written_frames
            if missing > 0:
                self._stream.writeframesraw(b"\x00\x00" * missing)
                self._written_frames += missing
                self.stats.silence_frames += missing
            self._last_generation = generation

        self._stream.writeframesraw(pcm16)
        self._written_frames += frame_count
        self.stats.chunks += 1
        self.stats.captured_frames += frame_count
        self.stats.generations.add(generation)
        if frame_count:
            samples = struct.unpack(f"<{frame_count}h", pcm16)
            self.stats.peak = max(self.stats.peak, max(abs(item) for item in samples))
            self.stats.square_sum += sum(float(item) * item for item in samples)

    def close(self) -> dict[str, Any]:
        if not self._closed:
            self._stream.close()
            self.part_path.replace(self.final_path)
            self._closed = True
        return self.stats.to_dict(self.final_path)


class CaptureRecorder:
    def __init__(self, session_dir: Path):
        self.audio_dir = session_dir / "audio"
        self._writers: dict[str, WavTrackWriter] = {}

    def ingest(self, chunk: dict[str, Any]) -> None:
        uid = str(chunk.get("uid") or "")
        sample_rate = int(chunk.get("sampleRate") or 0)
        frame_count = int(chunk.get("frameCount") or 0)
        writer = self._writers.get(uid)
        if writer is None:
            writer = WavTrackWriter(self.audio_dir, uid, sample_rate)
            self._writers[uid] = writer
        elif writer.stats.sample_rate != sample_rate:
            raise ValueError(f"sample rate changed for Agora UID {uid}")
        writer.append(
            base64.b64decode(str(chunk.get("pcm16Base64") or ""), validate=True),
            session_offset_ms=float(chunk.get("sessionOffsetMs") or 0),
            frame_count=frame_count,
            generation=int(chunk.get("generation") or 0),
        )

    def close(self) -> list[dict[str, Any]]:
        return [self._writers[uid].close() for uid in sorted(self._writers, key=int)]
