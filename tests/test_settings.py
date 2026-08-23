from __future__ import annotations

import os

import pytest

from oopz_capture.settings import apply_setting, canonical_setting_key, setting_status


def test_recording_cutoff_and_empty_timeout_accept_friendly_values(tmp_path, monkeypatch) -> None:
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


def test_generic_api_settings_accept_friendly_values(tmp_path, monkeypatch) -> None:
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


def test_non_sensitive_connectivity_settings_are_settable(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    keys = {
        "OOPZ_POLL_INTERVAL_SECONDS": "0.5",
        "OOPZ_MEMBERSHIP_REFRESH_SECONDS": "45",
        "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS": "2",
        "OOPZ_RECONNECT_MAX_DELAY_SECONDS": "60",
    }
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    for key, value in keys.items():
        assert apply_setting(key, value, env_path=env_path) == value
    for key, value in keys.items():
        assert f"{key}={value}" in env_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="不能小于"):
        apply_setting("OOPZ_RECONNECT_MAX_DELAY_SECONDS", "1", env_path=env_path)


def test_unconfigured_runtime_settings_report_effective_defaults(tmp_path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    for key in (
        "OOPZ_PROCESSING_DEADLINE_SECONDS",
        "ANALYZER_PROVIDER", "ANALYZER_BASE_URL", "ANALYZER_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)

    status = setting_status(env_path)

    assert status["OOPZ_PROCESSING_DEADLINE_SECONDS"] == "900"
    assert "OOPZ_SHOW_BROWSER" not in status
    assert status["ANALYZER_PROVIDER"] == "opencode-go"
    assert status["ANALYZER_BASE_URL"] == "https://opencode.ai/zen/go/v1"
    assert status["ANALYZER_MODEL"] == "mimo-v2.5"
