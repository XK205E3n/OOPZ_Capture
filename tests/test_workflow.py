from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.output import write_json, write_jsonl
from oopz_capture.workflow import (
    REQUEST_SCHEMA,
    WorkflowRequest,
    cleanup_expired,
    run_workflow,
    validate_transcript,
)


def request(**changes) -> WorkflowRequest:
    value = {
        "schema_version": REQUEST_SCHEMA,
        "command": "record_and_transcribe",
        "request_id": str(uuid4()),
        "area_id": "area-id",
        "channel_id": "channel-id",
        "duration_seconds": 90,
        "consent_confirmed": True,
        "language": "zh",
        "processing_deadline_seconds": 900,
        "retention_hours": 168,
    }
    value.update(changes)
    return WorkflowRequest.from_dict(value)


def make_capture(session_dir: Path) -> None:
    session_dir.mkdir(parents=True)
    write_json(session_dir / "session.json", {
        "session_id": session_dir.name,
        "started_at": datetime.now(timezone.utc).isoformat(),
    })
    write_json(session_dir / "users.json", [{
        "nickname": "星铸E3",
        "oopz_uid": "oopz-user-id",
        "agora_uid": 308348510,
    }])
    write_json(session_dir / "audio_manifest.json", [{
        "agora_uid": 308348510,
        "file": "audio/308348510.wav",
    }])
    (session_dir / "audio").mkdir()
    (session_dir / "audio" / "308348510.wav").write_bytes(b"RIFF-test")


def make_transcript(session_dir: Path) -> None:
    item = {
        "segment_id": str(uuid4()),
        "session_id": session_dir.name,
        "start_ms": 1000,
        "end_ms": 2000,
        "agora_uid": 308348510,
        "oopz_uid": "oopz-user-id",
        "speaker": "星铸E3",
        "text": "中文内容可读",
    }
    write_jsonl(session_dir / "transcript.jsonl", [item])
    write_json(session_dir / "transcript_summary.json", {"segments": 1})
    (session_dir / "transcript.md").write_text(
        f"# 转写\n\nSession ID: {session_dir.name}\n\n中文内容可读\n",
        encoding="utf-8",
    )


def test_request_rejects_missing_consent_and_more_than_one_week() -> None:
    with pytest.raises(ValueError, match="consent_confirmed"):
        request(consent_confirmed=False)
    with pytest.raises(ValueError, match="retention_hours"):
        request(retention_hours=169)


def test_validate_transcript_preserves_utf8_and_ids(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid4())
    make_capture(session_dir)
    make_transcript(session_dir)
    result = validate_transcript(session_dir)
    assert result == {"session_id": session_dir.name, "segment_count": 1}
    assert "中文内容可读" in (session_dir / "transcript.md").read_text(encoding="utf-8")


