from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.output import write_json, write_jsonl
from oopz_capture.qq_controller import (
    QQControllerConfig,
    QQControllerService,
    _analysis_usage_notice,
    acquire_instance_lock,
    release_instance_lock,
)


def test_analysis_usage_notice_includes_tokens_and_opencode_reference_value() -> None:
    notice = _analysis_usage_notice({
        "result": {
            "model": {
                "usage": {
                    "prompt_tokens": 1200, "completion_tokens": 340,
                    "reasoning_tokens": 80, "total_tokens": 1540,
                },
                "cost_estimate": {"status": "subscription_estimate", "total_estimated_cost_usd": 0.0123456},
            },
        },
    })
    assert notice is not None
    assert "1,540" in notice
    assert "US$0.012346" in notice
    assert "不代表实际套餐扣费" in notice
from oopz_capture.qq_outbox import acknowledge_delivery, cleanup_outbox, list_outbox
from oopz_capture.qq_send_request import list_send_requests
from oopz_capture.qq_protocol import AuthorizationPolicy, QQInboundMessage


def inbound(text: str, *, sender: str = "10001", message_id: str | None = None) -> dict:
    return {
        "schema_version": "oopz.qq.inbound.v1",
        "message_id": message_id or str(uuid4()),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_id": sender,
        "chat_type": "private",
        "chat_id": sender,
        "text": text,
    }


def controller_config(tmp_path: Path) -> QQControllerConfig:
    return QQControllerConfig(
        output_root=tmp_path / "output", state_root=tmp_path / "state",
        authorization=AuthorizationPolicy(frozenset({"10001"}), frozenset({"20002"})),
        consent_confirmed=True,
    )


def install_target_choices(service: QQControllerService) -> None:
    async def areas():
        return [
            {"area_id": "hidden-area-id", "name": "朋友的小天地"},
            {"area_id": "other-area-id", "name": "备用域"},
        ]

    async def channels(area_id: str):
        assert area_id == "hidden-area-id"
        return [
            {"channel_id": "hidden-channel-id", "name": "大厅", "display_name": "日常 / 大厅"},
            {"channel_id": "other-channel-id", "name": "游戏房", "display_name": "游戏 / 游戏房"},
        ]

    service._load_area_choices = areas
    service._load_channel_choices = channels


async def choose_first_target(service: QQControllerService, start_text: str = "/oopz 开始") -> dict:
    area_reply = await service.handle(inbound(start_text))
    assert area_reply["status"] == "completed"
    assert "朋友的小天地" in area_reply["text"]
    assert "hidden-area-id" not in area_reply["text"]
    channel_reply = await service.handle(inbound("1"))
    assert channel_reply["status"] == "completed"
    assert "日常 / 大厅" in channel_reply["text"]
    assert "hidden-channel-id" not in channel_reply["text"]
    return await service.handle(inbound("1"))


def test_start_status_leave_analysis_and_outbox(tmp_path: Path) -> None:
    asyncio.run(_start_status_leave_analysis_and_outbox(tmp_path))


