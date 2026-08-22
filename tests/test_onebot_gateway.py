from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from oopz_capture.onebot_gateway import (
    ControllerDirectoryBridge,
    DiagnosticEchoBridge,
    OneBotGateway,
    OneBotGatewayConfig,
    OneBotRPC,
    _onebot_text_message,
    classify_event,
)
from oopz_capture.qq_send_request import (
    enqueue_send_request,
    list_send_requests,
    reschedule_send_request,
)


ADMIN = "123456789"
GROUP = "987654321"
FRIEND = "112120116"
TOKEN = "test-token-with-at-least-24-characters"


def config(tmp_path: Path, **changes) -> OneBotGatewayConfig:
    values = {
        "websocket_url": "ws://127.0.0.1:3001",
        "access_token": TOKEN,
        "admin_qq": ADMIN,
        "report_group_qq": GROUP,
        "report_friend_qq": FRIEND,
        "state_root": tmp_path / "state",
    }
    values.update(changes)
    return OneBotGatewayConfig(**values)


def private_event(**changes) -> dict:
    value = {
        "time": 1786630000,
        "self_id": 111111111,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": 24680,
        "user_id": int(ADMIN),
        "message": [{"type": "text", "data": {"text": "/oopz状态"}}],
        "raw_message": "/oopz状态",
    }
    value.update(changes)
    return value


def test_config_requires_loopback_strong_token_and_numeric_ids(tmp_path: Path) -> None:
    config(tmp_path).validate()
    with pytest.raises(ValueError, match="loopback"):
        config(tmp_path, websocket_url="ws://192.168.1.10:3001").validate()
    with pytest.raises(ValueError, match="24"):
        config(tmp_path, access_token="weak").validate()
    with pytest.raises(ValueError, match="ASCII digits"):
        config(tmp_path, admin_qq="admin").validate()


def test_public_config_summary_never_contains_token_or_full_ids(tmp_path: Path) -> None:
    summary = config(tmp_path).public_summary()
    serialized = json.dumps(summary)
    assert TOKEN not in serialized
    assert ADMIN not in serialized
    assert GROUP not in serialized
    assert summary["admin_qq_masked"].endswith(ADMIN[-4:])


def test_lifecycle_notice_targets_all_configured_admins(tmp_path: Path) -> None:
    asyncio.run(_lifecycle_notice_targets_all_configured_admins(tmp_path))


async def _lifecycle_notice_targets_all_configured_admins(tmp_path: Path) -> None:
    class FakeNotifier:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def notify(self, action: str, params: dict) -> None:
            self.calls.append((action, params))

    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "admins.json").write_text(
        json.dumps({"admin_ids": [ADMIN, "234567890"]}), encoding="utf-8"
    )
    gateway = OneBotGateway(config(tmp_path, state_root=state_root), DiagnosticEchoBridge(), mode="controller")
    rpc = FakeNotifier()
    await gateway._notify_admins(rpc, "OOPZ QQ 机器人已启动。")  # type: ignore[arg-type]
    assert [params["user_id"] for _, params in rpc.calls] == [123456789, 234567890]
    assert all(
        params["message"] == [{"type": "text", "data": {"text": "OOPZ QQ 机器人已启动。"}}]
        for _, params in rpc.calls
    )


def test_onebot_text_segment_replaces_invalid_control_characters() -> None:
    payload = _onebot_text_message("正常中文\nA\x00B\ud800C")
    text = payload[0]["data"]["text"]
    assert text == "正常中文\nA�B�C"
    assert text.encode("utf-8", errors="strict")


def test_send_requests_are_delivered_in_enqueue_order(tmp_path: Path) -> None:
    first = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="先发送文本", source="test",
    )
    second = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="再发送附件说明", source="test",
    )
    values = list_send_requests(tmp_path / "state", statuses={"pending"})
    assert [value["send_request_id"] for value in values] == [
        first["send_request_id"], second["send_request_id"],
    ]


def test_lifecycle_notice_queue_survives_an_offline_account(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "admins.json").write_text(
        json.dumps({"admin_ids": [ADMIN, "234567890"]}), encoding="utf-8"
    )
    gateway = OneBotGateway(config(tmp_path, state_root=state_root), DiagnosticEchoBridge(), mode="controller")
    gateway._queue_admin_lifecycle_notice("OOPZ QQ 机器人已启动。", source="gateway_startup")
    queued = list_send_requests(state_root, statuses={"pending"})
    assert {item["target_id"] for item in queued} == {ADMIN, "234567890"}
    assert all(item["source"] == "gateway_startup" for item in queued)


