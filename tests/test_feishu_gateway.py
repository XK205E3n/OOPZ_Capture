import asyncio
import json
from pathlib import Path

import pytest

from oopz_capture.feishu_gateway import (
    FEISHU_SETTING_KEYS, LOCAL_ONLY_SETTING_KEYS, FeishuGateway,
    FeishuGatewayConfig, adapt_controller_reply_for_feishu,
)
from oopz_capture.feishu_protocol import FeishuInbound, synthetic_controller_id
from oopz_capture.feishu_publisher import FeishuPublisher, PublicationConfig
from oopz_capture.controller import ControllerConfig
from oopz_capture.controller_protocol import SenderPolicy
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
        self._state = {"last_job": {"session_id": "session-1"}}
        self.reply = reply

    async def handle(self, raw):
        self.received.append(raw)
        return {"text": self.reply}


class FakePublisherClient:
    def __init__(self):
        self.calls = []

    async def create_document(self, **kwargs):
        self.calls.append(("create_document", kwargs)); return "docx_public"

    async def append_text_blocks(self, **kwargs):
        self.calls.append(("append_text_blocks", kwargs))

    async def set_anyone_with_link_readable(self, **kwargs):
        self.calls.append(("set_anyone_with_link_readable", kwargs))

    async def create_base_record(self, **kwargs):
        self.calls.append(("create_base_record", kwargs)); return "rec_public"

    async def set_document_private(self, **kwargs):
        self.calls.append(("set_document_private", kwargs))

    async def update_base_record(self, **kwargs):
        self.calls.append(("update_base_record", kwargs))

    async def delete_document(self, **kwargs):
        self.calls.append(("delete_document", kwargs))

    async def delete_base_record(self, **kwargs):
        self.calls.append(("delete_base_record", kwargs))


class MissingDocumentDeleteScopeClient(FakePublisherClient):
    async def delete_document(self, **kwargs):
        self.calls.append(("delete_document", kwargs))
        raise RuntimeError(
            "Feishu API failed: code=99991672, msg=Access denied; "
            "required scopes: [drive:drive, space:document:delete]"
        )


class MissingIndexDeleteScopeClient(FakePublisherClient):
    async def delete_base_record(self, **kwargs):
        self.calls.append(("delete_base_record", kwargs))
        raise RuntimeError(
            "Feishu API failed: code=99991672, msg=Access denied; "
            "required scopes: [bitable:app, base:record:delete]"
        )


class ReportController(FakeController):
    def __init__(self, root: Path):
        super().__init__()
        self.root = root
        self.deleted = []

    def _delivery_for_session(self, session_id):
        return ["公开摘要"], str(self.root / session_id / "public.pdf"), session_id

    def _internal_report_path(self, session_dir):
        return session_dir / "analysis" / "summary.md"

    def _delete_session(self, session_id):
        self.deleted.append(session_id)


def config(tmp_path: Path) -> FeishuGatewayConfig:
    open_id = "ou_admin"
    controller = ControllerConfig(
        output_root=tmp_path / "output", state_root=tmp_path / "feishu_state",
        authorization=SenderPolicy(frozenset({synthetic_controller_id(open_id)})),
        consent_confirmed=True,
    )
    return FeishuGatewayConfig("app", "secret", "oc_admins", tmp_path / "feishu_state", controller)


def test_production_gateway_rejects_incomplete_analyzer_configuration(monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_FEISHU_APP_ID", "app")
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("OOPZ_FEISHU_ADMIN_CHAT_ID", "oc_admins")
    for key in (
        "ANALYZER_PROVIDER", "ANALYZER_API_KEY", "ANALYZER_BASE_URL", "ANALYZER_MODEL",
        "ANALYZER_TIMEOUT_SECONDS", "ANALYZER_MAX_RETRIES", "ANALYZER_MIN_INTERVAL_SECONDS",
        "ANALYZER_MAX_TOKENS", "ANALYZER_THINKING_MAX_TOKENS",
        "ANALYZER_THINKING_MODE", "ANALYZER_JSON_MODE",
    ):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="required analyzer settings are missing"):
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
        assert "飞书群共用指令" in channel.sent[-1][1]["text"]
        assert "最近报告" in channel.sent[-1][1]["text"]
        assert "删除会话" in channel.sent[-1][1]["text"]
        assert "均对本群所有成员开放" in channel.sent[-1][1]["text"]
    asyncio.run(run())


