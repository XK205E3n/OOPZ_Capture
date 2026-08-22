from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, uuid5

from .output import write_json
from .readable import identity_label


SHORT_WINDOW_MS = 300_000
LONG_WINDOW_MS = 3_600_000
WINDOW_PLANNER_VERSION = "2.0.0"


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def determine_session_duration_ms(
    session_dir: Path,
    session: dict[str, Any],
    transcript: list[dict[str, Any]],
) -> int:
    candidates = [max((int(item["end_ms"]) for item in transcript), default=0)]
    duration = session.get("duration_seconds")
    if duration is not None:
        candidates.append(max(0, round(float(duration) * 1000)))
    lifecycle: dict[str, Any] = {}
    lifecycle_path = session_dir / "lifecycle.json"
    if lifecycle_path.is_file():
        lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        stopped = lifecycle.get("stopped_at") or lifecycle.get("capture_finished_at")
        started = session.get("capture_clock_started_at") or lifecycle.get("capture_started_at") or session.get("started_at")
        if stopped and started:
            candidates.append(max(0, round((_parse_time(str(stopped)) - _parse_time(str(started))).total_seconds() * 1000)))
    actual = max(candidates)
    request_path = session_dir / "request.json"
    if lifecycle.get("stop_reason") == "diagnostic_max_runtime" and request_path.is_file():
        request = json.loads(request_path.read_text(encoding="utf-8"))
        configured = request.get("max_runtime_seconds") if isinstance(request, dict) else None
        if configured is not None:
            configured_ms = round(float(configured) * 1000)
            if configured_ms > 0 and configured_ms <= actual <= configured_ms + 2000:
                return configured_ms
    return actual


def _window_id(session_id: str, kind: str, start_ms: int, end_ms: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"{session_id}:{kind}:{start_ms}:{end_ms}"))


def _speaker(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "nickname": str(item.get("speaker") or item.get("nickname") or ""),
        "oopz_uid": str(item.get("oopz_uid") or ""),
        "agora_uid": int(item["agora_uid"]),
    }


def _speakers(segments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[int, str], dict[str, Any]] = {}
    for item in segments:
        value = _speaker(item)
        unique[(value["agora_uid"], value["oopz_uid"])] = value
    return [unique[key] for key in sorted(unique)]


def _segment_reference(item: dict[str, Any], start_ms: int, end_ms: int) -> dict[str, Any]:
    segment_start = int(item["start_ms"])
    segment_end = int(item["end_ms"])
    return {
        "segment_id": str(item["segment_id"]),
        "start_ms": segment_start,
        "end_ms": segment_end,
        "visible_start_ms": max(segment_start, start_ms),
        "visible_end_ms": min(segment_end, end_ms),
        "crosses_window_boundary": segment_start < start_ms or segment_end > end_ms,
        **_speaker(item),
        "text": str(item.get("text") or "").strip(),
        "overlap": bool(item.get("overlap")),
    }


