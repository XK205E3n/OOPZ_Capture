from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.analyzer_job import load_analyzer_input, prepare_analysis
from oopz_capture.output import write_json, write_jsonl


def make_session(
    tmp_path: Path,
    *,
    transcript_relative: str = "transcript.jsonl",
    session_id: str | None = None,
) -> Path:
    session_id = session_id or str(uuid4())
    request_id = str(uuid4())
    session = tmp_path / session_id
    handoff = session / "handoff" / "analyzer_request.json"
    now = datetime.now(timezone.utc)
    write_json(session / "session.json", {
        "session_id": session_id,
        "started_at": now.isoformat(),
        "capture_clock_started_at": now.isoformat(),
        "duration_seconds": 601,
    })
    write_json(session / "users.json", [{
        "nickname": "测试用户",
        "oopz_uid": "oopz-user",
        "agora_uid": 123,
    }])
    transcript = [{
        "segment_id": str(uuid4()),
        "session_id": session_id,
        "start_ms": 299_000,
        "end_ms": 301_000,
        "agora_uid": 123,
        "oopz_uid": "oopz-user",
        "speaker": "测试用户",
        "text": "跨越300秒边界",
    }]
    if transcript_relative == "transcript.jsonl":
        write_jsonl(session / transcript_relative, transcript)
    (session / "transcript.md").write_text(f"Session ID: {session_id}\n", encoding="utf-8")
    write_json(session / "transcript_summary.json", {"segments": 1})
    write_json(handoff, {
        "schema_version": "oopz.analyzer.request.v1",
        "request_id": request_id,
        "session_id": session_id,
        "created_at": now.isoformat(),
        "analysis_deadline_at": (now + timedelta(minutes=15)).isoformat(),
        "encoding": "UTF-8",
        "delivery_mode": "final_only",
        "summary_windows": {"short_summary_seconds": 300, "long_summary_seconds": 3600},
        "inputs": {
            "transcript_jsonl": transcript_relative,
            "transcript_markdown": "transcript.md",
            "transcript_summary": "transcript_summary.json",
            "users": "users.json",
            "session": "session.json",
            "segment_count": 1,
        },
        "required_outputs": {},
        "retention": {
            "delete_after": (now + timedelta(hours=168)).isoformat(),
            "maximum_hours": 168,
        },
    })
    return handoff


def test_load_validates_ids_utf8_and_summary_windows(tmp_path: Path) -> None:
    value = load_analyzer_input(make_session(tmp_path))
    assert value.short_summary_seconds == 300
    assert value.long_summary_seconds == 3600
    assert value.transcript[0]["text"] == "跨越300秒边界"
    assert len(value.fingerprint) == 64


def test_analyzer_accepts_readable_beijing_session_id(tmp_path: Path) -> None:
    value = load_analyzer_input(make_session(
        tmp_path, session_id="2026-08-13_22-15-30_BJT"
    ))
    assert value.session_id == "2026-08-13_22-15-30_BJT"


def test_input_path_may_not_escape_session(tmp_path: Path) -> None:
    handoff = make_session(tmp_path, transcript_relative="../outside.jsonl")
    with pytest.raises(ValueError, match="safe relative"):
        load_analyzer_input(handoff)


def test_prepare_is_idempotent_and_writes_checkpoint(tmp_path: Path) -> None:
    handoff = make_session(tmp_path)
    first = prepare_analysis(handoff)
    second = prepare_analysis(handoff)
    assert first["reused"] is False
    assert second["reused"] is True
    assert first["job"]["input_fingerprint"] == second["job"]["input_fingerprint"]
    assert first["windows"]["short_window_count"] == 3
    checkpoint = json.loads((handoff.parent.parent / "analysis" / "checkpoint.json").read_text(encoding="utf-8"))
    assert checkpoint["completed_short_window_ids"] == []
    lifecycle = json.loads((handoff.parent.parent / "analysis" / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "windows_ready"