def test_private_admin_event_is_normalized_deterministically() -> None:
    first = classify_event(private_event(), admin_qq=ADMIN)
    second = classify_event(private_event(), admin_qq=ADMIN)
    assert first.kind == "accepted"
    assert first.inbound == second.inbound
    assert first.inbound is not None
    assert first.inbound["chat_type"] == "private"
    assert first.inbound["sender_id"] == ADMIN
    assert first.inbound["chat_id"] == ADMIN
    assert first.inbound["text"] == "/oopz状态"
    datetime.fromisoformat(first.inbound["received_at"])


def test_unauthorized_private_is_discarded_without_reading_content() -> None:
    class GuardedPrivate(dict):
        def get(self, key, default=None):
            if key in {"message", "raw_message"}:
                raise AssertionError("unauthorized private content was inspected")
            return super().get(key, default)

    event = GuardedPrivate(private_event(user_id=555555555))
    assert classify_event(event, admin_qq=ADMIN).kind == "ignored_unauthorized_private"


def test_group_event_is_discarded_before_message_content_is_read() -> None:
    class GuardedGroup(dict):
        def get(self, key, default=None):
            if key in {"message", "raw_message"}:
                raise AssertionError("group content was inspected")
            return super().get(key, default)

    event = GuardedGroup({
        "post_type": "message", "message_type": "group", "group_id": 222222222,
    })
    assert classify_event(event, admin_qq=ADMIN).kind == "ignored_group"


def test_non_text_segments_are_not_interpreted_as_commands() -> None:
    decision = classify_event(private_event(message=[
        {"type": "image", "data": {"file": "secret.jpg"}},
        {"type": "text", "data": {"text": "/oopz帮助"}},
    ]), admin_qq=ADMIN)
    assert decision.inbound is not None
    assert decision.inbound["text"] == "/oopz帮助"


def test_controller_directory_bridge_waits_for_matching_utf8_reply(tmp_path: Path) -> None:
    asyncio.run(_controller_directory_bridge_waits_for_matching_utf8_reply(tmp_path))


async def _controller_directory_bridge_waits_for_matching_utf8_reply(tmp_path: Path) -> None:
    bridge = ControllerDirectoryBridge(tmp_path / "state", timeout_seconds=2)
    inbound = classify_event(private_event(), admin_qq=ADMIN).inbound
    assert inbound is not None
    task = asyncio.create_task(bridge.submit(inbound))
    inbox = tmp_path / "state" / "inbox" / f"{inbound['message_id']}.json"
    for _ in range(100):
        if inbox.is_file():
            break
        await asyncio.sleep(0.01)
    assert json.loads(inbox.read_text(encoding="utf-8"))["text"] == "/oopz状态"
    reply = {
        "schema_version": "oopz.qq.reply.v1",
        "message_id": inbound["message_id"],
        "text": "当前状态：空闲。",
    }
    reply_path = tmp_path / "state" / "replies" / f"{inbound['message_id']}.json"
    reply_path.write_text(json.dumps(reply, ensure_ascii=False), encoding="utf-8")
    assert (await task)["text"] == "当前状态：空闲。"


class FakeRPC:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def action(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        return {"message_id": 13579}


def test_idle_gateway_health_probe_detects_logged_out_qq(tmp_path: Path) -> None:
    asyncio.run(_idle_gateway_health_probe_detects_logged_out_qq(tmp_path))


async def _idle_gateway_health_probe_detects_logged_out_qq(tmp_path: Path) -> None:
    class OfflineRPC:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def action(self, action: str, params: dict) -> dict:
            self.calls.append(action)
            return {"online": False, "good": False}

    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="controller")
    rpc = OfflineRPC()
    task = asyncio.create_task(gateway._outbox_loop(rpc))  # type: ignore[arg-type]
    try:
        for _ in range(50):
            if rpc.calls:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert rpc.calls == ["get_status"]
    assert gateway.state.qq_send_available is False
    assert "online=False" in str(gateway.state.last_qq_health_error)


def test_send_request_failure_is_rescheduled_instead_of_lost(tmp_path: Path) -> None:
    asyncio.run(_send_request_failure_is_rescheduled_instead_of_lost(tmp_path))