async def _start_status_leave_analysis_and_outbox(tmp_path: Path) -> None:
    capture_calls = []

    async def config_loader(show_browser: bool):
        assert show_browser is False
        return object()

    async def capture_runner(config, request, *, output_root, device, session_id):
        capture_calls.append((session_id, request))
        session = output_root / session_id
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "recording",
        })
        while not (session / "control" / "stop.json").is_file():
            await asyncio.sleep(0.01)
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "ready_for_analysis",
        })
        write_json(session / "handoff" / "analyzer_request.json", {})
        return session

    def analysis_runner(handoff, client):
        session = handoff.parent.parent
        report_id = str(uuid4())
        message_id = str(uuid4())
        write_jsonl(session / "handoff" / "qq_messages.jsonl", [{
            "schema_version": "oopz.qq.message.v1", "message_id": message_id,
            "request_id": str(uuid4()), "session_id": session.name, "report_id": report_id,
            "target": {"type": "group", "id": "20002"}, "delivery_status": "pending",
            "kind": "report", "message_index": 1, "message_count": 1,
            "text": f"最终报告 | Report ID={report_id} | Session ID={session.name}",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }])
        return {"result": {"report_id": report_id}}

    service = QQControllerService(
        controller_config(tmp_path), config_loader=config_loader,
        capture_runner=capture_runner, analysis_runner=analysis_runner,
        model_client_factory=lambda: object(),
    )
    install_target_choices(service)
    started = await choose_first_target(service, "/oopz 开始 90")
    assert started["status"] == "accepted"
    assert "Session ID=" in started["text"]
    assert "域：朋友的小天地" in started["text"]
    assert "频道：日常 / 大厅" in started["text"]
    assert "hidden-area-id" not in started["text"]
    assert "hidden-channel-id" not in started["text"]
    assert "时长=90 秒" in started["text"]

    for _ in range(100):
        lifecycle = tmp_path / "output" / started["session_id"] / "lifecycle.json"
        if lifecycle.is_file():
            break
        await asyncio.sleep(0.01)
    assert len(capture_calls) == 1
    assert capture_calls[0][1].area_id == "hidden-area-id"
    assert capture_calls[0][1].channel_id == "hidden-channel-id"
    assert capture_calls[0][1].max_runtime_seconds == 90
    for _ in range(100):
        saved = json.loads((tmp_path / "state" / "controller.json").read_text(encoding="utf-8"))
        if saved.get("active", {}).get("status") == "recording":
            break
        await asyncio.sleep(0.01)
    assert saved["active"]["status"] == "recording"
    status = await service.handle(inbound("/oopz 状态"))
    assert status["session_status"] == "recording"

    second = await service.handle(inbound("/oopz start"))
    assert second["status"] == "rejected"
    assert second["session_id"] == started["session_id"]

    leaving = await service.handle(inbound("/oopz 离开"))
    assert leaving["status"] == "accepted"
    await service.wait_until_idle()
    state = json.loads((tmp_path / "state" / "controller.json").read_text(encoding="utf-8"))
    assert state["active"] is None
    assert state["last_job"]["status"] == "waiting_analysis_decision"
    assert list_outbox(tmp_path / "state") == []
    ask = list_send_requests(tmp_path / "state", statuses={"pending"})
    assert any("是否开始分析" in item["text"] for item in ask)
    confirmed = await service.handle(inbound("是"))
    assert confirmed["status"] == "accepted"
    for _ in range(300):
        if not service._background_tasks:
            break
        await asyncio.sleep(0.01)
    delivered = list_send_requests(tmp_path / "state", statuses={"pending"})
    assert any("最终报告" in item["text"] and item["target_id"] == "10001" for item in delivered)
    assert service._load_flow() is not None


def test_unauthorized_sender_cannot_start(tmp_path: Path) -> None:
    asyncio.run(_unauthorized_sender_cannot_start(tmp_path))


