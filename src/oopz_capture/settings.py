"""Admin-settable runtime settings persisted to the gitignored project .env.

Values are applied to ``os.environ`` immediately (so the running controller
picks them up on the next capture/analysis) and written back to the project
``.env`` file so a later restart keeps them.  Only a fixed whitelist of keys
may be changed through the Feishu control surface; secrets are never echoed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"

_PHONE_RE = re.compile(r"^\d{5,20}$")
_CUTOFF_RE = re.compile(r"^(?:([01]?\d|2[0-3])(?::00)?)$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$", re.IGNORECASE)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDERS = frozenset({"deepseek", "opencode-go", "openai-compatible"})
_THINKING_MODES = frozenset({"auto", "enabled", "disabled"})


def _https_url(value: str) -> bool:
    parsed = urlparse(value)
    return len(value) <= 512 and parsed.scheme == "https" and bool(parsed.netloc)


def _number_between(value: str, minimum: float, maximum: float) -> bool:
    try:
        number = float(value)
    except ValueError:
        return False
    return minimum <= number <= maximum


def _integer_between(value: str, minimum: int, maximum: int) -> bool:
    return value.isdigit() and minimum <= int(value) <= maximum

SETTABLE_KEYS: dict[str, dict[str, object]] = {
    "OOPZ_LOGIN_PHONE": {
        "validator": lambda value: bool(_PHONE_RE.fullmatch(value)),
        "description": "OOPZ 登录手机号（5-20 位数字）",
        "secret": False,
    },
    "OOPZ_LOGIN_PASSWORD": {
        "validator": lambda value: 1 <= len(value) <= 200 and "\n" not in value,
        "description": "OOPZ 登录密码",
        "secret": True,
    },
    "OOPZ_CUTOFF_LOCAL_HOUR": {
        "validator": lambda value: value.isdigit() and 0 <= int(value) <= 23,
        "description": "北京时间强制结束小时 0-23",
        "secret": False,
    },
    "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS": {
        "validator": lambda value: value.isdigit() and 5 <= int(value) <= 3600,
        "description": "频道无人自动退出秒数 5-3600，可输入 5m/1h",
        "secret": False,
    },
    "OOPZ_CHUNK_SECONDS": {
        "validator": lambda value: value.isdigit() and 30 <= int(value) <= 300,
        "description": "录音分片秒数 30-300",
        "secret": False,
    },
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS": {
        "validator": lambda value: value.isdigit() and 0 <= int(value) <= 3,
        "description": "失败转写分片重试轮数 0-3",
        "secret": False,
    },
    "OOPZ_PROCESSING_DEADLINE_SECONDS": {
        "validator": lambda value: _integer_between(value, 60, 3600),
        "description": "录音结束后处理期限秒数 60-3600",
        "secret": False,
    },
    "OOPZ_POLL_INTERVAL_SECONDS": {
        "validator": lambda value: _number_between(value, 0.05, 5),
        "description": "录音状态轮询秒数 0.05-5",
        "secret": False,
    },
    "OOPZ_MEMBERSHIP_REFRESH_SECONDS": {
        "validator": lambda value: _number_between(value, 5, 600),
        "description": "频道成员刷新秒数 5-600",
        "secret": False,
    },
    "OOPZ_MEMBERSHIP_TIMEOUT_SECONDS": {
        "validator": lambda value: _number_between(value, 1, 60),
        "description": "成员查询超时秒数 1-60",
        "secret": False,
    },
    "OOPZ_CONNECTION_CHECK_SECONDS": {
        "validator": lambda value: _number_between(value, 0.5, 30),
        "description": "连接检查秒数 0.5-30",
        "secret": False,
    },
    "OOPZ_DISCONNECT_GRACE_SECONDS": {
        "validator": lambda value: _number_between(value, 3, 120),
        "description": "断线宽限秒数 3-120",
        "secret": False,
    },
    "OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS": {
        "validator": lambda value: _number_between(value, 0.5, 15),
        "description": "浏览器操作超时秒数 0.5-15",
        "secret": False,
    },
    "OOPZ_RECONNECT_WINDOW_SECONDS": {
        "validator": lambda value: _number_between(value, 30, 3600),
        "description": "自动重连窗口秒数 30-3600",
        "secret": False,
    },
    "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS": {
        "validator": lambda value: _number_between(value, 0.25, 60),
        "description": "首次重连等待秒数 0.25-60",
        "secret": False,
    },
    "OOPZ_RECONNECT_MAX_DELAY_SECONDS": {
        "validator": lambda value: _number_between(value, 1, 300),
        "description": "重连最大等待秒数 1-300",
        "secret": False,
    },
    "OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS": {
        "validator": lambda value: _number_between(value, 5, 120),
        "description": "单次重连超时秒数 5-120",
        "secret": False,
    },
    "OOPZ_LANGUAGE": {
        "validator": lambda value: value in {"auto", "zh"},
        "description": "转写语言 auto / zh",
        "secret": False,
    },
    "OOPZ_RETAIN_AUDIO": {
        "validator": lambda value: value in {"true", "false"},
        "description": "转写后保留音频 true / false",
        "secret": False,
    },
    "OOPZ_RETENTION_HOURS": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 360,
        "description": "文本和报告保留小时数 1-360",
        "secret": False,
    },
    "OOPZ_DEVICE": {
        "validator": lambda value: value in {"cpu", "cuda:0"},
        "description": "转写设备 cpu / cuda:0",
        "secret": False,
    },
    "OOPZ_ANALYSIS_MAX_PARALLELISM": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 8,
        "description": "分析并行任务数 1-8",
        "secret": False,
    },
    "ANALYZER_PROVIDER": {
        "validator": lambda value: value in _PROVIDERS,
        "description": "分析供应商 deepseek / opencode-go / openai-compatible",
        "secret": False,
    },
    "ANALYZER_API_KEY": {
        "validator": lambda value: 8 <= len(value) <= 512 and "\n" not in value and "\r" not in value,
        "description": "分析 API Key（Bearer Token）",
        "secret": True,
    },
    "ANALYZER_BASE_URL": {
        "validator": _https_url,
        "description": "分析 API HTTPS 地址",
        "secret": False,
    },
    "ANALYZER_MODEL": {
        "validator": lambda value: bool(_MODEL_RE.fullmatch(value)),
        "description": "分析模型 ID",
        "secret": False,
    },
    "ANALYZER_TIMEOUT_SECONDS": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 600,
        "description": "API 超时秒数 1-600",
        "secret": False,
    },
    "ANALYZER_MAX_RETRIES": {
        "validator": lambda value: value.isdigit() and 0 <= int(value) <= 8,
        "description": "API 失败重试次数 0-8",
        "secret": False,
    },
    "ANALYZER_MIN_INTERVAL_SECONDS": {
        "validator": lambda value: bool(re.fullmatch(r"\d+(?:\.\d+)?", value)) and 0 <= float(value) <= 60,
        "description": "API 请求最小间隔秒数 0-60",
        "secret": False,
    },
    "ANALYZER_MAX_TOKENS": {
        "validator": lambda value: value.isdigit() and 128 <= int(value) <= 384000,
        "description": "普通请求最大输出 Token 128-384000",
        "secret": False,
    },
    "ANALYZER_THINKING_MAX_TOKENS": {
        "validator": lambda value: value.isdigit() and 128 <= int(value) <= 384000,
        "description": "思考请求最大输出 Token 128-384000",
        "secret": False,
    },
    "ANALYZER_THINKING_MODE": {
        "validator": lambda value: value in _THINKING_MODES,
        "description": "思考模式 auto / enabled / disabled",
        "secret": False,
    },
    "ANALYZER_JSON_MODE": {
        "validator": lambda value: value in {"true", "false"},
        "description": "JSON 模式 true / false",
        "secret": False,
    },
}

# Defaults used by the real Feishu gateway, controller, and SDK-backed login
# flow. ANALYZER_* settings are intentionally absent: users must explicitly
# configure every analysis API field.
SETTING_DEFAULTS: dict[str, str] = {
    "OOPZ_CUTOFF_LOCAL_HOUR": "4",
    "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS": "300",
    "OOPZ_CHUNK_SECONDS": "300",
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS": "1",
    "OOPZ_PROCESSING_DEADLINE_SECONDS": "900",
    "OOPZ_POLL_INTERVAL_SECONDS": "0.25",
    "OOPZ_MEMBERSHIP_REFRESH_SECONDS": "30",
    "OOPZ_MEMBERSHIP_TIMEOUT_SECONDS": "10",
    "OOPZ_CONNECTION_CHECK_SECONDS": "2",
    "OOPZ_DISCONNECT_GRACE_SECONDS": "15",
    "OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS": "2",
    "OOPZ_RECONNECT_WINDOW_SECONDS": "300",
    "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS": "1",
    "OOPZ_RECONNECT_MAX_DELAY_SECONDS": "30",
    "OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS": "30",
    "OOPZ_LANGUAGE": "auto",
    "OOPZ_RETAIN_AUDIO": "false",
    "OOPZ_RETENTION_HOURS": "360",
    "OOPZ_DEVICE": "cpu",
    "OOPZ_ANALYSIS_MAX_PARALLELISM": "4",
}

KEY_ORDER = tuple(SETTABLE_KEYS)
SETTING_ALIASES = {
    "强制结束时间": "OOPZ_CUTOFF_LOCAL_HOUR",
    "结束时间": "OOPZ_CUTOFF_LOCAL_HOUR",
    "无人退出时间": "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS",
    "无人结束退出时间": "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS",
    "分片时长": "OOPZ_CHUNK_SECONDS",
    "转写修复次数": "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS",
    "转写语言": "OOPZ_LANGUAGE",
    "保留音频": "OOPZ_RETAIN_AUDIO",
    "文本保留小时": "OOPZ_RETENTION_HOURS",
    "转写设备": "OOPZ_DEVICE",
    "分析并行数": "OOPZ_ANALYSIS_MAX_PARALLELISM",
    "分析供应商": "ANALYZER_PROVIDER",
    "分析api密钥": "ANALYZER_API_KEY",
    "分析api地址": "ANALYZER_BASE_URL",
    "分析模型": "ANALYZER_MODEL",
    "分析超时秒": "ANALYZER_TIMEOUT_SECONDS",
    "分析重试次数": "ANALYZER_MAX_RETRIES",
    "分析最小间隔秒": "ANALYZER_MIN_INTERVAL_SECONDS",
    "分析最大token": "ANALYZER_MAX_TOKENS",
    "分析思考最大token": "ANALYZER_THINKING_MAX_TOKENS",
    "分析思考模式": "ANALYZER_THINKING_MODE",
    "分析json模式": "ANALYZER_JSON_MODE",
}


def canonical_setting_key(key: str) -> str:
    raw = str(key or "").strip()
    return SETTING_ALIASES.get(raw.casefold(), raw.upper())


def _normalize_value(key: str, value: str) -> str:
    if key == "OOPZ_CUTOFF_LOCAL_HOUR":
        match = _CUTOFF_RE.fullmatch(value)
        if match is None:
            return value
        return str(int(match.group(1)))
    if key == "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS":
        match = _DURATION_RE.fullmatch(value)
        if match is None:
            return value
        amount = float(match.group(1))
        unit = match.group(2).casefold() or "s"
        seconds = amount * {"s": 1, "m": 60, "h": 3600}[unit]
        if not seconds.is_integer():
            return value
        return str(int(seconds))
    return value


def masked_value(key: str, value: str) -> str:
    meta = SETTABLE_KEYS.get(key)
    if meta is None:
        return value
    if meta["secret"]:
        return f"已设置（长度 {len(value)}）"
    if key == "OOPZ_LOGIN_PHONE" and len(value) > 4:
        return value[:3] + "*" * (len(value) - 4) + value[-1:]
    return value


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _load_env(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8-sig").splitlines()


def _env_file_values(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs (quotes stripped), ignoring blanks and comments."""
    loaded: dict[str, str] = {}
    for line in _load_env(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        loaded[key.strip()] = value.strip().strip("\"'")
    return loaded


def _write_env_line(path: Path, key: str, value: str) -> None:
    """Replace the single KEY= line, or append it when absent."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    output: list[str] = []
    replaced = False
    for line in _load_env(path):
        if pattern.match(line):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    _atomic_write(path, "\n".join(output) + "\n")


def apply_setting(key: str, value: str, *, env_path: Path | None = None) -> str:
    """Validate and persist one setting; returns a masked display value."""
    key = canonical_setting_key(key)
    if key not in SETTABLE_KEYS:
        raise ValueError("不支持的变量名；可用：" + "、".join(KEY_ORDER))
    value = _normalize_value(key, str(value or "").strip())
    if not SETTABLE_KEYS[key]["validator"](value):
        raise ValueError(f"变量 {key} 的值无效；要求：{SETTABLE_KEYS[key]['description']}")
    path = env_path or _DEFAULT_ENV_PATH
    loaded = _env_file_values(path)
    if key in {"OOPZ_RECONNECT_INITIAL_DELAY_SECONDS", "OOPZ_RECONNECT_MAX_DELAY_SECONDS"}:
        initial = float(value) if key == "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS" else float(
            os.environ.get("OOPZ_RECONNECT_INITIAL_DELAY_SECONDS") or loaded.get("OOPZ_RECONNECT_INITIAL_DELAY_SECONDS") or "1"
        )
        maximum = float(value) if key == "OOPZ_RECONNECT_MAX_DELAY_SECONDS" else float(
            os.environ.get("OOPZ_RECONNECT_MAX_DELAY_SECONDS") or loaded.get("OOPZ_RECONNECT_MAX_DELAY_SECONDS") or "30"
        )
        if maximum < initial:
            raise ValueError("OOPZ_RECONNECT_MAX_DELAY_SECONDS 不能小于 OOPZ_RECONNECT_INITIAL_DELAY_SECONDS")
    os.environ[key] = value
    _write_env_line(path, key, value)
    return masked_value(key, value)


def setting_status(env_path: Path | None = None) -> dict[str, str]:
    """Report configured values, substituting effective runtime defaults."""
    path = env_path or _DEFAULT_ENV_PATH
    loaded = _env_file_values(path)
    result: dict[str, str] = {}
    for key in KEY_ORDER:
        value = os.environ.get(key) or loaded.get(key) or ""
        if value:
            result[key] = masked_value(key, value)
        elif key in SETTING_DEFAULTS:
            result[key] = SETTING_DEFAULTS[key]
        else:
            result[key] = "未设置"
    return result


def setting_description(key: str, *, env_path: Path | None = None) -> str:
    """Return a concise description including the effective default, if any."""
    description = str(SETTABLE_KEYS[key]["description"])
    default = SETTING_DEFAULTS.get(key)
    return f"{description}；默认 {default}" if default is not None else description


def setting_is_configured(key: str, *, env_path: Path | None = None) -> bool:
    """Return whether a key has a non-empty process or .env value."""
    if str(os.environ.get(key) or "").strip():
        return True
    return bool(_env_file_values(env_path or _DEFAULT_ENV_PATH).get(key))

def upsert_env(key: str, value: str, *, env_path: Path | None = None) -> None:
    """Write or update one KEY=VALUE line in the project .env (no whitelist)."""
    key = str(key or "").strip().upper()
    value = str(value or "").strip()
    os.environ[key] = value
    _write_env_line(env_path or _DEFAULT_ENV_PATH, key, value)