def test_settings_status_lists_local_only_names_without_values(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_FEISHU_APP_SECRET", "must-not-appear-in-status")
    monkeypatch.setenv("ANALYZER_API_KEY", "also-must-not-appear")
    gateway = FeishuGateway(config(tmp_path), FakeChannel(), controller=FakeController())

    status = gateway._settings_status_text()

    assert status.startswith("当前可通过飞书调整的运行设置：")
    assert "OOPZ_CHUNK_SECONDS =" in status
    assert "以下变量仅支持在本机 .env 修改" in status
    assert "OOPZ_FEISHU_APP_SECRET" in status
    assert "ANALYZER_API_KEY" in status
    assert "OOPZ_OUTPUT_ROOT" in status
    assert "OOPZ_POLL_INTERVAL_SECONDS =" in status
    assert "用途：" not in status
    assert "状态：" not in status
    assert "限制原因：" not in status
    assert ".env 未设置时自动使用" not in status
    assert "must-not-appear-in-status" not in status
    assert "also-must-not-appear" not in status


def test_feishu_setting_classification_keeps_security_boundaries_local() -> None:
    expected_local = {
        "ANALYZER_API_KEY", "ANALYZER_BASE_URL",
        "OOPZ_LOGIN_PHONE", "OOPZ_LOGIN_PASSWORD",
        "OOPZ_OUTPUT_ROOT",
        "OOPZ_FEISHU_APP_ID", "OOPZ_FEISHU_APP_SECRET", "OOPZ_FEISHU_ADMIN_CHAT_ID",
        "OOPZ_FEISHU_STATE_ROOT", "OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN",
        "OOPZ_FEISHU_BASE_APP_TOKEN", "OOPZ_FEISHU_BASE_TABLE_ID",
        "OOPZ_FEISHU_PUBLIC_INDEX_URL",
        "OOPZ_APP_VERSION",
    }
    assert set(LOCAL_ONLY_SETTING_KEYS) == expected_local
    assert not expected_local & FEISHU_SETTING_KEYS
    assert {
        "OOPZ_POLL_INTERVAL_SECONDS", "OOPZ_RECONNECT_WINDOW_SECONDS",
        "ANALYZER_PROVIDER",
    } <= FEISHU_SETTING_KEYS
    assert "OOPZ_SHOW_BROWSER" not in FEISHU_SETTING_KEYS
    assert "OOPZ_REPORT_SELECTION_TIMEOUT_SECONDS" not in FEISHU_SETTING_KEYS


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
    assert adapt_controller_reply_for_feishu("Session=session-1 已在分析中，请用 /oopz 状态 查看进度。") == "Session=session-1 已在分析中，请在本群 @OOPZ 后发送“状态”查看进度。"
    assert adapt_controller_reply_for_feishu("另一位管理员正在选择录音目标；发送 /oopz 状态 可查看详情。") == "另一位群成员正在选择录音目标；请在本群 @OOPZ 后发送“状态”查看详情。"
    assert "/oopz" not in adapt_controller_reply_for_feishu("不支持的指令；发送 /oopz 帮助 查看可用指令。")
    analysis_prompt = adapt_controller_reply_for_feishu(
        "录音已结束（原因：operator_stop_command）；Session ID=session-1。转写完成：81/81；"
        "是否开始分析（opencode-go / mimo-v2.5）？回复：是 / 否。"
    )
    assert "回复" not in analysis_prompt
    assert "请点击下方按钮选择“开始分析”或“暂不分析”" in analysis_prompt
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


def test_analysis_outbox_card_never_requests_yes_no_reply(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        gateway = FeishuGateway(config(tmp_path), channel, controller=FakeController())
        enqueue_send_request(
            gateway.state_root,
            target_type="private",
            target_id=synthetic_controller_id("ou_admin"),
            text=("录音已结束；Session ID=session-1。转写完成：81/81；"
                  "是否开始分析（opencode-go / mimo-v2.5）？回复：是 / 否。"),
            source="analysis_decision",
        )

        assert await gateway.drain_outbox() == 1
        card = channel.sent[-1][1]["card"]
        body = card["elements"][0]["content"]
        assert "回复" not in body
        assert "请点击下方按钮选择“开始分析”或“暂不分析”" in body
        assert [button["text"]["content"] for button in card["elements"][1]["actions"]] == ["开始分析", "暂不分析"]
    asyncio.run(run())


def test_outbox_files_and_approval_card_stay_in_admin_group(tmp_path: Path) -> None:
    async def run():
        channel, controller = FakeChannel(), FakeController()
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        enqueue_send_request(gateway.state_root, target_type="private", target_id=synthetic_controller_id("ou_admin"), text="候选报告", source="publication_review:prompt")
        assert await gateway.drain_outbox() == 1
        assert channel.sent[0][0] == "oc_admins"
        assert "card" in channel.sent[0][1]
        action = "publication:approve:session-1"
        await gateway.handle_card_action(action_id=action, open_id="ou_any_member", event_id="evt_1", chat_id="oc_admins")
        decision = (gateway.state_root / "publication_decisions" / "session-1.json").read_text(encoding="utf-8")
        assert '"decision": "approve"' in decision
        await gateway.handle_card_action(action_id=action, open_id="ou_any_member", event_id="evt_2", chat_id="oc_admins")
        assert len(list((gateway.state_root / "publication_decisions").glob("*.json"))) == 1
    asyncio.run(run())


def test_withdraw_card_revokes_an_existing_publication(tmp_path: Path) -> None:
    async def run():
        channel, controller, client = FakeChannel(), FakeController(), FakePublisherClient()
        publication = PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index")
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller, publisher=FeishuPublisher(publication, client, output_root=tmp_path / "output"))
        path = gateway.state_root / "publication_decisions" / "session-1.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"session_id":"session-1","publication_created":true,"document_id":"docx_public","base_record_id":"rec_public"}', encoding="utf-8")
        await gateway.handle_card_action(action_id="publication:withdraw:session-1", open_id="ou_any_member", event_id="evt_withdraw", chat_id="oc_admins")
        assert [kind for kind, _ in client.calls] == ["set_document_private", "update_base_record"]
        assert '"revoked_by_open_id": "ou_any_member"' in path.read_text(encoding="utf-8")
        await gateway.handle_card_action(action_id="publication:bad", open_id="ou_any_member", event_id="evt_bad", chat_id="oc_admins")
    asyncio.run(run())


def test_report_cards_upload_the_selected_pdf_and_internal_markdown(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        session_id = "2026-08-22_10-51-32_BJT"
        session = tmp_path / "output" / session_id / "analysis"
        session.mkdir(parents=True)
        (session / "summary.md").write_text("内部报告", encoding="utf-8")
        (session.with_name("public.pdf")).write_bytes(b"pdf")
        controller = ReportController(tmp_path / "output")
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        await gateway.handle_message(FeishuInbound("om_reports", "oc_admins", "ou_admin", "最近报告"))
        assert "card" in channel.sent[-1][1]
        await gateway.handle_card_action(action_id=f"report:pdf:{session_id}", open_id="ou_admin", event_id="evt_pdf", chat_id="oc_admins")
        assert channel.sent[-2][1]["file"]["file_name"] == "public.pdf"
        await gateway.handle_message(FeishuInbound("om_full", "oc_admins", "ou_admin", "详细报告"))
        await gateway.handle_card_action(action_id=f"report:md:{session_id}", open_id="ou_admin", event_id="evt_md", chat_id="oc_admins")
        assert channel.sent[-2][1]["file"]["file_name"] == "summary.md"
    asyncio.run(run())


def test_delete_card_removes_published_document_and_index_before_local_session(tmp_path: Path) -> None:
    async def run():
        channel, client = FakeChannel(), FakePublisherClient()
        session_id = "2026-08-22_10-51-32_BJT"
        session = tmp_path / "output" / session_id
        session.mkdir(parents=True)
        controller = ReportController(tmp_path / "output")
        publication = PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index")
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller, publisher=FeishuPublisher(publication, client, output_root=tmp_path / "output"))
        path = gateway.state_root / "publication_decisions" / f"{session_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"session_id":"2026-08-22_10-51-32_BJT","publication_created":true,"document_id":"docx_public","base_record_id":"rec_public"}', encoding="utf-8")
        await gateway.handle_card_action(action_id=f"delete:confirm:{session_id}", open_id="ou_any_member", event_id="evt_delete", chat_id="oc_admins")
        assert [kind for kind, _ in client.calls] == ["delete_document", "delete_base_record"]
        assert controller.deleted == [session_id]
        assert '"deleted_at"' in path.read_text(encoding="utf-8")
        assert "公开文档和公开索引记录" in channel.sent[-1][1]["text"]
        assert f"Session ID={session_id}" in channel.sent[-1][1]["text"]
    asyncio.run(run())


def test_delete_card_distinguishes_nearby_sessions_and_does_not_claim_remote_deletion(tmp_path: Path) -> None:
    async def run():
        channel = FakeChannel()
        local_session_id = "2026-08-22_19-06-39_BJT"
        published_session_id = "2026-08-22_19-08-45_BJT"
        for session_id in (local_session_id, published_session_id):
            session = tmp_path / "output" / session_id
            session.mkdir(parents=True)
            analysis = session / "analysis"
            analysis.mkdir()
            (analysis / "summary.md").write_text("报告", encoding="utf-8")
        controller = ReportController(tmp_path / "output")
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller)
        decision = gateway.state_root / "publication_decisions" / f"{published_session_id}.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({
            "session_id": published_session_id,
            "publication_created": True,
            "document_id": "docx_public",
            "base_record_id": "rec_public",
        }), encoding="utf-8")

        card = gateway._delete_selection_card()
        labels = [button["text"]["content"] for button in card["elements"][1]["actions"]]
        assert any(local_session_id in label and "仅本地" in label for label in labels)
        assert any(published_session_id in label and "已发布" in label for label in labels)

        confirmation = gateway._delete_confirmation_card(local_session_id)
        assert local_session_id in confirmation["elements"][0]["content"]
        assert "没有待删除的公开文档或公开索引记录" in confirmation["elements"][0]["content"]

        await gateway.handle_card_action(
            action_id=f"delete:confirm:{local_session_id}", open_id="ou_any_member",
            event_id="evt_delete_local_only", chat_id="oc_admins",
        )
        response = channel.sent[-1][1]["text"]
        assert controller.deleted == [local_session_id]
        assert "没有公开文档或公开索引记录" in response
        assert "的本地会话、公开文档" not in response
    asyncio.run(run())


