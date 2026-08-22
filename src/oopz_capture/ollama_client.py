from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


class OllamaError(RuntimeError):
    pass


class RetryableOllamaError(OllamaError):
    pass


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:8b"
    timeout_seconds: float = 900.0
    max_retries: int = 1
    keep_alive: str = "30m"
    context_tokens: int = 32768
    thinking_timeout_seconds: float = 600.0
    thinking_short_max_tokens: int = 0
    thinking_long_max_tokens: int = 0
    thinking_final_max_tokens: int = 0

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        base_url = os.environ.get("OLLAMA_BASE_URL", cls.base_url).strip().rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", cls.model).strip()
        timeout = float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", str(cls.timeout_seconds)))
        retries = int(os.environ.get("OLLAMA_MAX_RETRIES", str(cls.max_retries)))
        keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", cls.keep_alive).strip()
        context = int(os.environ.get("OLLAMA_CONTEXT_TOKENS", str(cls.context_tokens)))
        thinking_timeout = float(os.environ.get(
            "OLLAMA_THINKING_TIMEOUT_SECONDS", str(cls.thinking_timeout_seconds)
        ))
        thinking_short = int(os.environ.get(
            "OLLAMA_THINKING_SHORT_MAX_TOKENS", str(cls.thinking_short_max_tokens)
        ))
        thinking_long = int(os.environ.get(
            "OLLAMA_THINKING_LONG_MAX_TOKENS", str(cls.thinking_long_max_tokens)
        ))
        thinking_final = int(os.environ.get(
            "OLLAMA_THINKING_FINAL_MAX_TOKENS", str(cls.thinking_final_max_tokens)
        ))
        if base_url not in {"http://127.0.0.1:11434", "http://localhost:11434"}:
            raise ValueError("OLLAMA_BASE_URL must be the local Ollama endpoint on port 11434")
        if not model:
            raise ValueError("OLLAMA_MODEL must not be empty")
        if not 1 <= timeout <= 3600:
            raise ValueError("OLLAMA_TIMEOUT_SECONDS must be 1 to 3600")
        if not 0 <= retries <= 5:
            raise ValueError("OLLAMA_MAX_RETRIES must be 0 to 5")
        if not 4096 <= context <= 131072:
            raise ValueError("OLLAMA_CONTEXT_TOKENS must be 4096 to 131072")
        if not 5 <= thinking_timeout <= 600:
            raise ValueError("OLLAMA_THINKING_TIMEOUT_SECONDS must be 5 to 600")
        if thinking_short and not 256 <= thinking_short <= 32768:
            raise ValueError("OLLAMA_THINKING_SHORT_MAX_TOKENS must be 0 or 256 to 32768")
        if thinking_long and not 256 <= thinking_long <= 32768:
            raise ValueError("OLLAMA_THINKING_LONG_MAX_TOKENS must be 0 or 256 to 32768")
        if thinking_final and not 256 <= thinking_final <= 32768:
            raise ValueError("OLLAMA_THINKING_FINAL_MAX_TOKENS must be 0 or 256 to 32768")
        return cls(
            base_url, model, timeout, retries, keep_alive, context, thinking_timeout,
            thinking_short, thinking_long, thinking_final,
        )

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/api/chat"


