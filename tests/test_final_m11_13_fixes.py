from __future__ import annotations

from uuid import uuid4

from oopz_capture.analysis_windows import plan_windows
from oopz_capture.deepseek_client import DeepSeekConfig


def test_long_window_preserves_speaker_nickname() -> None:
    plan = plan_windows(str(uuid4()), [{
        "segment_id": str(uuid4()),
        "session_id": "unused",
        "start_ms": 1000,
        "end_ms": 2000,
        "agora_uid": 123,
        "oopz_uid": "oopz-user",
        "speaker": "测试昵称",
        "text": "内容",
    }], 300_000)
    assert plan["long_windows"][0]["speakers"] == [{
        "nickname": "测试昵称",
        "oopz_uid": "oopz-user",
        "agora_uid": 123,
    }]


def test_deepseek_config_repr_hides_api_key() -> None:
    api_key = "synthetic-key-" + uuid4().hex
    value = DeepSeekConfig(
        api_key=api_key, base_url="https://api.example.test", model="model-id",
    )
    assert api_key not in repr(value)
