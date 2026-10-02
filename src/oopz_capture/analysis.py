from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

from .output import write_json
from .readable import identity_label


def _window_dbfs(pcm: bytes) -> float | None:
    count = len(pcm) // 2
    if not count:
        return None
    samples = struct.unpack(f"<{count}h", pcm)
    rms = math.sqrt(sum(float(item) * item for item in samples) / count)
    return 20 * math.log10(rms / 32768.0) if rms else None


def _track_windows(path: Path, window_ms: int) -> tuple[int, list[float | None]]:
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError(f"expected mono PCM16 WAV: {path}")
        rate = stream.getframerate()
        frames_per_window = max(1, round(rate * window_ms / 1000))
        values: list[float | None] = []
        while pcm := stream.readframes(frames_per_window):
            values.append(_window_dbfs(pcm))
    return rate, values


def analyze_session(session_dir: Path, *, threshold_dbfs: float = -45.0, window_ms: int = 100) -> dict[str, Any]:
    if window_ms <= 0:
        raise ValueError("window_ms must be positive")
    audio_dir = session_dir / "audio"
    files = sorted(
        (item for item in audio_dir.glob("*.wav") if item.stem.isdigit()),
        key=lambda item: int(item.stem),
    )
    if not files:
        raise ValueError(f"no WAV tracks found in {audio_dir}")
    users_path = session_dir / "users.json"
    users = json.loads(users_path.read_text(encoding="utf-8")) if users_path.exists() else []
    users_by_agora = {str(item.get("agora_uid")): item for item in users}
    tracks: dict[str, list[float | None]] = {}
    track_results: list[dict[str, Any]] = []
    for path in files:
        rate, windows = _track_windows(path, window_ms)
        uid = path.stem
        tracks[uid] = windows
        active = [index for index, value in enumerate(windows) if value is not None and value >= threshold_dbfs]
        track_results.append({
            "agora_uid": uid,
            "path": str(path),
            "sample_rate": rate,
            "active_windows": len(active),
            "active_seconds": len(active) * window_ms / 1000.0,
        })

    overlaps: list[dict[str, Any]] = []
    total_windows = max(len(values) for values in tracks.values())
    open_overlap: dict[str, Any] | None = None
    for index in range(total_windows + 1):
        active_uids = sorted((uid for uid, values in tracks.items() if index < len(values) and values[index] is not None and values[index] >= threshold_dbfs), key=int)
        if len(active_uids) >= 2:
            if open_overlap and open_overlap["agora_uids"] == active_uids:
                open_overlap["end_ms"] = (index + 1) * window_ms
            else:
                if open_overlap:
                    overlaps.append(open_overlap)
                open_overlap = {"start_ms": index * window_ms, "end_ms": (index + 1) * window_ms, "agora_uids": active_uids}
        elif open_overlap:
            overlaps.append(open_overlap)
            open_overlap = None

    result = {
        "method": "fixed-window RMS energy (not speech recognition or speaker ID)",
        "threshold_dbfs": threshold_dbfs,
        "window_ms": window_ms,
        "tracks": track_results,
        "overlaps": overlaps,
    }
    write_json(session_dir / "analysis" / "overlap.json", result)
    lines = [
        "# Milestone 6 energy analysis", "",
        "This report detects simultaneous signal energy. It does not prove speech, identity, or transcription accuracy.", "",
        f"Threshold: {threshold_dbfs:.1f} dBFS; window: {window_ms} ms.", "", "## Tracks", "",
    ]
    for item in track_results:
        uid = item["agora_uid"]
        user = users_by_agora.get(uid, {})
        lines.append(f"- {identity_label(nickname=str(user.get('nickname') or ''), oopz_uid=str(user.get('oopz_uid') or ''), agora_uid=int(uid))}; active={item['active_seconds']:.1f}s; file=audio/{uid}.wav")
    lines.extend(["", "## Simultaneous-energy intervals", ""])
    if overlaps:
        for item in overlaps:
            labels = []
            for uid in item["agora_uids"]:
                user = users_by_agora.get(uid, {})
                labels.append(identity_label(nickname=str(user.get("nickname") or ""), oopz_uid=str(user.get("oopz_uid") or ""), agora_uid=int(uid)))
            lines.append(f"- {item['start_ms'] / 1000:.1f}s to {item['end_ms'] / 1000:.1f}s: " + " || ".join(labels))
    else:
        lines.append("- No overlap detected at the selected threshold.")
    lines.extend(["", "## Acceptance note", "", "Manually listen to each UID-labelled WAV. Milestone 6 passes only if every file contains only its mapped participant, including intentional overlap intervals.", ""])
    report_path = session_dir / "analysis" / "overlap.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return result
