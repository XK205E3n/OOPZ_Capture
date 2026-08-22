from __future__ import annotations

import json

import pytest

from oopz_capture.deepseek_client import (
    DeepSeekClient,
    DeepSeekConfig,
    DeepSeekError,
    MockDeepSeekClient,
    RateLimiter,
    RetryableDeepSeekError,
    configured_analysis_client,
    opencode_go_client,
    opencode_mimo_v25_client,
)


def config(**changes) -> DeepSeekConfig:
    values = {
        "api_key": "secret-key-must-not-leak",
        "base_url": "https://api.example.test",
        "model": "exact-model-id",
        "timeout_seconds": 5,
        "max_retries": 2,
        "min_interval_seconds": 0,
        "max_tokens": 512,
    }
    values.update(changes)
    return DeepSeekConfig(**values)


def success(content: dict) -> dict:
    return {
        "id": "response-id",
        "model": "returned-model",
        "choices": [{
            "message": {"content": json.dumps(content, ensure_ascii=False)},
            "finish_reason": "stop",
        }],
        "usage": {"total_tokens": 10},
    }


def test_client_sends_json_mode_and_returns_structured_metadata() -> None:
    observed = {}

    def transport(endpoint, headers, payload, timeout):
        observed.update({"endpoint": endpoint, "headers": headers, "payload": payload, "timeout": timeout})
        return success({"summary": "测试", "items": []})

    result = DeepSeekClient(config(), transport=transport).complete_json(
        system_prompt="Return JSON only.",
        user_prompt="Produce a JSON summary.",
        required_keys={"summary": str, "items": list},
    )
    assert observed["endpoint"] == "https://api.example.test/chat/completions"
    assert observed["payload"]["response_format"] == {"type": "json_object"}
    assert observed["payload"]["model"] == "exact-model-id"
    assert observed["payload"]["max_tokens"] == 16384
    assert result["content"]["summary"] == "测试"
    assert result["metadata"]["usage"] == {"total_tokens": 10}
    assert result["metadata"]["requested_at"].endswith("+00:00")
    assert result["metadata"]["usage_by_request"] == [{
        "requested_at": result["metadata"]["requested_at"],
        "usage": {"total_tokens": 10},
    }]


def test_empty_content_and_invalid_shape_are_retried() -> None:
    responses = [
        {"choices": [{"message": {"content": ""}}]},
        success({"wrong": "shape"}),
        success({"summary": "ok", "items": []}),
    ]
    sleeps = []

    def transport(*args):
        return responses.pop(0)

    result = DeepSeekClient(config(), transport=transport, sleeper=sleeps.append, random_source=lambda: 0).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str, "items": list},
    )
    assert result["metadata"]["attempts"] == 3
    assert sleeps == [1, 2]


def test_api_key_does_not_appear_in_errors() -> None:
    def transport(*args):
        raise RetryableDeepSeekError("temporary outage")

    with pytest.raises(DeepSeekError) as captured:
        DeepSeekClient(config(max_retries=0), transport=transport).complete_json(
            system_prompt="Return JSON only.", user_prompt="Produce JSON.", required_keys={"summary": str},
        )
    assert "secret-key-must-not-leak" not in str(captured.value)


def test_prompt_must_explicitly_request_json() -> None:
    with pytest.raises(ValueError, match="explicitly mention JSON"):
        DeepSeekClient(config(), transport=lambda *args: success({})).complete_json(
            system_prompt="Summarize.", user_prompt="Please summarize.", required_keys={},
        )


def test_mock_client_is_offline_and_deterministic() -> None:
    result = MockDeepSeekClient().complete_json(
        system_prompt="unused", user_prompt="unused",
        required_keys={"summary": str, "items": list, "uncertain": bool},
    )
    assert result["content"] == {"summary": "mock-summary", "items": [], "uncertain": False}
    assert result["metadata"]["provider"] == "mock"


def test_short_summary_disables_thinking_and_uses_temperature() -> None:
    observed = {}

    def transport(endpoint, headers, payload, timeout):
        observed.update(payload)
        return success({"summary": "ok"})

    result = DeepSeekClient(config(), transport=transport).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="disabled", reasoning_effort=None,
    )
    assert observed["thinking"] == {"type": "disabled"}
    assert observed["temperature"] == 0.1
    assert "reasoning_effort" not in observed
    assert observed["max_tokens"] == 512
    assert result["metadata"]["thinking"] == "disabled"
    assert result["metadata"]["reasoning_effort"] is None


def test_long_summary_enables_high_reasoning_without_temperature() -> None:
    observed = {}

    def transport(endpoint, headers, payload, timeout):
        observed.update(payload)
        return success({"summary": "ok"})

    result = DeepSeekClient(config(), transport=transport).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="enabled", reasoning_effort="high",
    )
    assert observed["thinking"] == {"type": "enabled"}
    assert observed["reasoning_effort"] == "high"
    assert observed["max_tokens"] == 16384
    assert "temperature" not in observed
    assert result["metadata"]["thinking"] == "enabled"
    assert result["metadata"]["reasoning_effort"] == "high"


