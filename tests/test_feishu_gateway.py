import asyncio
import json
from pathlib import Path

import pytest

from oopz_capture.analyzer.backend import BackendError
from oopz_capture.controller import ControllerConfig
from oopz_capture.controller_protocol import SenderPolicy
from oopz_capture.feishu_gateway import (
    FEISHU_SETTING_KEYS, LOCAL_ONLY_SETTING_KEYS, FeishuGateway,
    FeishuGatewayConfig, adapt_controller_reply_for_feishu,
)
from oopz_capture.feishu_protocol import FeishuInbound, synthetic_controller_id
from oopz_capture.send_request import enqueue_send_request


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, to, message, opts=None):
        self.sent.append((to, message, opts))
        return type("Result", (), {"success": True})()


class FakeController:
    def __init__(self, reply="收到"):
        self.received = []
        self.reply = reply
        self.analysed = []
        self.deleted = []

    async def handle(self, raw):
        self.received.append(raw)
        return {"text": self.reply}

    def busy_sessions(self):
        return frozenset()

    def _start_analysis_and_deliver(self, session_dir):
        self.analysed.append(session_dir.name)
        return True

    def _delete_session(self, session_id):
        self.deleted.append(session_id)


def config(tmp_path: Path) -> FeishuGatewayConfig:
    controller = ControllerConfig(
        output_root=tmp_path / "output", state_root=tmp_path / "feishu_state",
        authorization=SenderPolicy(frozenset({synthetic_controller_id("ou_admin")})),
        consent_confirmed=True,
    )
    return FeishuGatewayConfig("app", "secret", "oc_admins", tmp_path / "feishu_state", controller)


def make_session(tmp_path: Path, session_id: str, *, digest: bool, ready: bool = True) -> Path:
    session = tmp_path / "output" / session_id
    (session / "analysis" / "digest").mkdir(parents=True)
    (session / "lifecycle.json").write_text(
        json.dumps({"status": "ready_for_analysis" if ready else "recording"}), encoding="utf-8")
    if digest:
        (session / "analysis" / "digest" / "digest.png").write_bytes(b"png")
    return session


def test_production_gateway_rejects_missing_analyzer_cli(monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_FEISHU_APP_ID", "app")
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("OOPZ_FEISHU_ADMIN_CHAT_ID", "oc_admins")
    monkeypatch.delenv("OOPZ_ANALYZER_CLI", raising=False)
    monkeypatch.delenv("OOPZ_ANALYZER_HOME", raising=False)
    monkeypatch.delenv("OOPZ_CAPTURE_ONLY", raising=False)
    with pytest.raises(BackendError, match="OOPZ_ANALYZER_CLI"):
        FeishuGatewayConfig.from_env()


def test_every_member_of_configured_group_reaches_controller(tmp_path: Path) -> None:
    async def run():
        channel, controller = FakeChannel(), FakeController()
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_1", "oc_admins", "ou_admin", "开始录音 1小时"))
        await gateway.handle_message(FeishuInbound("om_2", "oc_other", "ou_admin", "状态"))
        await gateway.handle_message(FeishuInbound("om_3", "oc_admins", "ou_any_member", "状态"))
        await gateway.handle_message(FeishuInbound("om_1", "oc_admins", "ou_admin", "开始录音 1小时"))
        assert len(controller.received) == 2
        assert controller.received[0]["text"] == "/oopz 开始 1h"
        assert controller.received[1]["text"] == "/oopz 状态"
        assert len(channel.sent) == 2 and channel.sent[0][1] == {"text": "收到"}
    asyncio.run(run())


def test_configured_group_member_is_admitted_to_reused_controller_in_memory(tmp_path: Path) -> None:
    gateway = FeishuGateway(config(tmp_path), FakeChannel())
    member_id = synthetic_controller_id("ou_any_member")
    assert member_id not in gateway.controller.config.authorization.allowed_sender_ids
    assert gateway._allow_group_member_in_controller("ou_any_member") == member_id
    assert member_id in gateway.controller.config.authorization.allowed_sender_ids


def test_help_is_feishu_specific_and_does_not_enter_controller(tmp_path: Path) -> None:
    async def run():
        channel, controller = FakeChannel(), FakeController()
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_help", "oc_admins", "ou_admin", "帮助"))
        assert controller.received == []
        text = channel.sent[-1][1]["text"]
        assert "开始录音" in text and "最近图片" in text and "删除会话" in text
        assert "不需要再确认" in text and "报告" not in text and "批准" not in text
    asyncio.run(run())


def test_settings_status_lists_local_only_names_without_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "must-not-appear-in-status")
    monkeypatch.setenv("OOPZ_LOGIN_PASSWORD", "also-must-not-appear")
    gateway = FeishuGateway(config(tmp_path), FakeChannel(), controller=FakeController())

    status = gateway._settings_status_text()

    assert status.startswith("当前可通过飞书调整的运行设置：")
    assert "OOPZ_CHUNK_SECONDS =" in status
    assert "以下变量仅支持在本机 .env 修改" in status
    assert "OOPZ_FEISHU_APP_SECRET" in status
    assert "OOPZ_ANALYZER_CLI" in status
    assert "OOPZ_OUTPUT_ROOT" in status
    assert "must-not-appear-in-status" not in status
    assert "also-must-not-appear" not in status


