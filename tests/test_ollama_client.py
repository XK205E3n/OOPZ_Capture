from __future__ import annotations

import pytest

from oopz_capture.ollama_client import OllamaClient, OllamaConfig, OllamaError, RetryableOllamaError


def test_relaxed_thinking_limits_are_opt_in(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_THINKING_TIMEOUT_SECONDS", "180")
    monkeypatch.setenv("OLLAMA_THINKING_SHORT_MAX_TOKENS", "4096")
    monkeypatch.setenv("OLLAMA_THINKING_LONG_MAX_TOKENS", "8192")
    monkeypatch.setenv("OLLAMA_THINKING_FINAL_MAX_TOKENS", "12288")
    config = OllamaConfig.from_env()
    assert config.thinking_timeout_seconds == 180
    assert config.thinking_short_max_tokens == 4096
    assert config.thinking_long_max_tokens == 8192
    assert config.thinking_final_max_tokens == 12288


def test_ollama_client_uses_schema_nonthinking_and_reports_local_tokens() -> None:
    captured = {}

    def transport(endpoint, payload, timeout):
        captured.update(endpoint=endpoint, payload=payload, timeout=timeout)
        return {
            "model": "qwen3:8b",
            "message": {"content": '{"summary":"测试","items":[]}'},
            "done_reason": "stop",
            "prompt_eval_count": 100,
            "eval_count": 20,
            "total_duration": 2_000_000_000,
            "load_duration": 500_000_000,
            "prompt_eval_duration": 800_000_000,
            "eval_duration": 700_000_000,
        }

    client = OllamaClient(OllamaConfig(), transport=transport)
    response = client.complete_json(
        system_prompt="只输出JSON。",
        user_prompt="测试",
        required_keys={"summary": str, "items": list},
        thinking="disabled",
        reasoning_effort=None,
        max_tokens=256,
    )

    assert captured["endpoint"] == "http://127.0.0.1:11434/api/chat"
    assert captured["payload"]["think"] is False
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["format"]["required"] == ["summary", "items"]
    assert captured["payload"]["format"]["properties"]["summary"]["minLength"] == 1
    assert captured["payload"]["format"]["properties"]["items"]["items"] == {}
    assert captured["payload"]["options"]["num_predict"] == 256
    assert response["metadata"]["provider"] == "ollama-local"
    assert response["metadata"]["api_called"] is False
    assert response["metadata"]["usage"]["total_tokens"] == 120
    assert response["metadata"]["performance"]["ollama_total_seconds"] == 2.0


def test_ollama_client_bounds_thinking_and_does_not_claim_reasoning_token_split() -> None:
    captured = {}

    def transport(endpoint, payload, timeout):
        captured.update(endpoint=endpoint, payload=payload, timeout=timeout)
        return {
            "model": "qwen3:8b",
            "message": {"thinking": "简短思考", "content": '{"summary":"测试","items":[]}'},
            "done_reason": "stop",
            "prompt_eval_count": 50,
            "eval_count": 30,
        }

    config = OllamaConfig(timeout_seconds=600, thinking_timeout_seconds=30)
    response = OllamaClient(config, transport=transport).complete_json(
        system_prompt="只输出JSON。",
        user_prompt="测试",
        required_keys={"summary": str, "items": list},
        thinking="enabled",
        reasoning_effort="low",
        max_tokens=1024,
    )

    assert captured["payload"]["think"] is True
    assert captured["payload"]["options"]["num_predict"] == 1024
    assert captured["timeout"] == 30
    assert response["metadata"]["thinking"] == "enabled"
    assert response["metadata"]["reasoning_tokens_unreported"] is True
    assert response["metadata"]["thinking_output_characters"] == 4
    assert "completion_tokens_details" not in response["metadata"]["usage"]


def test_ollama_client_retries_empty_required_string() -> None:
    calls = 0

    def transport(endpoint, payload, timeout):
        nonlocal calls
        calls += 1
        summary = "" if calls == 1 else "有效摘要"
        return {
            "model": "deepseek-r1:8b",
            "message": {"thinking": "简短思考", "content": '{"summary":"' + summary + '","items":[]}'},
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 10,
        }

    response = OllamaClient(OllamaConfig(max_retries=1), transport=transport, sleeper=lambda value: None).complete_json(
        system_prompt="只输出JSON。",
        user_prompt="测试",
        required_keys={"summary": str, "items": list},
        thinking="enabled",
        reasoning_effort="low",
        max_tokens=1024,
    )

    assert calls == 2
    assert response["content"]["summary"] == "有效摘要"
    assert response["metadata"]["attempts"] == 2


def test_thinking_timeout_is_total_across_retries() -> None:
    now = [0.0]
    timeouts = []

    def clock():
        return now[0]

    def sleeper(value):
        now[0] += value

    def transport(endpoint, payload, timeout):
        timeouts.append(timeout)
        now[0] += 20 if len(timeouts) == 1 else timeout
        raise RetryableOllamaError("planned timeout")

    client = OllamaClient(
        OllamaConfig(max_retries=1, thinking_timeout_seconds=30),
        transport=transport,
        sleeper=sleeper,
        clock=clock,
    )
    with pytest.raises(OllamaError, match="planned timeout"):
        client.complete_json(
            system_prompt="只输出JSON。",
            user_prompt="测试",
            required_keys={"summary": str},
            thinking="enabled",
            reasoning_effort="low",
            max_tokens=1024,
        )

    assert timeouts == [30.0, 9.0]