async def _send_request_failure_is_rescheduled_instead_of_lost(tmp_path: Path) -> None:
    class FailingRPC:
        async def action(self, action: str, params: dict) -> dict:
            raise RuntimeError("temporary QQ timeout")

    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    queued = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="录音结束", source="analysis_decision",
    )
    await gateway._deliver_send_requests(FailingRPC())  # type: ignore[arg-type]
    saved = next(item for item in list_send_requests(tmp_path / "state") if item["send_request_id"] == queued["send_request_id"])
    assert saved["status"] == "pending"
    assert saved["attempt_count"] == 1
    assert saved["next_attempt_at"] is not None
    assert "temporary QQ timeout" in saved["last_error"]
    assert gateway.state.qq_send_available is False
    assert gateway.state.consecutive_send_failures == 1
    assert gateway.state.last_send_failure_at is not None


def test_transient_send_failures_stay_pending_after_normal_retry_limit(tmp_path: Path) -> None:
    queued = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="录音结束", source="analysis_decision",
    )
    for _ in range(10):
        saved = reschedule_send_request(
            tmp_path / "state", queued["send_request_id"], error="retcode=1200 Timeout",
            retry_indefinitely=True,
        )
    assert saved["attempt_count"] == 10
    assert saved["status"] == "pending"
    assert saved["next_attempt_at"] is not None


def test_failed_attachment_queues_a_clear_administrator_notice(tmp_path: Path) -> None:
    asyncio.run(_failed_attachment_queues_a_clear_administrator_notice(tmp_path))


async def _failed_attachment_queues_a_clear_administrator_notice(tmp_path: Path) -> None:
    class FailingRPC:
        async def action(self, action: str, params: dict) -> dict:
            raise RuntimeError("attachment service rejected request")

    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    queued = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="", source="report_pdf", file_path="D:/reports/example.pdf",
        notify_admin_id=FRIEND,
    )
    request_path = tmp_path / "state" / "send_requests" / f"{queued['send_request_id']}.json"
    record = json.loads(request_path.read_text(encoding="utf-8"))
    record["max_attempts"] = 1
    request_path.write_text(json.dumps(record), encoding="utf-8")
    await gateway._deliver_send_requests(FailingRPC())  # type: ignore[arg-type]
    values = list_send_requests(tmp_path / "state")
    failed = next(item for item in values if item["send_request_id"] == queued["send_request_id"])
    notice = next(item for item in values if item["source"] == "qq_forward_failed")
    assert failed["status"] == "failed"
    assert notice["status"] == "pending"
    assert notice["target_id"] == FRIEND
    assert "报告转发失败" in notice["text"]


def test_attachments_use_dedicated_napcat_upload_actions(tmp_path: Path) -> None:
    asyncio.run(_attachments_use_dedicated_napcat_upload_actions(tmp_path))


async def _attachments_use_dedicated_napcat_upload_actions(tmp_path: Path) -> None:
    private_file = tmp_path / "report.pdf"
    group_file = tmp_path / "details.md"
    private_file.write_bytes(b"pdf")
    group_file.write_text("details", encoding="utf-8")
    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    rpc = FakeRPC()
    enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="", source="report_pdf", file_path=str(private_file),
    )
    enqueue_send_request(
        tmp_path / "state", target_type="group", target_id=GROUP,
        text="", source="report_md", file_path=str(group_file),
    )

    await gateway._deliver_send_requests(rpc)  # type: ignore[arg-type]

    assert [action for action, _ in rpc.calls] == [
        "upload_private_file", "upload_group_file",
    ]
    assert rpc.calls[0][1] == {
        "user_id": int(ADMIN),
        "file": str(private_file).replace("\\", "/"),
        "name": "report.pdf",
    }
    assert rpc.calls[1][1] == {
        "group_id": int(GROUP),
        "file": str(group_file).replace("\\", "/"),
        "name": "details.md",
    }


def test_gateway_records_offline_qq_health_and_expedites_after_recovery(tmp_path: Path) -> None:
    asyncio.run(_gateway_records_offline_qq_health_and_expedites_after_recovery(tmp_path))