def test_feishu_setting_classification_keeps_security_boundaries_local() -> None:
    expected_local = {
        "OOPZ_ANALYZER_CLI", "OOPZ_ANALYZER_HOME",
        "OOPZ_LOGIN_PHONE", "OOPZ_LOGIN_PASSWORD",
        "OOPZ_OUTPUT_ROOT",
        "OOPZ_FEISHU_APP_ID", "OOPZ_FEISHU_APP_SECRET", "OOPZ_FEISHU_ADMIN_CHAT_ID",
        "OOPZ_FEISHU_STATE_ROOT",
        "OOPZ_APP_VERSION",
    }
    assert set(LOCAL_ONLY_SETTING_KEYS) == expected_local
    assert not expected_local & FEISHU_SETTING_KEYS
    assert {"OOPZ_POLL_INTERVAL_SECONDS", "OOPZ_RECONNECT_WINDOW_SECONDS", "OOPZ_ANALYZER_MODEL"} <= FEISHU_SETTING_KEYS
    assert "OOPZ_SHOW_BROWSER" not in FEISHU_SETTING_KEYS


def test_recording_reply_is_rewritten_for_feishu(tmp_path: Path) -> None:
    async def run():
        legacy = "录音任务已启动；域：粘合国；频道：无分类 / 尼古喵喵；Session ID=session-1；时长=60 秒。发送 /oopz 离开 可提前结束录音。"
        channel, controller = FakeChannel(), FakeController(legacy)
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_start", "oc_admins", "ou_admin", "开始录音 1分钟"))
        response = channel.sent[-1][1]["text"]
        assert "/oopz" not in response
        assert "@OOPZ 后发送“停止”" in response
        assert "Session ID=session-1" in response
    asyncio.run(run())


def test_progress_prompts_are_rewritten_for_feishu() -> None:
    assert adapt_controller_reply_for_feishu("录音已占用，请用 /oopz 状态 查看进度。") == "录音已占用，请在本群 @OOPZ 后发送“状态”查看进度。"
    assert adapt_controller_reply_for_feishu("另一位管理员正在选择录音目标；发送 /oopz 状态 可查看详情。") == "另一位群成员正在选择录音目标；请在本群 @OOPZ 后发送“状态”查看详情。"
    assert "/oopz" not in adapt_controller_reply_for_feishu("不支持的指令；发送 /oopz 帮助 查看可用指令。")
    setting_prompt = adapt_controller_reply_for_feishu("格式：/oopz设置 变量名=值；可用变量见 /oopz 设置状态。")
    assert "/oopz" not in setting_prompt
    assert "设置状态" in setting_prompt


