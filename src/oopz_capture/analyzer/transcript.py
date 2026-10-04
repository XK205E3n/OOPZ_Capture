"""Load a recorded session and turn its ASR segments into evidence runs.

Nothing is dropped: every non-empty segment ends up in exactly one run.  A run is a
stretch of one speaker's consecutive segments, so a model sees sentences instead of
two-word fragments.  Run ids (r0001, ...) follow time order and are the evidence ids.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

RUN_GAP_MS = 1500
RUN_MAX_MS = 30_000
RUN_MAX_CHARS = 240
LOCAL_TZ = timezone(timedelta(hours=8))  # project convention: Beijing time, no zone wording on the card

_TAG = re.compile(r"<\|[^|>]*\|>")
_NOISE = re.compile(r"^[\W_]*$")


@dataclass(frozen=True)
class Run:
    id: str
    speaker_id: str
    start_ms: int
    end_ms: int
    text: str

    def as_evidence(self) -> dict:
        return {"id": self.id, "kind": "asr_excerpt", "speaker_id": self.speaker_id,
                "start_ms": self.start_ms, "end_ms": self.end_ms, "text": self.text}


@dataclass(frozen=True)
class Session:
    session_id: str
    started_at: datetime        # capture clock start, local time
    stopped_at: datetime | None
    duration_ms: int            # full capture duration (denominator for statistics)
    runs: list[Run]
    roster: list[dict]          # [{"speaker_id", "nickname"}] for people who spoke
    segments: list[dict]        # raw ASR segments (statistics count these)


def speaker_key(segment: dict) -> str:
    """The OOPZ uid, or agora-<uid> for an audio track the recorder could not map to a member."""
    return str(segment.get("oopz_uid") or "") or f"agora-{segment['agora_uid']}"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub("", text)).strip()


def merge_runs(segments: list[dict]) -> list[Run]:
    """Merge each speaker's near-adjacent segments (gap <= RUN_GAP_MS, span <= RUN_MAX_MS, <= RUN_MAX_CHARS)."""
    pending: dict[str, list] = {}   # speaker -> [start_ms, end_ms, text]
    finished: list[tuple[int, str, int, str]] = []

    def flush(speaker: str) -> None:
        start, end, text = pending.pop(speaker)
        finished.append((start, speaker, end, text))

    for segment in sorted(segments, key=lambda s: (s["start_ms"], s["end_ms"])):
        text = clean(segment.get("text", ""))
        if not text or _NOISE.match(text):
            continue
        speaker = speaker_key(segment)
        current = pending.get(speaker)
        if current and (segment["start_ms"] - current[1] > RUN_GAP_MS
                        or segment["end_ms"] - current[0] > RUN_MAX_MS
                        or len(current[2]) + 1 + len(text) > RUN_MAX_CHARS):
            flush(speaker)
            current = None
        if current:
            current[1] = max(current[1], segment["end_ms"])
            current[2] += " " + text if _needs_space(current[2], text) else text
        else:
            pending[speaker] = [segment["start_ms"], segment["end_ms"], text]
    for speaker in list(pending):
        flush(speaker)
    finished.sort()
    return [Run(f"r{index:04d}", speaker, start, end, text)
            for index, (start, speaker, end, text) in enumerate(finished, 1)]


def _needs_space(left: str, right: str) -> bool:
    return left[-1:].isascii() and left[-1:].isalnum() and right[:1].isascii() and right[:1].isalnum()


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(LOCAL_TZ)


def load_session(session_dir: Path) -> Session:
    session_dir = Path(session_dir)
    meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    lifecycle = json.loads((session_dir / "lifecycle.json").read_text(encoding="utf-8"))
    with (session_dir / "transcript.jsonl").open(encoding="utf-8") as stream:
        segments = [json.loads(line) for line in stream if line.strip()]
    runs = merge_runs(segments)
    started = _local(meta.get("capture_clock_started_at") or meta["started_at"])
    stopped = _local(lifecycle["stopped_at"]) if lifecycle.get("stopped_at") else None
    last_end = max((s["end_ms"] for s in segments), default=0)
    duration = int((stopped - started).total_seconds() * 1000) if stopped else last_end
    users = {str(u["oopz_uid"]): u for u in json.loads((session_dir / "users.json").read_text(encoding="utf-8"))}
    seen_names = {speaker_key(s): s.get("speaker", "") for s in segments}
    unknown = sorted(k for k in {run.speaker_id for run in runs} if k.startswith("agora-"))
    roster = []
    for speaker in sorted({run.speaker_id for run in runs}):
        user = users.get(speaker, {})
        if user.get("is_bot"):
            continue
        if speaker in unknown:
            nickname = "未识别成员" + (chr(65 + unknown.index(speaker)) if len(unknown) > 1 else "")
        else:
            nickname = clean(user.get("nickname") or seen_names.get(speaker) or speaker)
        roster.append({"speaker_id": speaker, "nickname": nickname})
    known = {p["speaker_id"] for p in roster}
    return Session(meta["session_id"], started, stopped, max(duration, last_end),
                   [run for run in runs if run.speaker_id in known], roster, segments)


def clock(session: Session, offset_ms: int) -> str:
    return (session.started_at + timedelta(milliseconds=offset_ms)).strftime("%H:%M")