def test_delete_card_reports_missing_feishu_document_delete_scope(tmp_path: Path) -> None:
    async def run():
        channel, client = FakeChannel(), MissingDocumentDeleteScopeClient()
        session_id = "2026-08-22_19-08-45_BJT"
        session = tmp_path / "output" / session_id
        session.mkdir(parents=True)
        controller = ReportController(tmp_path / "output")
        publication = PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index")
        gateway = FeishuGateway(
            config(tmp_path), channel, controller=controller,
            publisher=FeishuPublisher(publication, client, output_root=tmp_path / "output"),
        )
        decision = gateway.state_root / "publication_decisions" / f"{session_id}.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({
            "session_id": session_id,
            "publication_created": True,
            "document_id": "docx_public",
            "base_record_id": "rec_public",
        }), encoding="utf-8")

        await gateway.handle_card_action(
            action_id=f"delete:confirm:{session_id}", open_id="ou_any_member",
            event_id="evt_delete_missing_scope", chat_id="oc_admins",
        )

        response = channel.sent[-1][1]["text"]
        assert "space:document:delete" in response
        assert "99991672" in response
        assert "本地会话和公开索引记录均未删除" in response
        assert controller.deleted == []
        assert [kind for kind, _ in client.calls] == ["delete_document"]
    asyncio.run(run())


