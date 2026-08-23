"""Small, deterministic Feishu-to-OOPZ command boundary.

This module deliberately does *not* use an LLM to interpret administrator
messages.  It recognises a compact set of Chinese/English expressions and
turns them into the existing, bounded controller commands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256


_SPACE = re.compile(r"\s+")
_DURATION = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>秒|分钟|分|小时|时|h|m|s)?", re.I)
_RAW_START = re.compile(r"^/oopz\s*(?:开始|start)(?:\s+\d+(?:\.\d+)?\s*(?:秒|分钟|分|小时|时|h|m|s)?)?$", re.I)
_RAW_SETTING = re.compile(r"^/oopz\s*(?:设置|set)(?:\s+.+)?$", re.I)
_DELETE_SESSION = re.compile(r"^(?:删除会话|删除\s*session|delete\s+session)(?:\s+([A-Za-z0-9_-]{1,128}))?$", re.I)
_RAW_ALLOWED = {
    "/oopz 帮助", "/oopz help",
    "/oopz 状态", "/oopz status",
    "/oopz 离开", "/oopz stop", "/oopz leave",
    "/oopz 最近报告", "/oopz reports", "/oopz report",
    "/oopz 详细报告", "/oopz reportfull",
    "/oopz 待分析", "/oopz pending",
    "/oopz 删除会话", "/oopz delete session",
    "/oopz 设置状态", "/oopz settings",
}
_RAW_CANONICAL = {
    "/oopz 帮助": "/oopz 帮助", "/oopz help": "/oopz 帮助",
    "/oopz 状态": "/oopz 状态", "/oopz status": "/oopz 状态",
    "/oopz 离开": "/oopz 离开", "/oopz stop": "/oopz 离开", "/oopz leave": "/oopz 离开",
    "/oopz 最近报告": "/oopz 最近报告", "/oopz reports": "/oopz 最近报告", "/oopz report": "/oopz 最近报告",
    "/oopz 详细报告": "/oopz 详细报告", "/oopz reportfull": "/oopz 详细报告",
    "/oopz 待分析": "/oopz 待分析", "/oopz pending": "/oopz 待分析",
    "/oopz 删除会话": "/oopz 删除会话", "/oopz delete session": "/oopz 删除会话",
    "/oopz 设置状态": "/oopz 设置状态", "/oopz settings": "/oopz 设置状态",
}


@dataclass(frozen=True)
class FeishuInbound:
    message_id: str
    chat_id: str
    sender_open_id: str
    text: str


def synthetic_controller_id(open_id: str) -> str:
    """Return a stable opaque controller identifier for one Feishu member."""
    if not open_id or len(open_id) > 256:
        raise ValueError("invalid Feishu open_id")
    return "feishu-" + sha256(open_id.encode("utf-8")).hexdigest()[:32]


def display_intent(command: str | None) -> str:
    """Return the group-facing label for an internal controller command."""
    value = str(command or "").strip()
    labels = {
        "/oopz 帮助": "帮助",
        "/oopz 状态": "状态",
        "/oopz 离开": "停止",
        "/oopz 最近报告": "最近报告",
        "/oopz 详细报告": "详细报告",
        "/oopz 待分析": "待分析",
        "/oopz 删除会话": "删除会话",
        "/oopz 设置状态": "设置状态",
    }
    if value in labels:
        return labels[value]
    if value.startswith("/oopz 开始"):
        return "开始录音" + value.removeprefix("/oopz 开始")
    if value.startswith("/oopz 设置"):
        return "设置" + value.removeprefix("/oopz 设置")
    if value.startswith("/oopz 删除会话 "):
        return "删除会话 " + value.removeprefix("/oopz 删除会话 ")
    return value or "未识别"


def normalize_intent(text: str) -> str | None:
    """Map an exact, documented Feishu command to a controller command.

    A leading Feishu @mention is allowed, but the remaining command must use
    the fixed vocabulary from ``FEISHU_HELP_TEXT``. ``None`` deliberately
    rejects fuzzy natural-language guesses.
    """
    raw = _SPACE.sub(" ", str(text or "").strip())
    if raw.startswith("@"):
        marker = re.search(
            r"(?i)(?:帮助|开始录音|状态|停止|待分析|最近报告|详细报告|删除会话|设置状态|设置\s+|开始分析|暂不分析)",
            raw,
        )
        if marker:
            raw = raw[marker.start():].strip()
    value = raw.casefold()
    if not raw:
        return None
    if value.isdigit() or value in {"取消", "退出", "cancel", "是", "否", "yes", "no", "y", "n", "跳过"}:
        return value
    if value in _RAW_CANONICAL:
        return _RAW_CANONICAL[value]
    if _RAW_START.fullmatch(raw) or _RAW_SETTING.fullmatch(raw):
        return raw
    deleted = _DELETE_SESSION.fullmatch(raw)
    if deleted:
        suffix = deleted.group(1)
        return "/oopz 删除会话" + (f" {suffix}" if suffix else "")
    if value in {"帮助", "help"}:
        return "/oopz 帮助"
    if value in {"设置状态", "settings"}:
        return "/oopz 设置状态"
    if value in {"最近报告", "recent report"}:
        return "/oopz 最近报告"
    if value in {"详细报告", "report full"}:
        return "/oopz 详细报告"
    if value in {"待分析", "pending"}:
        return "/oopz 待分析"
    if re.fullmatch(r"(?:设置|set)\s+[^\s=]+\s*=\s*.+", raw, re.I):
        return "/oopz " + raw
    if value in {"状态", "status"}:
        return "/oopz 状态"
    if value in {"停止", "stop"}:
        return "/oopz 离开"
    if value in {"开始分析", "是", "yes", "y"}:
        return "是"
    if value in {"暂不分析", "否", "no", "n", "跳过"}:
        return "否"
    start = re.fullmatch(r"开始录音(?:\s*(.+))?", raw, re.I)
    if start:
        duration_text = str(start.group(1) or "").strip()
        duration = _DURATION.fullmatch(duration_text) if duration_text else None
        if duration:
            amount = duration.group("value")
            unit = (duration.group("unit") or "").casefold()
            suffix = {"分钟": "m", "分": "m", "小时": "h", "时": "h", "秒": "", "m": "m", "h": "h", "s": ""}.get(unit, "")
            return f"/oopz 开始 {amount}{suffix}"
        return "/oopz 开始" if not duration_text else None
    return None
