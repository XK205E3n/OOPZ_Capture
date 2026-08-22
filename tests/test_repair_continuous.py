from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.asr import resolve_sensevoice_model
from oopz_capture.continuous import ContinuousRequest, repair_continuous_session
from oopz_capture.output import write_json, write_jsonl


def test_model_path_can_be_explicit_or_configured_by_environment(tmp_path: Path, monkeypatch) -> None:
    model = tmp_path / "SenseVoiceSmall"
    model.mkdir()
    (model / "model.pt").write_bytes(b"model")
    assert resolve_sensevoice_model(str(model)) == model.resolve()
    monkeypatch.setenv("OOPZ_SENSEVOICE_MODEL", str(model))
    assert resolve_sensevoice_model() == model.resolve()
    with pytest.raises(RuntimeError, match="missing"):
        resolve_sensevoice_model(str(tmp_path / "missing"))


def _write_chunk_transcript(chunk: Path, parent_id: str, index: int, offset_ms: int) -> None:
    chunk_id = chunk.name.split("-", 1)[1]
    write_json(chunk / "chunk.json", {
        "chunk_id": chunk_id, "chunk_index": index,
        "parent_session_id": parent_id, "session_offset_ms": offset_ms,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(chunk / "session.json", {
        "session_id": chunk_id, "parent_session_id": parent_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "capture_clock_started_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": 300,
    })
    segment = {
        "segment_id": str(uuid4()), "session_id": chunk_id,
        "start_ms": 1000, "end_ms": 2000, "agora_uid": 123,
        "oopz_uid": "oopz-user", "speaker": "测试用户", "text": f"第{index}段内容",
    }
    write_jsonl(chunk / "transcript.jsonl", [segment])
    (chunk / "transcript.md").write_text(f"Session ID: {chunk_id}\n", encoding="utf-8")
    write_json(chunk / "transcript_summary.json", {"segments": 1})


def test_repair_retries_only_failed_chunks_and_rebuilds_parent(tmp_path: Path) -> None:
    asyncio.run(_repair_retries_only_failed_chunks_and_rebuilds_parent(tmp_path))


async def _repair_retries_only_failed_chunks_and_rebuilds_parent(tmp_path: Path) -> None:
    session_id = str(uuid4())
    request = ContinuousRequest(
        request_id=str(uuid4()), area_id="area", channel_id="channel", consent_confirmed=True,
    )
    session = tmp_path / session_id
    now = datetime.now(timezone.utc)
    write_json(session / "request.json", request.to_dict())
    write_json(session / "session.json", {
        "session_id": session_id, "started_at": now.isoformat(),
        "capture_clock_started_at": now.isoformat(), "duration_seconds": 600,
    })
    write_json(session / "users.json", [{
        "nickname": "测试用户", "oopz_uid": "oopz-user", "agora_uid": 123,
    }])
    write_json(session / "lifecycle.json", {
        "managed_by": "oopz-worker-v1", "mode": "continuous",
        "status": "ready_for_analysis_with_errors", "stopped_at": now.isoformat(),
        "delete_after": (now + timedelta(hours=168)).isoformat(),
    })

    first_id, second_id = str(uuid4()), str(uuid4())
    first = session / "chunks" / f"000001-{first_id}"
    second = session / "chunks" / f"000002-{second_id}"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    _write_chunk_transcript(first, session_id, 1, 0)
    write_json(first / "lifecycle.json", {"status": "transcribed"})
    write_json(second / "chunk.json", {
        "chunk_id": second_id, "chunk_index": 2, "parent_session_id": session_id,
        "session_offset_ms": 300000, "closed_at": now.isoformat(),
    })
    write_json(second / "lifecycle.json", {"status": "failed"})
    audio = second / "audio"
    audio.mkdir()
    (audio / "123.wav").write_bytes(b"RIFF")
    calls = []

    async def processor(parent, chunk, saved_request, vad_config, device, reset_deadline=False):
        calls.append((chunk.name, reset_deadline))
        _write_chunk_transcript(chunk, session_id, 2, 300000)
        write_json(chunk / "lifecycle.json", {"status": "transcribed"})
        (audio / "123.wav").unlink()
        audio.rmdir()
        return {"chunk_dir": chunk, "ok": True, "segments": 1}

    await repair_continuous_session(tmp_path, session_id, chunk_processor=processor)
    assert calls == [(second.name, True)]
    parent_records = [json.loads(line) for line in (session / "transcript.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(parent_records) == 2
    assert parent_records[1]["start_ms"] == 301000
    lifecycle = json.loads((session / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "ready_for_analysis"
    assert lifecycle["chunks_transcribed"] == 2
    assert lifecycle["chunks_failed"] == 0
    assert lifecycle["audio_deleted"] is True
    handoff = json.loads((session / "handoff" / "analyzer_request.json").read_text(encoding="utf-8"))
    assert handoff["inputs"]["segment_count"] == 2
    assert handoff["inputs"]["failed_chunk_ids"] == []
