from __future__ import annotations

from uuid import uuid4
from pathlib import Path

from oopz_capture.analysis_windows import LONG_WINDOW_MS, SHORT_WINDOW_MS, determine_session_duration_ms, plan_windows
from oopz_capture.output import write_json


def segment(start_ms: int, end_ms: int, *, uid: int = 123, text: str = "内容") -> dict:
    return {
        "segment_id": str(uuid4()),
        "session_id": "unused",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "agora_uid": uid,
        "oopz_uid": f"oopz-{uid}",
        "speaker": f"用户{uid}",
        "text": text,
        "overlap": False,
    }


def test_windows_are_fixed_from_session_zero_and_cross_chunk_boundaries() -> None:
    session_id = str(uuid4())
    crossing = segment(299_000, 301_000)
    plan = plan_windows(session_id, [crossing], 600_000)
    assert plan["short_window_ms"] == SHORT_WINDOW_MS
    assert plan["short_window_count"] == 2
    assert [(item["start_ms"], item["end_ms"]) for item in plan["short_windows"]] == [
        (0, 300_000), (300_000, 600_000),
    ]
    assert plan["short_windows"][0]["source_segment_ids"] == [crossing["segment_id"]]
    assert plan["short_windows"][1]["source_segment_ids"] == [crossing["segment_id"]]
    assert plan["short_windows"][0]["segments"][0]["visible_end_ms"] == 300_000
    assert plan["short_windows"][1]["segments"][0]["visible_start_ms"] == 300_000


def test_silent_and_partial_tail_windows_are_explicit() -> None:
    plan = plan_windows(str(uuid4()), [segment(1_000, 2_000)], 620_000)
    assert plan["short_window_count"] == 3
    assert plan["short_windows"][1]["silent"] is True
    assert plan["short_windows"][2]["partial"] is True
    assert plan["short_windows"][2]["duration_ms"] == 20_000


def test_sixty_minutes_contains_twelve_short_windows() -> None:
    plan = plan_windows(str(uuid4()), [], LONG_WINDOW_MS)
    assert plan["short_window_count"] == 12
    assert plan["long_window_count"] == 1
    assert plan["long_windows"][0]["short_window_count"] == 12
    assert plan["short_windows_per_full_long_window"] == 12


def test_window_ids_are_deterministic() -> None:
    session_id = str(uuid4())
    first = plan_windows(session_id, [], 300_000)
    second = plan_windows(session_id, [], 300_000)
    assert first["short_windows"][0]["window_id"] == second["short_windows"][0]["window_id"]


def test_diagnostic_runtime_clamps_small_polling_overshoot(tmp_path: Path) -> None:
    write_json(tmp_path / "lifecycle.json", {
        "stop_reason": "diagnostic_max_runtime",
        "capture_started_at": "2026-08-13T00:00:00+00:00",
        "stopped_at": "2026-08-13T00:30:00.206+00:00",
    })
    write_json(tmp_path / "request.json", {"max_runtime_seconds": 1800})
    session = {"capture_clock_started_at": "2026-08-13T00:00:00+00:00"}
    assert determine_session_duration_ms(tmp_path, session, []) == 1_800_000


def test_non_diagnostic_duration_keeps_actual_tail(tmp_path: Path) -> None:
    write_json(tmp_path / "lifecycle.json", {
        "stop_reason": "operator_stop_command",
        "capture_started_at": "2026-08-13T00:00:00+00:00",
        "stopped_at": "2026-08-13T00:30:00.206+00:00",
    })
    write_json(tmp_path / "request.json", {"max_runtime_seconds": 1800})
    session = {"capture_clock_started_at": "2026-08-13T00:00:00+00:00"}
    assert determine_session_duration_ms(tmp_path, session, []) == 1_800_206
