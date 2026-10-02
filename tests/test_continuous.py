from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.continuous import (
    ContinuousRequest,
    _merge_transcripts,
    _purge_chunk_audio,
    next_local_cutoff,
    rebase_browser_chunk,
    request_stop,
)
from oopz_capture.output import write_json, write_jsonl


def valid_request(**changes) -> ContinuousRequest:
    values = {
        "request_id": str(uuid4()),
        "area_id": "area",
        "channel_id": "channel",
        "consent_confirmed": True,
    }
    values.update(changes)
    request = ContinuousRequest(**values)
    request.validate()
    return request


def test_five_minutes_is_hard_maximum() -> None:
    assert valid_request().chunk_seconds == 300
    with pytest.raises(ValueError, match="hard maximum"):
        valid_request(chunk_seconds=301)


def test_next_cutoff_uses_today_or_tomorrow_local_04() -> None:
    zone = timezone.utc
    before = datetime(2026, 8, 13, 2, 30, tzinfo=zone)
    after = datetime(2026, 8, 13, 4, 1, tzinfo=zone)
    assert next_local_cutoff(before) == datetime(2026, 8, 13, 4, 0, tzinfo=zone)
    assert next_local_cutoff(after) == datetime(2026, 8, 14, 4, 0, tzinfo=zone)


def test_rebase_preserves_first_frame_without_hours_of_silence() -> None:
    chunk = {
        "uid": "123",
        "sampleRate": 1000,
        "frameCount": 100,
        "generation": 1,
        "sessionOffsetMs": 300100,
        "pcm16Base64": base64.b64encode(b"\x00\x00" * 100).decode("ascii"),
    }
    rebased = rebase_browser_chunk(chunk, 300000)
    assert rebased["sessionOffsetMs"] == 100


def test_stop_request_only_targets_active_managed_session(tmp_path: Path) -> None:
    session_id = str(uuid4())
    session = tmp_path / session_id
    session.mkdir()
    write_json(session / "lifecycle.json", {
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "status": "recording",
    })
    path = request_stop(tmp_path, session_id, reason="local_stop_command")
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["schema_version"] == "oopz.continuous.stop.v1"
    assert value["session_id"] == session_id
    assert value["reason"] == "local_stop_command"


def test_stop_request_accepts_readable_beijing_session_id(tmp_path: Path) -> None:
    session_id = "2026-08-13_22-15-30_BJT"
    session = tmp_path / session_id
    session.mkdir()
    write_json(session / "lifecycle.json", {
        "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "recording",
    })
    path = request_stop(tmp_path, session_id)
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == session_id


def test_chunk_audio_is_deleted_but_transcript_is_retained(tmp_path: Path) -> None:
    session = tmp_path / str(uuid4())
    chunk = session / "chunks" / f"000001-{uuid4()}"
    audio = chunk / "audio"
    audio.mkdir(parents=True)
    (audio / "123.wav").write_bytes(b"RIFF")
    write_json(chunk / "audio_manifest.json", [{"agora_uid": 123}])
    (chunk / "transcript.md").write_text("人类可读文本", encoding="utf-8")
    deleted = _purge_chunk_audio(session, chunk)
    assert deleted == [f"chunks/{chunk.name}/audio/123.wav"]
    assert not audio.exists()
    assert (chunk / "transcript.md").read_text(encoding="utf-8") == "人类可读文本"


def test_merge_uses_parent_session_timeline_and_removes_live_audio_pointer(tmp_path: Path) -> None:
    session = tmp_path / str(uuid4())
    chunk = session / "chunks" / f"000001-{uuid4()}"
    chunk.mkdir(parents=True)
    chunk_id = str(uuid4())
    write_json(session / "session.json", {
        "session_id": session.name,
        "started_at": "2026-08-13T00:00:00+00:00",
    })
    write_json(chunk / "chunk.json", {
        "chunk_id": chunk_id,
        "chunk_index": 1,
        "session_offset_ms": 300000,
    })
    write_jsonl(chunk / "transcript.jsonl", [{
        "segment_id": str(uuid4()),
        "session_id": chunk_id,
        "start_ms": 1000,
        "end_ms": 2000,
        "start_time": "2026-08-13T00:05:01+00:00",
        "end_time": "2026-08-13T00:05:02+00:00",
        "agora_uid": 123,
        "oopz_uid": "oopz-id",
        "speaker": "测试用户",
        "text": "测试内容",
        "audio_file": "audio/123.wav",
        "audio_start_sample": 1,
        "audio_end_sample": 2,
    }])
    count, markdown = _merge_transcripts(session, [{"chunk_dir": chunk, "ok": True}])
    value = json.loads((session / "transcript.jsonl").read_text(encoding="utf-8"))
    assert count == 1
    assert value["session_id"] == session.name
    assert value["chunk_id"] == chunk_id
    assert value["start_ms"] == 301000
    assert value["source_audio_deleted"] is True
    assert "audio_file" not in value
    assert "Session ID: " + session.name in markdown.read_text(encoding="utf-8")
