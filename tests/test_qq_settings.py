from __future__ import annotations

import os

import pytest

from oopz_capture.qq_settings import apply_setting, canonical_setting_key, setting_status


def test_recording_cutoff_and_empty_timeout_accept_friendly_qq_values(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.delenv("OOPZ_CUTOFF_LOCAL_HOUR", raising=False)
    monkeypatch.delenv("OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS", raising=False)

    assert canonical_setting_key("强制结束时间") == "OOPZ_CUTOFF_LOCAL_HOUR"
    assert apply_setting("强制结束时间", "04:00", env_path=env_path) == "4"
    assert canonical_setting_key("无人结束退出时间") == "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS"
    assert apply_setting("无人退出时间", "5m", env_path=env_path) == "300"

    assert env_path.read_text(encoding="utf-8") == (
        "OOPZ_CUTOFF_LOCAL_HOUR=4\n"
        "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS=300\n"
    )
    assert setting_status(env_path)["OOPZ_CUTOFF_LOCAL_HOUR"] == "4"
    assert setting_status(env_path)["OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS"] == "300"
    assert "OOPZ_DEFAULT_AREA_ID" not in setting_status(env_path)
    assert "OOPZ_DEFAULT_CHANNEL_ID" not in setting_status(env_path)


def test_generic_api_settings_accept_friendly_qq_values(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    monkeypatch.delenv("ANALYZER_API_KEY", raising=False)
    monkeypatch.delenv("ANALYZER_BASE_URL", raising=False)
    monkeypatch.delenv("ANALYZER_MODEL", raising=False)

    assert apply_setting("分析供应商", "openai-compatible", env_path=env_path) == "openai-compatible"
    assert apply_setting("分析API密钥", "go-test-key", env_path=env_path) == "已设置（长度 11）"
    assert apply_setting("分析API地址", "https://example.test/v1", env_path=env_path) == "https://example.test/v1"
    assert apply_setting("分析模型", "vendor/model-v1", env_path=env_path) == "vendor/model-v1"
    assert env_path.read_text(encoding="utf-8") == (
        "ANALYZER_PROVIDER=openai-compatible\n"
        "ANALYZER_API_KEY=go-test-key\n"
        "ANALYZER_BASE_URL=https://example.test/v1\n"
        "ANALYZER_MODEL=vendor/model-v1\n"
    )
    # apply_setting intentionally updates the live process environment without
    # going through monkeypatch.setenv; avoid leaking this fixture into later
    # provider-default tests in the same pytest process.
    for key in ("ANALYZER_PROVIDER", "ANALYZER_API_KEY", "ANALYZER_BASE_URL", "ANALYZER_MODEL"):
        os.environ.pop(key, None)


def test_chunk_setting_matches_continuous_five_minute_hard_limit(tmp_path) -> None:
    env_path = tmp_path / ".env"
    assert apply_setting("分片时长", "300", env_path=env_path) == "300"
    with pytest.raises(ValueError, match="30-300"):
        apply_setting("分片时长", "301", env_path=env_path)
    assert "OOPZ_CHUNK_SECONDS=301" not in env_path.read_text(encoding="utf-8")


def test_report_reply_timeout_is_qq_settable(tmp_path) -> None:
    env_path = tmp_path / ".env"
    assert canonical_setting_key("报告回复超时") == "OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS"
    assert apply_setting("报告回复超时", "180", env_path=env_path) == "180"
    with pytest.raises(ValueError, match="30-1800"):
        apply_setting("报告回复超时", "29", env_path=env_path)
