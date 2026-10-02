from __future__ import annotations

from pathlib import Path

import pytest

from oopz_capture.analyzer_job import _safe_session_file
from oopz_capture.deepseek_client import DeepSeekClient, DeepSeekConfig


def test_safe_session_file_rejects_symlink_before_resolving(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    real = session / "real.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    link = session / "linked.jsonl"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("this Windows account cannot create a symlink")
    with pytest.raises(ValueError, match="symlink or reparse"):
        _safe_session_file(session, "linked.jsonl", "transcript")


def test_finish_reason_length_is_retried() -> None:
    responses = [
        {
            "choices": [{
                "message": {"content": '{"summary":"truncated but parseable"}'},
                "finish_reason": "length",
            }],
        },
        {
            "choices": [{
                "message": {"content": '{"summary":"complete"}'},
                "finish_reason": "stop",
            }],
        },
    ]
    sleeps = []
    client = DeepSeekClient(
        DeepSeekConfig(
            api_key="secret", base_url="https://api.example.test", model="exact-model",
            timeout_seconds=5, max_retries=1, min_interval_seconds=0, max_tokens=512,
        ),
        transport=lambda *args: responses.pop(0),
        sleeper=sleeps.append,
        random_source=lambda: 0,
    )
    result = client.complete_json(
        system_prompt="Return JSON only.",
        user_prompt="Return JSON with summary.",
        required_keys={"summary": str},
    )
    assert result["content"]["summary"] == "complete"
    assert result["metadata"]["attempts"] == 2
    assert sleeps == [1]