def test_recording_target_card_omits_list_spacing_from_markdown(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        controller = FakeController()
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway._send_reply(
            "已选择域：粘合国\n请选择语音频道：\n"
            "1. 无分类 / 尼古喵喵\n2. 无分类 / yy dz\n\n"
            "回复编号；回复 取消 可退出选择。"
        )
        card = channel.sent[-1][1]["card"]
        assert card["elements"][0] == {"tag": "markdown", "content": "已选择域：粘合国\n请选择语音频道："}
        assert [button["text"]["content"] for button in card["elements"][1]["actions"]] == ["无分类 / 尼古喵喵", "无分类 / yy dz", "取消选择"]
        assert len(card["elements"]) == 2
        await gateway.handle_card_action(
            action_id="selection:cancel", open_id="ou_admin",
            event_id="evt_cancel_selection", chat_id="oc_admins",
        )
        assert controller.received[-1]["text"] == "取消"
    asyncio.run(run())


def test_finished_digest_is_sent_as_an_image_only(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        gateway = FeishuGateway(config(tmp_path), channel, controller=FakeController())
        png = tmp_path / "digest.png"
        png.write_bytes(b"png")
        enqueue_send_request(gateway.state_root, target_type="group", target_id="oopz-group", text="",
                             source="digest:image", image_path=str(png))
        assert await gateway.drain_outbox() == 1
        assert channel.sent == [("oc_admins", {"image": {"source": str(png)}}, None)]
        assert await gateway.drain_outbox() == 0
    asyncio.run(run())


def test_text_notices_stay_in_the_admin_group(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        gateway = FeishuGateway(config(tmp_path), channel, controller=FakeController())
        enqueue_send_request(gateway.state_root, target_type="group", target_id="oopz-group",
                             text="分析失败，可发送“待分析”重试。", source="analysis_error")
        assert await gateway.drain_outbox() == 1
        assert channel.sent[0][0] == "oc_admins"
        assert channel.sent[0][1] == {"text": "分析失败，可发送“待分析”重试。"}
    asyncio.run(run())


def test_pending_card_lists_unanalysed_sessions_and_starts_the_analysis(tmp_path: Path) -> None:
    async def run():
        channel, controller = FakeChannel(), FakeController()
        make_session(tmp_path, "2026-10-03_14-32-31_BJT", digest=False)
        make_session(tmp_path, "2026-10-02_10-00-00_BJT", digest=True)                    # already has its image
        make_session(tmp_path, "2026-10-01_09-00-00_BJT", digest=False, ready=False)      # still recording
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_pending", "oc_admins", "ou_admin", "待分析"))
        buttons = channel.sent[-1][1]["card"]["elements"][1]["actions"]
        assert [b["value"]["action_id"] for b in buttons] == ["pending:analyze:2026-10-03_14-32-31_BJT"]
        assert "2026-10-03 14:32" in buttons[0]["text"]["content"]
        await gateway.handle_card_action(action_id=buttons[0]["value"]["action_id"], open_id="ou_admin",
                                         event_id="evt_a", chat_id="oc_admins")
        assert controller.analysed == ["2026-10-03_14-32-31_BJT"]
        assert "完成后图片会发到本群" in channel.sent[-1][1]["text"]
    asyncio.run(run())


def test_recent_digest_card_sends_the_selected_image_again(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        session = make_session(tmp_path, "2026-10-03_14-32-31_BJT", digest=True)
        gateway = FeishuGateway(config(tmp_path), channel, controller=FakeController())
        await gateway.handle_message(FeishuInbound("om_recent", "oc_admins", "ou_admin", "最近图片"))
        action = channel.sent[-1][1]["card"]["elements"][1]["actions"][0]["value"]["action_id"]
        assert action == "digest:send:2026-10-03_14-32-31_BJT"
        await gateway.handle_card_action(action_id=action, open_id="ou_admin", event_id="evt_img", chat_id="oc_admins")
        assert channel.sent[-1][1] == {"image": {"source": str(session / "analysis" / "digest" / "digest.png")}}
        await gateway.handle_message(FeishuInbound("om_old", "oc_admins", "ou_admin", "最近报告"))   # the old command is gone
        assert "未能可靠识别" in channel.sent[-1][1]["text"]
    asyncio.run(run())


def test_delete_needs_a_second_confirmation_and_only_removes_the_local_session(tmp_path: Path) -> None:
    async def run():
        channel, controller = FakeChannel(), FakeController()
        session_id = "2026-10-03_14-32-31_BJT"
        make_session(tmp_path, session_id, digest=True)
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_del", "oc_admins", "ou_admin", f"删除会话 {session_id}"))
        assert channel.sent[-1][1]["card"]["header"]["title"]["content"] == "确认删除会话"
        assert controller.deleted == []
        await gateway.handle_card_action(action_id=f"delete:confirm:{session_id}", open_id="ou_admin",
                                         event_id="evt_d1", chat_id="oc_admins")
        assert controller.deleted == [session_id]
        await gateway.handle_card_action(action_id=f"delete:confirm:{session_id}", open_id="ou_admin",
                                         event_id="evt_d1", chat_id="oc_admins")
        assert controller.deleted == [session_id]                      # the same click never runs twice
    asyncio.run(run())


def test_retention_deletes_expired_local_sessions(tmp_path: Path) -> None:
    async def run():
        controller = FakeController()
        session_id = "2026-08-22_10-51-32_BJT"
        session = make_session(tmp_path, session_id, digest=True)
        (session / "lifecycle.json").write_text(
            json.dumps({"status": "ready_for_analysis", "delete_after": "2026-09-01T00:00:00+00:00"}), encoding="utf-8")
        gateway = FeishuGateway(config(tmp_path), FakeChannel(), controller=controller)
        assert await gateway.cleanup_expired_sessions() == 1
        assert controller.deleted == [session_id]
    asyncio.run(run())
