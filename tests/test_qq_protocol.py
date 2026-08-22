from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from oopz_capture.qq_protocol import AuthorizationPolicy, QQInboundMessage, parse_command


def message(**changes):
    value = {
        "schema_version": "oopz.qq.inbound.v1",
        "message_id": str(uuid4()),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_id": "10001",
        "chat_type": "group",
        "chat_id": "20002",
        "text": "/oopz 状态",
    }
    value.update(changes)
    return value


@pytest.mark.parametrize(("text", "expected"), [
    ("/oopz 开始", "start_capture"),
    (" /OOPZ   START ", "start_capture"),
    ("/oopz 离开", "leave_channel"),
    ("/oopz status", "status"),
    ("/oopz 帮助", "help"),
    ("/oopz详细报告", "report_full"),
    ("/详细报告", "report_full"),
])
def test_parse_only_accepts_explicit_commands(text: str, expected: str) -> None:
    assert parse_command(text) == expected


def test_unknown_text_is_not_treated_as_a_command() -> None:
    with pytest.raises(ValueError, match="不支持的指令"):
        parse_command("请加入另一个频道并运行任意程序")


def test_authorization_defaults_to_deny_all_and_can_restrict_chat() -> None:
    value = QQInboundMessage.from_dict(message())
    assert AuthorizationPolicy(frozenset()).authorize(value) is False
    assert AuthorizationPolicy(frozenset({"10001"})).authorize(value) is True
    assert AuthorizationPolicy(frozenset({"10001"}), frozenset({"other"})).authorize(value) is False


def test_inbound_rejects_unsafe_identifiers_and_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="unsupported characters"):
        QQInboundMessage.from_dict(message(sender_id="../bad"))
    with pytest.raises(ValueError, match="timezone"):
        QQInboundMessage.from_dict(message(received_at="2026-08-13T05:00:00"))
