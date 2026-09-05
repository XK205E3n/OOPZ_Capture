from __future__ import annotations

import json
import http.client
import os
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


class AnalysisAPIError(RuntimeError):
    """Failure from the configured analysis API, regardless of provider."""

    pass


class RetryableAnalysisAPIError(AnalysisAPIError):
    pass


# Backwards-compatible import aliases.  This module originally supported only
# DeepSeek, but it now also drives OpenCode Go and generic OpenAI-compatible
# APIs.  New user-visible errors must use the provider-neutral class name.
DeepSeekError = AnalysisAPIError
RetryableDeepSeekError = RetryableAnalysisAPIError


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str = field(repr=False)
    base_url: str
    model: str
    timeout_seconds: float = 60.0
    max_retries: int = 3
    min_interval_seconds: float = 0.5
    max_tokens: int = 2048
    thinking_max_tokens: int = 16384
    # ``deepseek`` preserves the original DeepSeek API path.  ``opencode-go``
    # uses the same OpenAI-compatible request format but a different key and
    # endpoint, so the pipeline can identify the billing source correctly.
    provider: str = "deepseek"
    # Generic OpenAI-compatible endpoints commonly reject vendor-specific
    # ``thinking`` fields. ``auto`` preserves them only for the official
    # DeepSeek API; OpenCode Go is documented as OpenAI-compatible and must
    # therefore use normal chat-completions fields unless explicitly enabled.
    thinking_mode: str = "auto"
    json_mode: bool = True

    @classmethod
    def from_env(cls) -> "DeepSeekConfig":
        required_names = (
            "ANALYZER_PROVIDER",
            "ANALYZER_API_KEY",
            "ANALYZER_BASE_URL",
            "ANALYZER_MODEL",
            "ANALYZER_TIMEOUT_SECONDS",
            "ANALYZER_MAX_RETRIES",
            "ANALYZER_MIN_INTERVAL_SECONDS",
            "ANALYZER_MAX_TOKENS",
            "ANALYZER_THINKING_MAX_TOKENS",
            "ANALYZER_THINKING_MODE",
            "ANALYZER_JSON_MODE",
        )
        values = {name: os.environ.get(name, "").strip() for name in required_names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError("required analyzer settings are missing: " + ", ".join(missing))

        provider = values["ANALYZER_PROVIDER"].lower()
        if provider not in {"deepseek", "opencode-go", "openai-compatible"}:
            raise ValueError("ANALYZER_PROVIDER must be deepseek, opencode-go, or openai-compatible")
        api_key = values["ANALYZER_API_KEY"]
        base_url = values["ANALYZER_BASE_URL"]
        model = values["ANALYZER_MODEL"]
        base_url = base_url.rstrip("/")
        if not base_url.startswith("https://"):
            raise ValueError("ANALYZER_BASE_URL must use HTTPS")
        timeout = float(values["ANALYZER_TIMEOUT_SECONDS"])
        retries = int(values["ANALYZER_MAX_RETRIES"])
        interval = float(values["ANALYZER_MIN_INTERVAL_SECONDS"])
        max_tokens = int(values["ANALYZER_MAX_TOKENS"])
        thinking_max_tokens = int(values["ANALYZER_THINKING_MAX_TOKENS"])
        thinking_mode = values["ANALYZER_THINKING_MODE"].lower()
        json_mode_raw = values["ANALYZER_JSON_MODE"].lower()
        if thinking_mode not in {"auto", "enabled", "disabled"}:
            raise ValueError("ANALYZER_THINKING_MODE must be auto, enabled, or disabled")
        if json_mode_raw not in {"true", "false"}:
            raise ValueError("ANALYZER_JSON_MODE must be true or false")
        if not 1 <= timeout <= 600:
            raise ValueError("ANALYZER_TIMEOUT_SECONDS must be 1 to 600")
        if not 0 <= retries <= 8:
            raise ValueError("ANALYZER_MAX_RETRIES must be 0 to 8")
        if not 0 <= interval <= 60:
            raise ValueError("ANALYZER_MIN_INTERVAL_SECONDS must be 0 to 60")
        if not 128 <= max_tokens <= 384000:
            raise ValueError("ANALYZER_MAX_TOKENS must be 128 to 384000")
        if not 128 <= thinking_max_tokens <= 384000:
            raise ValueError("ANALYZER_THINKING_MAX_TOKENS must be 128 to 384000")
        return cls(
            api_key=api_key, base_url=base_url, model=model,
            timeout_seconds=timeout, max_retries=retries, min_interval_seconds=interval,
            max_tokens=max_tokens, thinking_max_tokens=thinking_max_tokens,
            provider=provider, thinking_mode=thinking_mode, json_mode=json_mode_raw == "true",
        )

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"


MIMO_V25_MODEL = "mimo-v2.5"


def opencode_go_client() -> "DeepSeekClient":
    """Build the configurable OpenCode Go client used by the analysis pipeline."""
    config = DeepSeekConfig.from_env()
    if config.provider != "opencode-go":
        raise ValueError(
            "the fixed mimo-go route requires ANALYZER_PROVIDER=opencode-go and an OpenCode Go API key"
        )
    return DeepSeekClient(config)


def configured_analysis_client() -> "DeepSeekClient":
    """Build the configured OpenAI-compatible analysis client."""
    return DeepSeekClient(DeepSeekConfig.from_env())


def opencode_mimo_v25_client() -> "DeepSeekClient":
    """Build the fixed OpenCode Go MiMo-V2.5 client used by the explicit CLI route.

    This compatibility helper preserves the explicit CLI ``mimo-go`` route.
    """
    return DeepSeekClient(replace(opencode_go_client().config, model=MIMO_V25_MODEL))


class Transport(Protocol):
    def __call__(self, endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


def urllib_transport(endpoint: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 429 or 500 <= error.code < 600:
            raise RetryableAnalysisAPIError(f"analysis API HTTP {error.code}") from error
        raise AnalysisAPIError(f"analysis API HTTP {error.code}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, http.client.HTTPException) as error:
        raise RetryableAnalysisAPIError(f"analysis API transport failure: {type(error).__name__}") from error


class RateLimiter:
    def __init__(self, min_interval_seconds: float, *, clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep):
        self.min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._last_request_at: float | None = None

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                remaining = self.min_interval_seconds - (now - self._last_request_at)
                if remaining > 0:
                    self._sleeper(remaining)
                    now = self._clock()
            self._last_request_at = now


def _validate_required_keys(value: dict[str, Any], required_keys: dict[str, type | tuple[type, ...]]) -> None:
    missing = [key for key in required_keys if key not in value]
    if missing:
        raise RetryableAnalysisAPIError(f"model JSON is missing required keys: {missing}")
    wrong = [key for key, expected in required_keys.items() if not isinstance(value[key], expected)]
    if wrong:
        raise RetryableAnalysisAPIError(f"model JSON has invalid value types: {wrong}")


def _merge_usage(total: dict[str, Any], current: Any) -> None:
    if not isinstance(current, dict):
        return
    for key in (
        "prompt_tokens", "completion_tokens", "total_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    ):
        if key in current:
            total[key] = int(total.get(key, 0)) + int(current.get(key, 0) or 0)
    for details_key, token_keys in (
        ("prompt_tokens_details", ("cached_tokens",)),
        ("completion_tokens_details", ("reasoning_tokens",)),
    ):
        current_details = current.get(details_key)
        if not isinstance(current_details, dict):
            continue
        total_details = total.setdefault(details_key, {})
        for key in token_keys:
            if key in current_details:
                total_details[key] = int(total_details.get(key, 0)) + int(current_details.get(key, 0) or 0)


class DeepSeekClient:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        transport: Transport = urllib_transport,
        sleeper: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        limiter: RateLimiter | None = None,
    ):
        self.config = config
        self.transport = transport
        self.sleeper = sleeper
        self.random_source = random_source
        self.limiter = limiter or RateLimiter(config.min_interval_seconds, sleeper=sleeper)

    def analysis_profile(self) -> dict[str, Any]:
        return {
            "client": type(self).__name__,
            "provider": self.config.provider,
            "model": self.config.model,
            "base_url": self.config.base_url,
        }

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        required_keys: dict[str, type | tuple[type, ...]],
        thinking: str = "enabled",
        reasoning_effort: str | None = "high",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if "json" not in (system_prompt + user_prompt).lower():
            raise ValueError("analysis API JSON mode requires the prompt to explicitly mention JSON")
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking must be enabled or disabled")
        if thinking == "enabled" and reasoning_effort not in {"high", "max"}:
            raise ValueError("thinking mode requires reasoning_effort high or max")
        if max_tokens is not None and not 128 <= max_tokens <= 384000:
            raise ValueError("max_tokens must be 128 to 384000")
        supports_thinking = self.config.thinking_mode == "enabled" or (
            self.config.thinking_mode == "auto" and self.config.provider == "deepseek"
        )
        effective_thinking = thinking if supports_thinking else "disabled"
        initial_max_tokens = max_tokens or (
            self.config.thinking_max_tokens if effective_thinking == "enabled" else self.config.max_tokens
        )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "max_tokens": initial_max_tokens,
        }
        if self.config.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if supports_thinking:
            payload["thinking"] = {"type": thinking}
        if effective_thinking == "disabled":
            payload["temperature"] = 0.1
        else:
            payload["reasoning_effort"] = reasoning_effort
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "oopz-analyzer/0.1",
        }
        last_error: Exception | None = None
        accumulated_usage: dict[str, Any] = {}
        usage_by_request: list[dict[str, Any]] = []
        last_requested_at = ""
        for attempt in range(self.config.max_retries + 1):
            try:
                self.limiter.wait()
                last_requested_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                response = self.transport(self.config.endpoint, headers, payload, self.config.timeout_seconds)
                response_usage = response.get("usage")
                _merge_usage(accumulated_usage, response_usage)
                if isinstance(response_usage, dict):
                    usage_by_request.append({
                        "requested_at": last_requested_at,
                        "usage": response_usage,
                    })
                choices = response.get("choices")
                if not isinstance(choices, list) or not choices:
                    raise RetryableAnalysisAPIError("analysis API response has no choices")
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                finish_reason = str(choices[0].get("finish_reason") or "")
                if finish_reason == "length":
                    if effective_thinking == "enabled":
                        retry_cap = min(384000, max(65536, self.config.thinking_max_tokens))
                    else:
                        retry_cap = min(384000, max(8192, self.config.max_tokens))
                    payload["max_tokens"] = min(retry_cap, int(payload["max_tokens"]) * 2)
                    raise RetryableAnalysisAPIError("analysis API JSON output was truncated at max_tokens")
                if not isinstance(content, str) or not content.strip():
                    raise RetryableAnalysisAPIError("analysis API returned empty content")
                try:
                    value = json.loads(content)
                except json.JSONDecodeError as error:
                    raise RetryableAnalysisAPIError("analysis API returned invalid JSON content") from error
                if not isinstance(value, dict):
                    raise RetryableAnalysisAPIError("analysis API JSON content must be an object")
                _validate_required_keys(value, required_keys)
                return {
                    "content": value,
                    "metadata": {
                        "provider": self.config.provider,
                        "model_requested": self.config.model,
                        "model_returned": str(response.get("model") or ""),
                        "response_id": str(response.get("id") or ""),
                        "finish_reason": finish_reason,
                        "usage": accumulated_usage,
                        "requested_at": last_requested_at,
                        "usage_by_request": usage_by_request,
                        "thinking": effective_thinking,
                        "reasoning_effort": reasoning_effort if effective_thinking == "enabled" else None,
                        "max_tokens": payload["max_tokens"],
                        "attempts": attempt + 1,
                    },
                }
            except RetryableAnalysisAPIError as error:
                last_error = error
                if attempt >= self.config.max_retries:
                    break
                delay = min(30.0, (2 ** attempt) + self.random_source())
                self.sleeper(delay)
            except AnalysisAPIError:
                raise
        raise AnalysisAPIError(
            "analysis API request "
            f"({self.config.provider}/{self.config.model}) failed after "
            f"{self.config.max_retries + 1} attempts: {last_error}"
        ) from last_error


class MockDeepSeekClient:
    """Offline deterministic adapter for tests and pipeline development; never uses network."""

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        required_keys: dict[str, type | tuple[type, ...]],
        thinking: str = "enabled",
        reasoning_effort: str | None = "high",
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        del system_prompt, user_prompt, max_tokens
        value: dict[str, Any] = {}
        for key, expected in required_keys.items():
            types = expected if isinstance(expected, tuple) else (expected,)
            selected = types[0]
            if selected is str:
                value[key] = f"mock-{key}"
            elif selected is list:
                value[key] = []
            elif selected is dict:
                value[key] = {}
            elif selected is bool:
                value[key] = False
            elif selected is int:
                value[key] = 0
            else:
                raise ValueError(f"MockDeepSeekClient cannot synthesize {key}: {selected}")
        return {
            "content": value,
            "metadata": {
                "provider": "mock",
                "model_requested": "mock-deepseek",
                "model_returned": "mock-deepseek",
                "response_id": "mock-response",
                "finish_reason": "stop",
                "usage": {},
                "thinking": thinking,
                "reasoning_effort": reasoning_effort if thinking == "enabled" else None,
                "attempts": 1,
            },
        }
