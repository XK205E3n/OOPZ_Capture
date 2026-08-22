"""Admin-settable runtime settings persisted to the gitignored project .env.

Values are applied to ``os.environ`` immediately (so the running controller
picks them up on the next capture/analysis) and written back to the project
``.env`` file so a later restart keeps them.  Only a fixed whitelist of keys
may be changed through QQ; secrets are never echoed in replies.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"

_PHONE_RE = re.compile(r"^\d{5,20}$")
_METHODS = frozenset({"auto", "credentials", "password"})
_CUTOFF_RE = re.compile(r"^(?:([01]?\d|2[0-3])(?::00)?)$")
_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$", re.IGNORECASE)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDERS = frozenset({"deepseek", "opencode-go", "openai-compatible"})
_THINKING_MODES = frozenset({"auto", "enabled", "disabled"})


def _https_url(value: str) -> bool:
    parsed = urlparse(value)
    return len(value) <= 512 and parsed.scheme == "https" and bool(parsed.netloc)

SETTABLE_KEYS: dict[str, dict[str, object]] = {
    "OOPZ_LOGIN_METHOD": {
        "validator": lambda value: value in _METHODS,
        "description": "auto / credentials / password",
        "secret": False,
    },
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
        "description": "北京时间强制结束小时（0-23，或 HH:00）",
        "secret": False,
    },
    "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS": {
        "validator": lambda value: value.isdigit() and 5 <= int(value) <= 3600,
        "description": "频道无人时自动退出秒数（5-3600；可输入 5m、300s 或 1h）",
        "secret": False,
    },
    "OOPZ_CHUNK_SECONDS": {
        "validator": lambda value: value.isdigit() and 30 <= int(value) <= 300,
        "description": "单个录音分片秒数（30-300；最长5分钟）",
        "secret": False,
    },
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS": {
        "validator": lambda value: value.isdigit() and 0 <= int(value) <= 3,
        "description": "录音结束后自动重试失败转写分片的轮数（0-3）",
        "secret": False,
    },
    "OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS": {
        "validator": lambda value: value.isdigit() and 30 <= int(value) <= 1800,
        "description": "报告选择和转发等待回复秒数（30-1800）",
        "secret": False,
    },
    "OOPZ_LANGUAGE": {
        "validator": lambda value: value in {"auto", "zh"},
        "description": "默认转写语言：auto / zh",
        "secret": False,
    },
    "OOPZ_RETAIN_AUDIO": {
        "validator": lambda value: value in {"true", "false"},
        "description": "转写完成后保留音频：true / false",
        "secret": False,
    },
    "OOPZ_RETENTION_HOURS": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 168,
        "description": "文本和报告保留小时数（1-168）",
        "secret": False,
    },
    "OOPZ_DEVICE": {
        "validator": lambda value: value in {"cpu", "cuda:0"},
        "description": "录音/转写设备：cpu / cuda:0",
        "secret": False,
    },
    "OOPZ_ANALYSIS_MAX_PARALLELISM": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 8,
        "description": "分析 API 并行任务数（1-8）",
        "secret": False,
    },
    "OOPZ_ONEBOT_SEND_FAILURE_COOLDOWN_SECONDS": {
        "validator": lambda value: value.isdigit() and 5 <= int(value) <= 300,
        "description": "QQ 发送失败后的冷却秒数（5-300）",
        "secret": False,
    },
    "OOPZ_QQ_WATCHDOG_POLL_SECONDS": {
        "validator": lambda value: value.isdigit() and 2 <= int(value) <= 60,
        "description": "QQ 自愈检查间隔秒数（2-60）",
        "secret": False,
    },
    "OOPZ_QQ_WATCHDOG_PORT_GRACE_SECONDS": {
        "validator": lambda value: value.isdigit() and 10 <= int(value) <= 3600,
        "description": "NapCat 端口失联后等待恢复秒数（10-3600）",
        "secret": False,
    },
    "OOPZ_QQ_WATCHDOG_GATEWAY_GRACE_SECONDS": {
        "validator": lambda value: value.isdigit() and 10 <= int(value) <= 3600,
        "description": "OneBot 失联后等待恢复秒数（10-3600）",
        "secret": False,
    },
    "OOPZ_QQ_WATCHDOG_ESCALATION_SECONDS": {
        "validator": lambda value: value.isdigit() and 10 <= int(value) <= 3600,
        "description": "QQ 自愈升级到下一动作的等待秒数（10-3600）",
        "secret": False,
    },
    "ANALYZER_PROVIDER": {
        "validator": lambda value: value in _PROVIDERS,
        "description": "deepseek / opencode-go / openai-compatible",
        "secret": False,
    },
    "ANALYZER_API_KEY": {
        "validator": lambda value: 8 <= len(value) <= 512 and "\n" not in value and "\r" not in value,
        "description": "分析 API Key（Bearer Token）",
        "secret": True,
    },
    "ANALYZER_BASE_URL": {
        "validator": _https_url,
        "description": "OpenAI 兼容 HTTPS Base URL（例如 https://host/v1）",
        "secret": False,
    },
    "ANALYZER_MODEL": {
        "validator": lambda value: bool(_MODEL_RE.fullmatch(value)),
        "description": "分析模型 ID（1-128 位；字母、数字、. _ : / -）",
        "secret": False,
    },
    "ANALYZER_TIMEOUT_SECONDS": {
        "validator": lambda value: value.isdigit() and 1 <= int(value) <= 600,
        "description": "单次 API 超时秒数（1-600）",
        "secret": False,
    },
    "ANALYZER_MAX_RETRIES": {
        "validator": lambda value: value.isdigit() and 0 <= int(value) <= 8,
        "description": "失败重试次数（0-8）",
        "secret": False,
    },
    "ANALYZER_MIN_INTERVAL_SECONDS": {
        "validator": lambda value: bool(re.fullmatch(r"\d+(?:\.\d+)?", value)) and 0 <= float(value) <= 60,
        "description": "相邻 API 请求最小间隔秒数（0-60）",
        "secret": False,
    },
    "ANALYZER_MAX_TOKENS": {
        "validator": lambda value: value.isdigit() and 128 <= int(value) <= 384000,
        "description": "非思考请求最大输出 Token（128-384000）",
        "secret": False,
    },
    "ANALYZER_THINKING_MAX_TOKENS": {
        "validator": lambda value: value.isdigit() and 128 <= int(value) <= 384000,
        "description": "思考请求最大输出 Token（128-384000）",
        "secret": False,
    },
    "ANALYZER_THINKING_MODE": {
        "validator": lambda value: value in _THINKING_MODES,
        "description": "auto / enabled / disabled；通用接口建议 disabled",
        "secret": False,
    },
    "ANALYZER_JSON_MODE": {
        "validator": lambda value: value in {"true", "false"},
        "description": "true / false；接口不支持 JSON 模式时设 false",
        "secret": False,
    },
}

KEY_ORDER = tuple(SETTABLE_KEYS)
SETTING_ALIASES = {
    "强制结束时间": "OOPZ_CUTOFF_LOCAL_HOUR",
    "结束时间": "OOPZ_CUTOFF_LOCAL_HOUR",
    "无人退出时间": "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS",
    "无人结束退出时间": "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS",
    "分片时长": "OOPZ_CHUNK_SECONDS",
    "转写修复次数": "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS",
    "报告回复超时": "OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS",
    "转写语言": "OOPZ_LANGUAGE",
    "保留音频": "OOPZ_RETAIN_AUDIO",
    "文本保留小时": "OOPZ_RETENTION_HOURS",
    "转写设备": "OOPZ_DEVICE",
    "分析并行数": "OOPZ_ANALYSIS_MAX_PARALLELISM",
    "QQ发送失败冷却": "OOPZ_ONEBOT_SEND_FAILURE_COOLDOWN_SECONDS",
    "QQ自愈检查间隔": "OOPZ_QQ_WATCHDOG_POLL_SECONDS",
    "QQ端口失联等待": "OOPZ_QQ_WATCHDOG_PORT_GRACE_SECONDS",
    "QQ网关失联等待": "OOPZ_QQ_WATCHDOG_GATEWAY_GRACE_SECONDS",
    "QQ自愈升级等待": "OOPZ_QQ_WATCHDOG_ESCALATION_SECONDS",
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


def apply_setting(key: str, value: str, *, env_path: Path | None = None) -> str:
    """Validate and persist one setting; returns a masked display value."""
    key = canonical_setting_key(key)
    if key not in SETTABLE_KEYS:
        raise ValueError("不支持的变量名；可用：" + "、".join(KEY_ORDER))
    value = _normalize_value(key, str(value or "").strip())
    if not SETTABLE_KEYS[key]["validator"](value):
        raise ValueError(f"变量 {key} 的值无效；要求：{SETTABLE_KEYS[key]['description']}")
    os.environ[key] = value
    path = env_path or _DEFAULT_ENV_PATH
    lines = _load_env(path)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    output: list[str] = []
    for line in lines:
        if pattern.match(line):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    _atomic_write(path, "\n".join(output) + "\n")
    return masked_value(key, value)


def setting_status(env_path: Path | None = None) -> dict[str, str]:
    """Report configured status for every settable key with masking."""
    path = env_path or _DEFAULT_ENV_PATH
    loaded: dict[str, str] = {}
    for line in _load_env(path):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        loaded[k.strip()] = v.strip().strip("\"'")
    result: dict[str, str] = {}
    for key in KEY_ORDER:
        value = os.environ.get(key) or loaded.get(key) or ""
        result[key] = masked_value(key, value) if value else "未设置"
    return result

def upsert_env(key: str, value: str, *, env_path: Path | None = None) -> None:
    """Write or update one KEY=VALUE line in the project .env (no whitelist)."""
    key = str(key or "").strip().upper()
    value = str(value or "").strip()
    os.environ[key] = value
    path = env_path or _DEFAULT_ENV_PATH
    lines = _load_env(path)
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    replaced = False
    output: list[str] = []
    for line in lines:
        if pattern.match(line):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    _atomic_write(path, "\n".join(output) + "\n")
