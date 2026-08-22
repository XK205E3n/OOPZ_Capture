from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, NAMESPACE_URL, uuid5


INBOUND_SCHEMA = "oopz.qq.inbound.v1"
REPLY_SCHEMA = "oopz.qq.reply.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
_COMMAND_ALIASES = {
    "/oopz start": "start_capture",
    "/oopz 开始": "start_capture",
    "/oopz开始": "start_capture",
    "/oopz leave": "leave_channel",
    "/oopz stop": "leave_channel",
    "/oopz 离开": "leave_channel",
    "/oopz离开": "leave_channel",
    "/oopz status": "status",
    "/oopz 状态": "status",
    "/oopz状态": "status",
    "/oopz help": "help",
    "/oopz 帮助": "help",
    "/oopz帮助": "help",
    "/oopz set": "set_config",
    "/oopz 设置": "set_config",
    "/oopz设置": "set_config",
    "/oopzset": "set_config",
    "/oopz settings": "settings_status",
    "/oopz 设置状态": "settings_status",
    "/oopz设置状态": "settings_status",
    "/oopzsetstatus": "settings_status",
    "/oopz report": "reports",
    "/oopz recent": "reports",
    "/oopz 报告": "reports",
    "/oopz报告": "reports",
    "/oopzrecent": "reports",
    "/oopz addadmin": "add_admin",
    "/oopz 增加管理员": "add_admin",
    "/oopz增加管理员": "add_admin",
    "/oopzaddadmin": "add_admin",
    "/oopz pending": "pending_sessions",
    "/oopz 待分析": "pending_sessions",
    "/oopz待分析": "pending_sessions",
    "/ooppending": "pending_sessions",
    "/oopz reportfull": "report_full",
    "/oopz 报告全文": "report_full",
    "/oopz报告全文": "report_full",
    "/oopzfull": "report_full",
    "/oopz reportdetail": "report_full",
    "/oopz 详细报告": "report_full",
    "/oopz详细报告": "report_full",
    "/详细报告": "report_full",
    "/oopz 报告详细": "report_full",
    "/oopz报告详细": "report_full",
    "/oopzdetail": "report_full",
}


START_ALIASES = ("/oopz start", "/oopz 开始", "/oopz开始", "/oopzstart")

SET_CONFIG_ALIASES = ("/oopz set", "/oopz 设置", "/oopz设置", "/oopzset")

SETTINGS_STATUS_ALIASES = ("/oopz settings", "/oopz 设置状态", "/oopz设置状态", "/oopzsetstatus")

REPORTS_ALIASES = ("/oopz report", "/oopz recent", "/oopz 报告", "/oopz报告", "/oopzrecent")
REPORT_FULL_ALIASES = (
    "/oopz reportfull", "/oopz 报告全文", "/oopz报告全文", "/oopzfull", "/oopz reportdetail",
    "/oopz 详细报告", "/oopz详细报告", "/详细报告",
    # Kept only for compatibility with previously published help text.
    "/oopz 报告详细", "/oopz报告详细", "/oopzdetail",
)

ADD_ADMIN_ALIASES = ("/oopz addadmin", "/oopz 增加管理员", "/oopz增加管理员", "/oopzaddadmin")
PENDING_ALIASES = ("/oopz pending", "/oopz 待分析", "/oopz待分析", "/ooppending")


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _SAFE_ID.fullmatch(text):
        raise ValueError(f"{field} contains unsupported characters or has an invalid length")
    return text


def _timestamp(value: Any) -> str:
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("received_at must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class QQInboundMessage:
    message_id: str
    received_at: str
    sender_id: str
    chat_type: str
    chat_id: str
    text: str
    schema_version: str = INBOUND_SCHEMA

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QQInboundMessage":
        if not isinstance(value, dict) or value.get("schema_version") != INBOUND_SCHEMA:
            raise ValueError(f"schema_version must be {INBOUND_SCHEMA}")
        message_id = str(value.get("message_id") or "")
        try:
            UUID(message_id)
        except ValueError as error:
            raise ValueError("message_id must be a UUID") from error
        chat_type = str(value.get("chat_type") or "")
        if chat_type not in {"group", "private"}:
            raise ValueError("chat_type must be group or private")
        text = str(value.get("text") or "").strip()
        if not text or len(text) > 200:
            raise ValueError("text must contain 1 to 200 characters")
        return cls(
            message_id=message_id,
            received_at=_timestamp(value.get("received_at")),
            sender_id=_identifier(value.get("sender_id"), "sender_id"),
            chat_type=chat_type,
            chat_id=_identifier(value.get("chat_id"), "chat_id"),
            text=text,
        )

    @property
    def requested_by(self) -> dict[str, str]:
        return {
            "source": "qq",
            "chat_type": self.chat_type,
            "chat_id": self.chat_id,
            "sender_id": self.sender_id,
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class AuthorizationPolicy:
    allowed_sender_ids: frozenset[str]
    allowed_chat_ids: frozenset[str] = frozenset()

    def authorize(self, message: QQInboundMessage) -> bool:
        if not self.allowed_sender_ids or message.sender_id not in self.allowed_sender_ids:
            return False
        return not self.allowed_chat_ids or message.chat_id in self.allowed_chat_ids


def parse_command(text: str) -> str:
    normalized = " ".join(text.strip().split()).casefold()
    try:
        return _COMMAND_ALIASES[normalized]
    except KeyError:
        pass
    for alias in SETTINGS_STATUS_ALIASES:
        if normalized.startswith(alias):
            return "settings_status"
    for alias in SET_CONFIG_ALIASES:
        if normalized.startswith(alias):
            return "set_config"
    for alias in START_ALIASES:
        if normalized.startswith(alias):
            return "start_capture"
    for alias in REPORT_FULL_ALIASES:
        if normalized.startswith(alias):
            return "report_full"
    for alias in REPORTS_ALIASES:
        if normalized.startswith(alias):
            return "reports"
    for alias in ADD_ADMIN_ALIASES:
        if normalized.startswith(alias):
            return "add_admin"
    for alias in PENDING_ALIASES:
        if normalized.startswith(alias):
            return "pending_sessions"
    raise ValueError("不支持的指令；发送 /oopz help 查看可用指令")


def make_reply(
    message: QQInboundMessage,
    *,
    command: str,
    status: str,
    text: str,
    at: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "schema_version": REPLY_SCHEMA,
        "reply_id": str(uuid5(NAMESPACE_URL, f"oopz-qq-reply:{message.message_id}")),
        "message_id": message.message_id,
        "command": command,
        "status": status,
        "at": at,
        "target": {"type": message.chat_type, "id": message.chat_id},
        "text": text,
        **fields,
    }
