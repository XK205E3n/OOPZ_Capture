from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from oopz_capture.output import write_json, write_jsonl
from oopz_capture.workflow import validate_transcript


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


def test_validate_transcript_preserves_utf8_and_ids(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid4())
    make_capture(session_dir)
    make_transcript(session_dir)
    result = validate_transcript(session_dir)
    assert result == {"session_id": session_dir.name, "segment_count": 1}
    assert "中文内容可读" in (session_dir / "transcript.md").read_text(encoding="utf-8")