async def _unauthorized_sender_cannot_start(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    result = await service.handle(inbound("/oopz 开始", sender="99999"))
    assert result["status"] == "rejected"
    assert result["command"] == "unauthorized"
    assert service._active_task is None


def test_authorized_admin_group_message_is_still_rejected(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = QQControllerService(controller_config(tmp_path))
        message = inbound("/oopz 状态")
        message["chat_type"] = "group"
        message["chat_id"] = "20002"
        reply = await service.handle(message)
        assert reply["status"] == "rejected"
        assert reply["command"] == "unauthorized"

    asyncio.run(scenario())


def test_leave_transfers_analysis_decision_to_stopping_admin(tmp_path: Path) -> None:
    asyncio.run(_leave_transfers_analysis_decision_to_stopping_admin(tmp_path))


async def _leave_transfers_analysis_decision_to_stopping_admin(tmp_path: Path) -> None:
    async def config_loader(show_browser: bool):
        return object()

    async def capture_runner(config, request, *, output_root, device, session_id):
        session = output_root / session_id
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "recording",
        })
        while not (session / "control" / "stop.json").is_file():
            await asyncio.sleep(0.01)
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "ready_for_analysis",
        })
        write_json(session / "handoff" / "analyzer_request.json", {})
        return session

    config = controller_config(tmp_path)
    config = QQControllerConfig(
        output_root=config.output_root,
        state_root=config.state_root,
        authorization=AuthorizationPolicy(frozenset({"10001", "10002"}), frozenset({"20002"})),
        consent_confirmed=True,
    )
    service = QQControllerService(
        config, config_loader=config_loader, capture_runner=capture_runner,
        analysis_runner=lambda handoff, client: {}, model_client_factory=lambda: object(),
    )
    install_target_choices(service)
    started = await choose_first_target(service)
    lifecycle = tmp_path / "output" / started["session_id"] / "lifecycle.json"
    for _ in range(100):
        if lifecycle.is_file():
            break
        await asyncio.sleep(0.01)

    leaving = await service.handle(inbound("/oopz 离开", sender="10002"))
    assert leaving["status"] == "accepted"
    await service.wait_until_idle()

    decision = json.loads((tmp_path / "state" / "analysis_decision.json").read_text(encoding="utf-8"))
    assert decision["admin_id"] == "10002"
    state = json.loads((tmp_path / "state" / "controller.json").read_text(encoding="utf-8"))
    assert state["last_job"]["analysis_admin_id"] == "10002"
    assert state["last_job"]["stop_requested_by"]["sender_id"] == "10002"
    asks = [item for item in list_send_requests(tmp_path / "state", statuses={"pending"})
            if item["source"] == "analysis_decision"]
    assert len(asks) == 1
    assert asks[0]["target_id"] == "10002"


def test_controller_retries_failed_transcription_before_analysis_prompt(tmp_path: Path, monkeypatch) -> None:
    repair_calls: list[str] = []

    async def config_loader(show_browser: bool):
        return object()

    async def capture_runner(config, request, *, output_root, device, session_id):
        session = output_root / session_id
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous",
            "status": "ready_for_analysis_with_errors", "chunks_total": 2,
            "chunks_transcribed": 1, "chunks_failed": 1,
        })
        write_json(session / "handoff" / "analyzer_request.json", {})
        return session

    async def repair(output_root, session_id, *, device):
        repair_calls.append(session_id)
        session = output_root / session_id
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous",
            "status": "ready_for_analysis", "chunks_total": 2,
            "chunks_transcribed": 2, "chunks_failed": 0,
        })
        return session

    monkeypatch.setattr("oopz_capture.qq_controller.repair_continuous_session", repair)

    async def scenario() -> None:
        service = QQControllerService(
            controller_config(tmp_path), config_loader=config_loader,
            capture_runner=capture_runner, analysis_runner=lambda handoff, client: {},
            model_client_factory=lambda: object(),
        )
        install_target_choices(service)
        started = await choose_first_target(service)
        await service.wait_until_idle()
        assert repair_calls == [started["session_id"]]
        asks = [item for item in list_send_requests(tmp_path / "state", statuses={"pending"})
                if item["source"] == "analysis_decision"]
        assert len(asks) == 1
        assert "转写完成：2/2" in asks[0]["text"]
        assert "失败分片" not in asks[0]["text"]

    asyncio.run(scenario())