def plan_windows(
    session_id: str,
    transcript: list[dict[str, Any]],
    duration_ms: int,
    *,
    short_window_ms: int = SHORT_WINDOW_MS,
    long_window_ms: int = LONG_WINDOW_MS,
) -> dict[str, Any]:
    if duration_ms < 0:
        raise ValueError("duration_ms must be non-negative")
    if short_window_ms != SHORT_WINDOW_MS:
        raise ValueError(f"short_window_ms must be {SHORT_WINDOW_MS}")
    if long_window_ms != LONG_WINDOW_MS:
        raise ValueError(f"long_window_ms must be {LONG_WINDOW_MS}")
    if long_window_ms % short_window_ms:
        raise ValueError("long window must contain an integer number of short windows")
    for previous, current in zip(transcript, transcript[1:]):
        if (int(current["start_ms"]), int(current["agora_uid"])) < (int(previous["start_ms"]), int(previous["agora_uid"])):
            raise ValueError("transcript must be time ordered")

    short_windows: list[dict[str, Any]] = []
    short_count = math.ceil(duration_ms / short_window_ms) if duration_ms else 0
    for index in range(short_count):
        start_ms = index * short_window_ms
        end_ms = min(duration_ms, start_ms + short_window_ms)
        members = [item for item in transcript if int(item["start_ms"]) < end_ms and start_ms < int(item["end_ms"])]
        short_windows.append({
            "window_id": _window_id(session_id, "short", start_ms, end_ms),
            "kind": "short",
            "index": index + 1,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": end_ms - start_ms,
            "partial": end_ms - start_ms < short_window_ms,
            "silent": not members,
            "segment_count": len(members),
            "source_segment_ids": [str(item["segment_id"]) for item in members],
            "speakers": _speakers(members),
            "segments": [_segment_reference(item, start_ms, end_ms) for item in members],
        })

    long_windows: list[dict[str, Any]] = []
    long_count = math.ceil(duration_ms / long_window_ms) if duration_ms else 0
    for index in range(long_count):
        start_ms = index * long_window_ms
        end_ms = min(duration_ms, start_ms + long_window_ms)
        children = [item for item in short_windows if item["start_ms"] < end_ms and start_ms < item["end_ms"]]
        source_ids: list[str] = []
        speaker_values: list[dict[str, Any]] = []
        for child in children:
            for segment_id in child["source_segment_ids"]:
                if segment_id not in source_ids:
                    source_ids.append(segment_id)
            speaker_values.extend(child["segments"])
        long_windows.append({
            "window_id": _window_id(session_id, "long", start_ms, end_ms),
            "kind": "long",
            "index": index + 1,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": end_ms - start_ms,
            "partial": end_ms - start_ms < long_window_ms,
            "silent": all(item["silent"] for item in children),
            "short_window_ids": [item["window_id"] for item in children],
            "short_window_count": len(children),
            "source_segment_ids": source_ids,
            "speakers": _speakers(speaker_values),
        })

    return {
        "schema_version": "oopz.analysis.windows.v1",
        "session_id": session_id,
        "duration_ms": duration_ms,
        "short_window_ms": short_window_ms,
        "planner_version": WINDOW_PLANNER_VERSION,
        "long_window_ms": long_window_ms,
        "short_windows_per_full_long_window": long_window_ms // short_window_ms,
        "short_window_count": len(short_windows),
        "long_window_count": len(long_windows),
        "short_windows": short_windows,
        "long_windows": long_windows,
    }


def render_windows_markdown(path: Path, plan: dict[str, Any]) -> Path:
    lines = [
        "# OOPZ Analysis Window Plan", "",
        f"Session ID: {plan['session_id']}",
        f"Duration: {int(plan['duration_ms']) / 1000:.3f} seconds",
        f"Short windows: {plan['short_window_count']} × up to 300 seconds",
        f"Long windows: {plan['long_window_count']} × up to 60 minutes", "",
        "## Short windows", "",
    ]
    for window in plan["short_windows"]:
        state = "silent" if window["silent"] else f"{window['segment_count']} segments"
        lines.append(f"- Window ID={window['window_id']} | {window['start_ms']}–{window['end_ms']} ms | {state}")
        for speaker in window["speakers"]:
            lines.append(f"  - {identity_label(nickname=speaker['nickname'], oopz_uid=speaker['oopz_uid'], agora_uid=speaker['agora_uid'])}")
    lines.extend(["", "## Long windows", ""])
    for window in plan["long_windows"]:
        lines.append(f"- Window ID={window['window_id']} | {window['start_ms']}–{window['end_ms']} ms | short windows={window['short_window_count']}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_window_plan(session_dir: Path, plan: dict[str, Any]) -> tuple[Path, Path]:
    json_path = session_dir / "analysis" / "windows.json"
    markdown_path = session_dir / "analysis" / "windows.md"
    write_json(json_path, plan)
    render_windows_markdown(markdown_path, plan)
    return json_path, markdown_path