def test_workflow_handoff_then_deletes_audio(tmp_path: Path) -> None:
    async def capture_runner(config, **kwargs):
        del config
        session_dir = kwargs["output_root"] / str(uuid4())
        make_capture(session_dir)
        return session_dir

    async def transcription_runner(session_dir, workflow_request, vad_config, device, timeout):
        del workflow_request, vad_config, device
        assert timeout > 0
        make_transcript(session_dir)
        return "转写完成\n", ""

    session_dir = asyncio.run(run_workflow(
        object(),
        request(),
        output_root=tmp_path,
        capture_runner=capture_runner,
        transcription_runner=transcription_runner,
    ))
    lifecycle = json.loads((session_dir / "lifecycle.json").read_text(encoding="utf-8"))
    handoff = json.loads((session_dir / "handoff" / "analyzer_request.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "ready_for_analysis"
    assert lifecycle["audio_deleted"] is True
    assert not (session_dir / "audio").exists()
    assert handoff["session_id"] == session_dir.name
    assert handoff["inputs"]["segment_count"] == 1
    assert handoff["summary_windows"] == {
        "short_summary_seconds": 300,
        "long_summary_seconds": 3600,
    }
    assert (session_dir / "transcript.jsonl").is_file()


def test_workflow_retain_audio_for_asr_experiment(tmp_path: Path) -> None:
    async def capture_runner(config, **kwargs):
        del config
        session_dir = kwargs["output_root"] / str(uuid4())
        make_capture(session_dir)
        return session_dir

    async def transcription_runner(session_dir, workflow_request, vad_config, device, timeout):
        del workflow_request, vad_config, device
        assert timeout > 0
        make_transcript(session_dir)
        return "转写完成\n", ""

    session_dir = asyncio.run(run_workflow(
        object(), request(retain_audio=True), output_root=tmp_path,
        capture_runner=capture_runner,
        transcription_runner=transcription_runner,
    ))
    lifecycle = json.loads((session_dir / "lifecycle.json").read_text(encoding="utf-8"))
    handoff = json.loads((session_dir / "handoff" / "analyzer_request.json").read_text(encoding="utf-8"))
    assert lifecycle["audio_deleted"] is False
    assert lifecycle["audio_retained_for_testing"] is True
    assert (session_dir / "audio" / "308348510.wav").is_file()
    assert handoff["retention"]["audio_retained_for_testing"] is True


def test_workflow_failure_retains_audio(tmp_path: Path) -> None:
    async def capture_runner(config, **kwargs):
        del config
        session_dir = kwargs["output_root"] / str(uuid4())
        make_capture(session_dir)
        return session_dir

    async def failed_transcription(*args):
        raise RuntimeError("ASR unavailable")

    with pytest.raises(RuntimeError, match="ASR unavailable"):
        asyncio.run(run_workflow(
            object(), request(), output_root=tmp_path,
            capture_runner=capture_runner,
            transcription_runner=failed_transcription,
        ))
    session_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    lifecycle = json.loads((session_dir / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "failed"
    assert lifecycle["audio_deleted"] is False
    assert (session_dir / "audio" / "308348510.wav").is_file()


def test_cleanup_only_deletes_expired_managed_sessions(tmp_path: Path) -> None:
    expired = tmp_path / str(uuid4())
    active = tmp_path / str(uuid4())
    legacy = tmp_path / str(uuid4())
    for path in (expired, active, legacy):
        path.mkdir()
        (path / "keep.txt").write_text("text", encoding="utf-8")
    now = datetime.now(timezone.utc)
    write_json(expired / "lifecycle.json", {
        "managed_by": "oopz-worker-v1",
        "delete_after": (now - timedelta(seconds=1)).isoformat(),
    })
    write_json(active / "lifecycle.json", {
        "managed_by": "oopz-worker-v1",
        "delete_after": (now + timedelta(hours=1)).isoformat(),
    })
    write_json(legacy / "lifecycle.json", {
        "managed_by": "some-other-program",
        "delete_after": (now - timedelta(days=1)).isoformat(),
    })
    report_dir = tmp_path / "Report" / "2026-08-21"
    report_dir.mkdir(parents=True)
    expired_pdf = report_dir / "expired.pdf"
    unrelated_pdf = report_dir / "unrelated.pdf"
    expired_pdf.write_bytes(b"%PDF-1.4\n")
    unrelated_pdf.write_bytes(b"%PDF-1.4\n")
    write_json(expired / "report_archive.json", {
        "schema_version": "oopz.report.archive.v1",
        "session_id": expired.name,
        "files": ["Report/2026-08-21/expired.pdf"],
    })

    preview = cleanup_expired(tmp_path, now=now, dry_run=True)
    assert preview == [expired.resolve()]
    assert expired.exists()
    deleted = cleanup_expired(tmp_path, now=now)
    assert deleted == [expired.resolve()]
    assert not expired.exists()
    assert not expired_pdf.exists()
    assert unrelated_pdf.exists()
    assert active.exists() and legacy.exists()
