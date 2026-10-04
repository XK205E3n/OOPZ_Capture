"""Small, deterministic Feishu-to-OOPZ command boundary.

No model interprets group messages.  A fixed vocabulary of Chinese words (with a few alternative
wordings people naturally use) is turned into the controller's internal commands ("/oopz ...").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256

HELP_TEXT = "\n".join([
    "@我 后发送：",
    "• 开始录音 [时长]：如“开始录音 1小时”，再点选语音频道；不写时长就录到手动结束",
    "• 结束录音：结束并自动出图（也可发“停止”）",
    "• 状态：看录音或出图进度",
    "• 重新出图：给没出图的录音重新分析",
    "• 重发图片：把已出的图再发一次",
    "• 删除录音：删除本地的录音和图片",
    "• 设置：查看或修改运行参数（“设置 变量=值”）",
])

_SPACE = re.compile(r"\s+")
_DURATION = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>秒|分钟|分|小时|时|h|m|s)?", re.I)
_RAW_START = re.compile(r"^/oopz\s*(?:开始|start)(?:\s+\d+(?:\.\d+)?\s*(?:秒|分钟|分|小时|时|h|m|s)?)?$", re.I)
_RAW_SETTING = re.compile(r"^/oopz\s*(?:设置|set)(?:\s+.+)?$", re.I)
_SESSION_ARG = re.compile(r"^(?:删除录音|删除会话|delete)(?:\s+([A-Za-z0-9_-]{1,128}))?$", re.I)
_SETTING = re.compile(r"^设置\s+[^\s=]+\s*=\s*.+$", re.I)

# word people may type -> internal command
_WORDS = {
    "帮助": "/oopz 帮助", "指令": "/oopz 帮助", "菜单": "/oopz 帮助", "help": "/oopz 帮助", "?": "/oopz 帮助", "？": "/oopz 帮助",
    "状态": "/oopz 状态", "进度": "/oopz 状态", "status": "/oopz 状态",
    "结束录音": "/oopz 离开", "结束": "/oopz 离开", "停止": "/oopz 离开", "停止录音": "/oopz 离开", "stop": "/oopz 离开",
    "重新出图": "/oopz 重新出图", "重新分析": "/oopz 重新出图", "待分析": "/oopz 重新出图", "出图": "/oopz 重新出图",
    "重发图片": "/oopz 重发图片", "补发图片": "/oopz 重发图片", "最近图片": "/oopz 重发图片", "发图": "/oopz 重发图片",
    "删除录音": "/oopz 删除录音", "删除会话": "/oopz 删除录音",
    "设置": "/oopz 设置状态", "设置状态": "/oopz 设置状态", "settings": "/oopz 设置状态",
}
_LABELS = {
    "/oopz 帮助": "帮助", "/oopz 状态": "状态", "/oopz 离开": "结束录音", "/oopz 重新出图": "重新出图",
    "/oopz 重发图片": "重发图片", "/oopz 删除录音": "删除录音", "/oopz 设置状态": "设置",
}
_RAW_ALLOWED = {"/oopz help": "/oopz 帮助", "/oopz status": "/oopz 状态", "/oopz stop": "/oopz 离开",
                "/oopz leave": "/oopz 离开", "/oopz settings": "/oopz 设置状态"} | {k: k for k in _LABELS}
# the longest wording first, so that "结束录音" is not cut at "结束"
_KEYWORDS = "|".join(sorted({"开始录音", "开始", "录音", *[w for w in _WORDS if not w.isascii()]}, key=len, reverse=True))


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
    if value in _LABELS:
        return _LABELS[value]
    if value.startswith("/oopz 开始"):
        return "开始录音" + value.removeprefix("/oopz 开始")
    if value.startswith("/oopz 设置"):
        return "设置" + value.removeprefix("/oopz 设置")
    if value.startswith("/oopz 删除录音 "):
        return "删除录音 " + value.removeprefix("/oopz 删除录音 ")
    return value or "未识别"


def _start(argument: str) -> str | None:
    if not argument:
        return "/oopz 开始"
    duration = _DURATION.fullmatch(argument)
    if not duration:
        return None
    unit = (duration.group("unit") or "").casefold()
    suffix = {"分钟": "m", "分": "m", "小时": "h", "时": "h", "秒": "", "m": "m", "h": "h", "s": ""}.get(unit, "")
    return f"/oopz 开始 {duration.group('value')}{suffix}"


def normalize_intent(text: str) -> str | None:
    """Map a command from the fixed vocabulary to the controller's internal command, else ``None``.

    A leading @mention is allowed.  Because the bot's display name is unknown here, an @mention prefix
    is cut at the first command word, so a chat sentence that merely ends with a bare command word
    (for example "……可以停止") is read as that command; members should avoid that.
    """
    raw = _SPACE.sub(" ", str(text or "").strip())
    if raw.startswith("@"):
        marker = re.search(rf"(?:{_KEYWORDS})", raw)
        if marker:
            raw = raw[marker.start():].strip()
    value = raw.casefold()
    if not raw:
        return None
    if value.isdigit() or value in {"取消", "退出", "cancel"}:
        return value
    if value in _WORDS:
        return _WORDS[value]
    if value in _RAW_ALLOWED:
        return _RAW_ALLOWED[value]
    if _RAW_START.fullmatch(raw) or _RAW_SETTING.fullmatch(raw):
        return raw
    if _SETTING.fullmatch(raw) or re.fullmatch(r"set\s+[^\s=]+\s*=\s*.+", raw, re.I):
        return "/oopz " + re.sub(r"^set\b", "设置", raw, flags=re.I)
    deleted = _SESSION_ARG.fullmatch(raw)
    if deleted:
        return "/oopz 删除录音" + (f" {deleted.group(1)}" if deleted.group(1) else "")
    start = re.fullmatch(r"(?:开始录音|录音|开始)(?:\s*(.+))?", raw)
    if start:
        return _start(str(start.group(1) or "").strip())
    return None