def test_thinking_call_can_use_a_smaller_initial_budget() -> None:
    observed = {}

    def transport(endpoint, headers, payload, timeout):
        observed.update(payload)
        return success({"summary": "ok"})

    result = DeepSeekClient(config(), transport=transport).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="enabled", reasoning_effort="high",
        max_tokens=4096,
    )
    assert observed["max_tokens"] == 4096
    assert result["metadata"]["max_tokens"] == 4096


def test_invalid_thinking_mode_is_rejected_before_network() -> None:
    with pytest.raises(ValueError, match="thinking must be"):
        DeepSeekClient(config(), transport=lambda *args: success({})).complete_json(
            system_prompt="Return JSON only.", user_prompt="Produce JSON.",
            required_keys={}, thinking="sometimes",
        )


def test_thinking_output_budget_doubles_after_length_truncation() -> None:
    observed = []
    responses = [
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}], "usage": {"total_tokens": 20}},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}], "usage": {"total_tokens": 30}},
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}], "usage": {"total_tokens": 40}},
        success({"summary": "ok"}),
    ]

    def transport(endpoint, headers, payload, timeout):
        observed.append(payload["max_tokens"])
        return responses.pop(0)

    result = DeepSeekClient(
        config(max_retries=3), transport=transport, sleeper=lambda _: None, random_source=lambda: 0,
    ).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="enabled", reasoning_effort="high",
    )
    assert observed == [16384, 32768, 65536, 65536]
    assert result["metadata"]["max_tokens"] == 65536
    assert result["metadata"]["usage"]["total_tokens"] == 100
    assert [item["usage"]["total_tokens"] for item in result["metadata"]["usage_by_request"]] == [20, 30, 40, 10]


def test_non_thinking_output_budget_doubles_after_length_truncation() -> None:
    observed = []
    responses = [
        {"choices": [{"message": {"content": "{}"}, "finish_reason": "length"}]},
        success({"summary": "ok"}),
    ]

    def transport(endpoint, headers, payload, timeout):
        observed.append(payload["max_tokens"])
        return responses.pop(0)

    result = DeepSeekClient(
        config(max_retries=1), transport=transport, sleeper=lambda _: None, random_source=lambda: 0,
    ).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="disabled", reasoning_effort=None,
        max_tokens=1024,
    )
    assert observed == [1024, 2048]
    assert result["metadata"]["max_tokens"] == 2048


def test_opencode_go_config_uses_provider_specific_defaults(monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_PROVIDER", "opencode-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "go-secret")
    monkeypatch.delenv("OPENCODE_BASE_URL", raising=False)
    monkeypatch.delenv("OPENCODE_MODEL", raising=False)
    value = DeepSeekConfig.from_env()
    assert value.provider == "opencode-go"
    assert value.base_url == "https://opencode.ai/zen/go/v1"
    assert value.endpoint == "https://opencode.ai/zen/go/v1/chat/completions"
    assert value.model == "mimo-v2.5"
    assert "go-secret" not in repr(value)


def test_route_five_client_fixes_model_to_mimo_v25(monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_PROVIDER", "opencode-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "go-secret")
    monkeypatch.setenv("OPENCODE_MODEL", "deepseek-v4-flash")
    client = opencode_mimo_v25_client()
    assert client.config.provider == "opencode-go"
    assert client.config.model == "mimo-v2.5"


def test_configurable_opencode_go_client_honors_selected_model(monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_PROVIDER", "opencode-go")
    monkeypatch.setenv("OPENCODE_API_KEY", "go-secret")
    monkeypatch.setenv("OPENCODE_MODEL", "deepseek-v4-flash")

    client = opencode_go_client()

    assert client.config.provider == "opencode-go"
    assert client.config.model == "deepseek-v4-flash"


def test_generic_openai_compatible_config_omits_vendor_thinking_fields(monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_PROVIDER", "openai-compatible")
    monkeypatch.setenv("ANALYZER_API_KEY", "vendor-secret")
    monkeypatch.setenv("ANALYZER_BASE_URL", "https://api.vendor.test/v1")
    monkeypatch.setenv("ANALYZER_MODEL", "vendor-model")
    monkeypatch.setenv("ANALYZER_THINKING_MODE", "auto")
    monkeypatch.setenv("ANALYZER_JSON_MODE", "false")
    observed = {}

    def transport(endpoint, headers, payload, timeout):
        observed.update(payload)
        return success({"summary": "ok"})

    client = configured_analysis_client()
    result = DeepSeekClient(client.config, transport=transport).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="enabled", reasoning_effort="high",
    )
    assert client.config.provider == "openai-compatible"
    assert "thinking" not in observed
    assert "reasoning_effort" not in observed
    assert "response_format" not in observed
    assert observed["temperature"] == 0.1
    assert result["metadata"]["thinking"] == "disabled"


def test_route_five_client_rejects_non_go_provider(monkeypatch) -> None:
    monkeypatch.setenv("ANALYZER_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    with pytest.raises(ValueError, match="route 5 requires"):
        opencode_mimo_v25_client()


def test_client_reports_opencode_go_provider() -> None:
    result = DeepSeekClient(
        config(provider="opencode-go"),
        transport=lambda *args: success({"summary": "ok"}),
    ).complete_json(
        system_prompt="Return JSON only.", user_prompt="Produce JSON.",
        required_keys={"summary": str}, thinking="disabled", reasoning_effort=None,
    )
    assert result["metadata"]["provider"] == "opencode-go"
