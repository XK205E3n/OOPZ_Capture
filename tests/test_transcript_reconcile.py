from __future__ import annotations

import json
from pathlib import Path

from oopz_capture.output import write_json, write_jsonl
from oopz_capture.transcript_reconcile import reconcile_sensevoice_pair
from oopz_capture.ollama_client import OllamaError


class FakeQwen:
    def complete_json(self, **kwargs):
        assert kwargs["thinking"] == "enabled"
        return {
            "content": {"corrections": [{"segment_id": "s2", "source": "auto", "reason": "英文名"}]},
            "metadata": {"provider": "fake-qwen", "usage": {"total_tokens": 1}},
        }


def _record(segment_id: str, text: str, start_ms: int) -> dict:
    return {
        "segment_id": segment_id, "session_id": "session", "start_ms": start_ms,
        "end_ms": start_ms + 1000, "agora_uid": 1, "oopz_uid": "u1",
        "speaker": "用户A", "text": text, "language": "zh", "asr_backend": "sensevoice-small",
    }


def test_reconcile_uses_qwen_selection_without_inventing_text(tmp_path: Path) -> None:
    write_json(tmp_path / "session.json", {"session_id": "session", "started_at": "2026-08-20T00:00:00+00:00"})
    write_jsonl(tmp_path / "transcript.zh.jsonl", [_record("s1", "你好", 0), _record("s2", "迪普西克", 1000)])
    write_jsonl(tmp_path / "transcript.auto.jsonl", [_record("s1", "你好", 0), _record("s2", "DeepSeek", 1000)])
    final, summary = reconcile_sensevoice_pair(tmp_path, client=FakeQwen())
    assert [item["text"] for item in final] == ["你好", "DeepSeek"]
    assert final[1]["transcript_source"] == "sensevoice-auto"
    assert summary["selection_counts"] == {"sensevoice-zh": 1, "sensevoice-auto": 1}
    audit = json.loads((tmp_path / "transcript.reconcile.json").read_text(encoding="utf-8"))
    assert audit["decisions"][0]["segment_id"] == "s2"


def test_reconcile_marks_a_genuinely_empty_time_range_without_calling_qwen(tmp_path: Path) -> None:
    write_json(tmp_path / "session.json", {
        "session_id": "silent-session",
        "started_at": "2026-08-20T00:00:00+00:00",
        "capture_clock_started_at": "2026-08-20T00:00:00+00:00",
        "duration_seconds": 300,
    })
    write_jsonl(tmp_path / "transcript.zh.jsonl", [])
    write_jsonl(tmp_path / "transcript.auto.jsonl", [])

    final, summary = reconcile_sensevoice_pair(tmp_path, client=FakeQwen())

    assert len(final) == 1
    assert final[0]["text"] == "[该时间段未检测到有效语音文本]"
    assert final[0]["start_ms"] == 0
    assert final[0]["end_ms"] == 300_000
    assert summary["no_speech"] is True
    assert summary["qwen_decisions"] == 0


def test_reconcile_falls_back_to_nonthinking_after_invalid_qwen_output(tmp_path: Path) -> None:
    class ThinkingFailureThenValid:
        def __init__(self) -> None:
            self.calls = []

        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["thinking"] == "enabled":
                raise OllamaError("invalid JSON after retries")
            return {
                "content": {"corrections": []},
                "metadata": {"provider": "fake-qwen", "usage": {"total_tokens": 1}},
            }

    write_json(tmp_path / "session.json", {
        "session_id": "session", "started_at": "2026-08-20T00:00:00+00:00",
    })
    write_jsonl(tmp_path / "transcript.zh.jsonl", [_record("s1", "你好", 0)])
    write_jsonl(tmp_path / "transcript.auto.jsonl", [_record("s1", "你好", 0)])
    client = ThinkingFailureThenValid()
    final, summary = reconcile_sensevoice_pair(tmp_path, client=client)  # type: ignore[arg-type]

    assert final[0]["text"] == "你好"
    assert [call["thinking"] for call in client.calls] == ["enabled", "disabled"]
    assert summary["qwen_metadata"][0]["fallback"] == "nonthinking_after_thinking_failure"