def test_delete_card_persists_document_deletion_before_base_permission_failure(tmp_path: Path) -> None:
    async def run():
        channel, client = FakeChannel(), MissingIndexDeleteScopeClient()
        session_id = "2026-08-22_19-08-45_BJT"
        session = tmp_path / "output" / session_id
        session.mkdir(parents=True)
        controller = ReportController(tmp_path / "output")
        publication = PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index")
        gateway = FeishuGateway(
            config(tmp_path), channel, controller=controller,
            publisher=FeishuPublisher(publication, client, output_root=tmp_path / "output"),
        )
        decision = gateway.state_root / "publication_decisions" / f"{session_id}.json"
        decision.parent.mkdir(parents=True)
        decision.write_text(json.dumps({
            "session_id": session_id,
            "publication_created": True,
            "document_id": "docx_public",
            "base_record_id": "rec_public",
        }), encoding="utf-8")

        await gateway.handle_card_action(
            action_id=f"delete:confirm:{session_id}", open_id="ou_any_member",
            event_id="evt_delete_missing_base_scope", chat_id="oc_admins",
        )
        response = channel.sent[-1][1]["text"]
        saved = json.loads(decision.read_text(encoding="utf-8"))
        assert "公开文档已删除" in response
        assert "base:record:delete" in response
        assert saved["remote_document_deleted_at"]
        assert "remote_index_deleted_at" not in saved
        assert controller.deleted == []
        assert [kind for kind, _ in client.calls] == ["delete_document", "delete_base_record"]

        recovered_client = FakePublisherClient()
        gateway.publisher = FeishuPublisher(publication, recovered_client, output_root=tmp_path / "output")
        await gateway.handle_card_action(
            action_id=f"delete:confirm:{session_id}", open_id="ou_any_member",
            event_id="evt_delete_after_base_scope", chat_id="oc_admins",
        )
        assert [kind for kind, _ in recovered_client.calls] == ["delete_base_record"]
        assert controller.deleted == [session_id]
        assert "公开文档和公开索引记录" in channel.sent[-1][1]["text"]
    asyncio.run(run())


def test_retention_deletes_remote_report_before_local_session(tmp_path: Path) -> None:
    async def run():
        from datetime import datetime, timedelta, timezone

        channel, client = FakeChannel(), FakePublisherClient()
        session_id = "2026-08-22_10-51-32_BJT"
        session = tmp_path / "output" / session_id
        session.mkdir(parents=True)
        (session / "lifecycle.json").write_text(json.dumps({
            "delete_after": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        }), encoding="utf-8")
        controller = ReportController(tmp_path / "output")
        publication = PublicationConfig("folder", "base", "table", "https://feishu.cn/base/index")
        gateway = FeishuGateway(config(tmp_path), channel, controller=controller, publisher=FeishuPublisher(publication, client, output_root=tmp_path / "output"))
        path = gateway.state_root / "publication_decisions" / f"{session_id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "session_id": session_id, "publication_created": True,
            "document_id": "docx_public", "base_record_id": "rec_public",
        }), encoding="utf-8")
        assert await gateway.cleanup_expired_sessions() == 1
        assert [kind for kind, _ in client.calls] == ["delete_document", "delete_base_record"]
        assert controller.deleted == [session_id]
        assert "deleted_by_retention_at" in path.read_text(encoding="utf-8")
    asyncio.run(run())