class Transport(Protocol):
    def __call__(self, endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


def urllib_transport(endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        message = error.read().decode("utf-8", errors="replace")[:500]
        if error.code == 429 or 500 <= error.code < 600:
            raise RetryableOllamaError(f"Ollama HTTP {error.code}: {message}") from error
        raise OllamaError(f"Ollama HTTP {error.code}: {message}") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RetryableOllamaError(f"Ollama transport failure: {type(error).__name__}") from error


def _json_type(expected: type | tuple[type, ...]) -> dict[str, Any]:
    values = expected if isinstance(expected, tuple) else (expected,)
    types: list[str] = []
    for value in values:
        mapped = {str: "string", list: "array", dict: "object", int: "integer", float: "number", bool: "boolean"}.get(value)
        if mapped and mapped not in types:
            types.append(mapped)
    schema: dict[str, Any] = {"type": types[0] if len(types) == 1 else types or "string"}
    if str in values:
        schema["minLength"] = 1
    if list in values:
        # Callers use arrays of both strings and structured objects. Constraining
        # every array element to string conflicts with reconciliation prompts and
        # can cause Ollama to return empty or invalid JSON.
        schema["items"] = {}
    return schema


def _schema(required_keys: dict[str, type | tuple[type, ...]]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {key: _json_type(expected) for key, expected in required_keys.items()},
        "required": list(required_keys),
        "additionalProperties": False,
    }


def _validate(value: Any, required_keys: dict[str, type | tuple[type, ...]]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RetryableOllamaError("Ollama JSON content must be an object")
    missing = [key for key in required_keys if key not in value]
    if missing:
        raise RetryableOllamaError(f"Ollama JSON is missing required keys: {missing}")
    wrong = [key for key, expected in required_keys.items() if not isinstance(value[key], expected)]
    if wrong:
        raise RetryableOllamaError(f"Ollama JSON has invalid value types: {wrong}")
    empty = [
        key for key, expected in required_keys.items()
        if str in (expected if isinstance(expected, tuple) else (expected,))
        and not str(value[key]).strip()
    ]
    if empty:
        raise RetryableOllamaError(f"Ollama JSON has empty required strings: {empty}")
    return value


class OllamaClient:
    """Local structured-JSON client. This route never sends transcript text to a cloud API."""

    def __init__(
        self,
        config: OllamaConfig,
        *,
        transport: Transport = urllib_transport,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.transport = transport
        self.sleeper = sleeper
        self.clock = clock

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        required_keys: dict[str, type | tuple[type, ...]],
        thinking: str = "disabled",
        reasoning_effort: str | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        if thinking not in {"enabled", "disabled"}:
            raise ValueError("thinking must be enabled or disabled")
        if thinking == "enabled" and reasoning_effort not in {None, "low"}:
            raise ValueError("local Qwen thinking is limited to reasoning_effort=low")
        if thinking == "disabled" and reasoning_effort is not None:
            raise ValueError("non-thinking local Qwen may not set reasoning_effort")
        if max_tokens is not None and not 128 <= max_tokens <= 32768:
            raise ValueError("max_tokens must be 128 to 32768")
        schema = _schema(required_keys)
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        thinking_enabled = thinking == "enabled"
        local_system = system_prompt
        if thinking_enabled:
            local_system += (
                "\n允许进行简短内部思考，但必须优先在生成预算和时间限制内完成JSON答案；"
                "不要为了扩展推理而重复检查无关细节。"
            )
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": local_system + "\n严格遵守此JSON Schema：" + schema_text},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": schema,
            "think": thinking_enabled,
            "keep_alive": self.config.keep_alive,
            "options": {
                "temperature": 0.1,
                "num_ctx": self.config.context_tokens,
                "num_predict": max_tokens or 2048,
            },
        }
        request_timeout = min(self.config.timeout_seconds, self.config.thinking_timeout_seconds) if thinking_enabled else self.config.timeout_seconds
        deadline = self.clock() + request_timeout if thinking_enabled else None
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            requested_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            wall_started = time.perf_counter()
            try:
                effective_timeout = request_timeout
                if deadline is not None:
                    effective_timeout = deadline - self.clock()
                    if effective_timeout <= 0:
                        raise RetryableOllamaError("local thinking exceeded its total wall-time limit")
                response = self.transport(self.config.endpoint, payload, effective_timeout)
                message = response.get("message") if isinstance(response, dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, str) or not content.strip():
                    raise RetryableOllamaError("Ollama returned empty content")
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as error:
                    raise RetryableOllamaError("Ollama returned invalid JSON content") from error
                value = _validate(parsed, required_keys)
                prompt_tokens = int(response.get("prompt_eval_count", 0) or 0)
                completion_tokens = int(response.get("eval_count", 0) or 0)
                usage = {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": prompt_tokens,
                }
                if not thinking_enabled:
                    usage["completion_tokens_details"] = {"reasoning_tokens": 0}
                return {
                    "content": value,
                    "metadata": {
                        "provider": "ollama-local",
                        "api_called": False,
                        "model_requested": self.config.model,
                        "model_returned": str(response.get("model") or self.config.model),
                        "finish_reason": str(response.get("done_reason") or ""),
                        "usage": usage,
                        "requested_at": requested_at,
                        "usage_by_request": [{"requested_at": requested_at, "usage": usage}],
                        "thinking": thinking,
                        "reasoning_effort": "low" if thinking_enabled else None,
                        "reasoning_tokens_unreported": thinking_enabled,
                        "thinking_output_characters": len(str(message.get("thinking") or "")),
                        "max_tokens": payload["options"]["num_predict"],
                        "request_timeout_seconds": request_timeout,
                        "attempts": attempt + 1,
                        "performance": {
                            "wall_seconds": round(time.perf_counter() - wall_started, 6),
                            "ollama_total_seconds": round(int(response.get("total_duration", 0) or 0) / 1e9, 6),
                            "model_load_seconds": round(int(response.get("load_duration", 0) or 0) / 1e9, 6),
                            "prompt_eval_seconds": round(int(response.get("prompt_eval_duration", 0) or 0) / 1e9, 6),
                            "generation_seconds": round(int(response.get("eval_duration", 0) or 0) / 1e9, 6),
                        },
                    },
                }
            except RetryableOllamaError as error:
                last_error = error
                if attempt >= self.config.max_retries:
                    break
                delay = 1.0 + attempt
                if deadline is not None and self.clock() + delay >= deadline:
                    break
                self.sleeper(delay)
            except OllamaError:
                raise
        raise OllamaError(f"Ollama request failed after {self.config.max_retries + 1} attempts: {last_error}") from last_error