async def _gateway_records_offline_qq_health_and_expedites_after_recovery(tmp_path: Path) -> None:
    class OfflineRPC:
        async def action(self, action: str, params: dict) -> dict:
            assert action == "get_status"
            return {"online": False, "good": False}

    class HealthyRPC:
        async def action(self, action: str, params: dict) -> dict:
            assert action == "get_status"
            return {"online": True, "good": True}

    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    queued = enqueue_send_request(
        tmp_path / "state", target_type="private", target_id=ADMIN,
        text="录音结束", source="analysis_decision",
    )
    assert await gateway._refresh_qq_health(OfflineRPC()) is False  # type: ignore[arg-type]
    assert gateway.state.qq_send_available is False
    gateway._last_qq_health_monotonic = -100.0
    assert await gateway._refresh_qq_health(HealthyRPC()) is True  # type: ignore[arg-type]
    saved = next(item for item in list_send_requests(tmp_path / "state") if item["send_request_id"] == queued["send_request_id"])
    assert saved["next_attempt_at"] is not None
    assert gateway.state.qq_send_available is True


def test_diagnostic_gateway_replies_privately_and_deduplicates(tmp_path: Path) -> None:
    asyncio.run(_diagnostic_gateway_replies_privately_and_deduplicates(tmp_path))


async def _diagnostic_gateway_replies_privately_and_deduplicates(tmp_path: Path) -> None:
    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    rpc = FakeRPC()
    event = private_event()
    await gateway.handle_event(event, rpc)  # type: ignore[arg-type]
    assert len(rpc.calls) == 1
    action, params = rpc.calls[0]
    assert action == "send_private_msg"
    assert params["user_id"] == int(ADMIN)
    assert params["message"][0]["type"] == "text"
    assert "不会登录 OOPZ" in params["message"][0]["data"]["text"]
    await gateway.handle_event(event, rpc)  # type: ignore[arg-type]
    assert len(rpc.calls) == 1
    state = json.loads((tmp_path / "state" / "onebot_gateway.json").read_text(encoding="utf-8"))
    assert state["counters"]["replies_sent"] == 1
    assert state["counters"]["duplicate_events"] == 1
    serialized = json.dumps(state, ensure_ascii=False)
    assert "/oopz" not in serialized


def test_concurrent_redelivery_only_sends_one_reply(tmp_path: Path) -> None:
    asyncio.run(_concurrent_redelivery_only_sends_one_reply(tmp_path))


async def _concurrent_redelivery_only_sends_one_reply(tmp_path: Path) -> None:
    class SlowBridge:
        async def submit(self, inbound: dict) -> dict:
            await asyncio.sleep(0.02)
            return {"text": "诊断回复"}

    gateway = OneBotGateway(config(tmp_path), SlowBridge(), mode="diagnostic_echo")
    rpc = FakeRPC()
    event = private_event()
    await asyncio.gather(
        gateway.handle_event(event, rpc),  # type: ignore[arg-type]
        gateway.handle_event(event, rpc),  # type: ignore[arg-type]
    )
    assert len(rpc.calls) == 1
    assert gateway.state.counters.replies_sent == 1
    assert gateway.state.counters.duplicate_events == 1


def test_gateway_silently_discards_group_and_non_admin_private(tmp_path: Path) -> None:
    asyncio.run(_gateway_silently_discards_group_and_non_admin_private(tmp_path))


async def _gateway_silently_discards_group_and_non_admin_private(tmp_path: Path) -> None:
    gateway = OneBotGateway(config(tmp_path), DiagnosticEchoBridge(), mode="diagnostic_echo")
    rpc = FakeRPC()
    await gateway.handle_event({"post_type": "message", "message_type": "group"}, rpc)  # type: ignore[arg-type]
    await gateway.handle_event(private_event(user_id=555555555), rpc)  # type: ignore[arg-type]
    assert rpc.calls == []
    assert gateway.state.counters.group_events_discarded == 1
    assert gateway.state.counters.unauthorized_private_discarded == 1


class FakeWebSocket:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, value: str) -> None:
        self.sent.append(value)


def test_rpc_correlates_action_response() -> None:
    asyncio.run(_rpc_correlates_action_response())


async def _rpc_correlates_action_response() -> None:
    websocket = FakeWebSocket()
    rpc = OneBotRPC(websocket, action_timeout_seconds=1)
    task = asyncio.create_task(rpc.action("get_login_info", {}))
    for _ in range(20):
        if websocket.sent:
            break
        await asyncio.sleep(0)
    request = json.loads(websocket.sent[0])
    assert rpc.accept_response({
        "status": "ok", "retcode": 0,
        "data": {"user_id": 111111111, "nickname": "test"},
        "echo": request["echo"],
    }) is True
    assert (await task)["nickname"] == "test"