def test_status_reconciles_completed_analysis_lifecycle(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    session_id = "2026-08-20_18-23-52_BJT"
    session_dir = tmp_path / "output" / session_id
    write_json(session_dir / "analysis_variants" / "mimo-v2.5-opencode-go" / "lifecycle.json", {
        "schema_version": "oopz.analysis.lifecycle.v1",
        "status": "ready_for_qq",
        "updated_at": "2026-08-20T20:00:00+00:00",
        "completed_at": "2026-08-20T20:00:00+00:00",
        "report_id": "report-123",
    })
    service._state["last_job"] = {"session_id": session_id, "status": "waiting_analysis_decision"}
    reply = service._status(QQInboundMessage.from_dict(inbound("/oopz 状态")), "status")
    assert "分析已完成，报告已排队发送" in reply["text"]
    assert service._state["last_job"]["status"] == "analysis_completed_report_queued"
    assert service._state["last_job"]["report_id"] == "report-123"


def test_report_forward_sends_basic_summary_before_pdf(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = QQControllerService(controller_config(tmp_path))
        pdf = tmp_path / "output" / "Report" / "report.pdf"
        pdf.parent.mkdir(parents=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        service._save_flow({
            "schema_version": "oopz.qq.report_flow.v1",
            "admin_id": "10001",
            "stage": "awaiting_target_id",
            "reports": [{
                "session_id": "session-1",
                "pieces": ["录音开始：测试\n录音结束：测试\n\n基本总结"],
                "pdf_path": str(pdf),
            }],
            "selected_index": 1,
            "target_type": "private",
        })
        reply = await service.handle(inbound("12345678"))
        assert "基本总结（1 段）和 PDF" in reply["text"]
        queued = list_send_requests(tmp_path / "state", statuses={"pending"})
        assert [item["source"] for item in queued] == ["report_forward:summary", "report_forward:pdf"]
        assert queued[0]["text"].endswith("基本总结")
        assert queued[1]["file_path"] == str(pdf)

    asyncio.run(scenario())


def test_multi_admin_report_flows_are_isolated_and_pending_command_supersedes_own_flow(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    for admin_id in ("10001", "10002"):
        service._save_flow({
            "schema_version": "oopz.qq.report_flow.v1",
            "admin_id": admin_id,
            "stage": "awaiting_target_type",
            "reports": [],
        })
    assert service._load_flow("10001") is not None
    assert service._load_flow("10002") is not None

    session = tmp_path / "output" / "2026-08-21_20-00-00_BJT"
    write_json(session / "handoff" / "analyzer_request.json", {})
    message = QQInboundMessage.from_dict(inbound("/oopz 待分析", sender="10002"))
    reply = service._pending_sessions(message, "pending_sessions")
    assert reply["status"] == "completed"
    assert service._load_flow("10002") is None
    assert service._load_flow("10001") is not None
    assert service._load_pending_flow("10002") is not None
    selection = service._process_pending_flow(
        QQInboundMessage.from_dict(inbound("1", sender="10002")), "pending_sessions",
    )
    assert selection is not None and "已选择 Session=" in selection["text"]


def test_report_forward_flow_times_out_after_three_minutes(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    service._save_flow({
        "schema_version": "oopz.qq.report_flow.v1",
        "admin_id": "10001",
        "stage": "awaiting_target_type",
        "reports": [],
    })
    flow = service._load_flow("10001")
    assert flow is not None
    expires_at = datetime.fromisoformat(flow["expires_at"])

    assert service._expire_report_flows(
        now=expires_at + timedelta(milliseconds=1),
    ) == 1
    assert service._load_flow("10001") is None
    notices = list_send_requests(tmp_path / "state", statuses={"pending"})
    assert len(notices) == 1
    assert notices[0]["target_id"] == "10001"
    assert notices[0]["source"] == "report_flow_timeout"
    assert "转发已自动跳过" in notices[0]["text"]


def test_report_text_uses_newest_analysis_bundle_not_stale_generic_handoff(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    session = tmp_path / "output" / "2026-08-21_20-00-00_BJT"
    old = session / "analysis" / "summary.md"
    new = session / "analysis_variants" / "configured-api" / "summary.md"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("旧报告", encoding="utf-8")
    new.write_text("新报告", encoding="utf-8")
    write_jsonl(session / "handoff" / "qq_messages.jsonl", [{"text": "过期QQ正文"}])
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    pieces = service._session_report_pieces(session)
    assert pieces == ["新报告"]


def test_leave_during_login_is_applied_after_session_creation(tmp_path: Path) -> None:
    asyncio.run(_leave_during_login_is_applied_after_session_creation(tmp_path))


async def _leave_during_login_is_applied_after_session_creation(tmp_path: Path) -> None:
    login_allowed = asyncio.Event()

    async def config_loader(show_browser: bool):
        await login_allowed.wait()
        return object()

    async def capture_runner(config, request, *, output_root, device, session_id):
        session = output_root / session_id
        write_json(session / "lifecycle.json", {
            "managed_by": "oopz-worker-v1", "mode": "continuous", "status": "connecting",
        })
        for _ in range(100):
            if (session / "control" / "stop.json").is_file():
                break
            await asyncio.sleep(0.01)
        assert (session / "control" / "stop.json").is_file()
        write_json(session / "handoff" / "analyzer_request.json", {})
        return session

    def analysis_runner(handoff, client):
        session = handoff.parent.parent
        report_id = str(uuid4())
        write_jsonl(session / "handoff" / "qq_messages.jsonl", [{
            "schema_version": "oopz.qq.message.v1", "message_id": str(uuid4()),
            "request_id": str(uuid4()), "session_id": session.name, "report_id": report_id,
            "target": {"type": "group", "id": "20002"}, "delivery_status": "pending",
            "kind": "report", "message_index": 1, "message_count": 1,
            "text": "测试", "created_at": datetime.now(timezone.utc).isoformat(),
        }])
        return {"result": {"report_id": report_id}}

    service = QQControllerService(
        controller_config(tmp_path), config_loader=config_loader,
        capture_runner=capture_runner, analysis_runner=analysis_runner,
        model_client_factory=lambda: object(),
    )
    install_target_choices(service)
    started = await choose_first_target(service)
    leaving = await service.handle(inbound("/oopz 离开"))
    assert leaving["status"] == "accepted"
    assert "连接建立后" in leaving["text"]
    login_allowed.set()
    await service.wait_until_idle()
    assert list_outbox(tmp_path / "state") == []
    ask = list_send_requests(tmp_path / "state", statuses={"pending"})
    assert any("是否开始分析" in item["text"] for item in ask)
    confirmed = await service.handle(inbound("是"))
    assert confirmed["status"] == "accepted"
    for _ in range(300):
        if not service._background_tasks:
            break
        await asyncio.sleep(0.01)
    assert any("测试" in item["text"] for item in list_send_requests(tmp_path / "state", statuses={"pending"}))


def test_controller_requires_whitelist_and_explicit_consent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="deny-all"):
        QQControllerConfig(
            output_root=tmp_path / "o",
            state_root=tmp_path / "s", authorization=AuthorizationPolicy(frozenset()),
            consent_confirmed=True,
        ).validate()
    with pytest.raises(ValueError, match="CONSENT_CONFIRMED"):
        QQControllerConfig(
            output_root=tmp_path / "o",
            state_root=tmp_path / "s", authorization=AuthorizationPolicy(frozenset({"1"})),
            consent_confirmed=False,
        ).validate()


def test_transcription_completion_note_includes_failed_chunk_warning(tmp_path: Path) -> None:
    service = QQControllerService(controller_config(tmp_path))
    session_dir = tmp_path / "output" / "2026-08-20_18-23-52_BJT"
    session_dir.mkdir(parents=True)
    write_json(session_dir / "lifecycle.json", {
        "chunks_total": 12,
        "chunks_transcribed": 10,
        "chunks_failed": 2,
    })
    note = service._transcription_completion_note(session_dir)
    assert "10/12" in note
    assert "失败分片：2" in note
    assert "音频已保留" in note


def test_controller_instance_lock_rejects_second_process(tmp_path: Path) -> None:
    lock = acquire_instance_lock(tmp_path / "state")
    try:
        with pytest.raises(RuntimeError, match="already running"):
            acquire_instance_lock(tmp_path / "state")
    finally:
        release_instance_lock(lock)
