import asyncio
import sys
from types import SimpleNamespace

import pytest

from oopz_capture.feishu_cli import (
    _append_process_logs,
    _wait_for_admin_group_invitation,
    bind_admin_chat_id,
    inbound_message_text,
    lifecycle_notices,
)
from oopz_capture.feishu_gateway import FEISHU_HELP_TEXT


def test_fresh_start_sends_startup_and_help() -> None:
    notices = lifecycle_notices("started")
    assert len(notices) == 2
    assert notices[-1] == FEISHU_HELP_TEXT


def test_restart_sends_completion_without_help() -> None:
    notices = lifecycle_notices("restarted")
    assert len(notices) == 1
    assert FEISHU_HELP_TEXT not in notices
    assert "重启完成" in notices[0]


def test_first_group_invitation_persists_admin_chat_id(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OOPZ_FEISHU_ADMIN_CHAT_ID", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("OOPZ_FEISHU_APP_ID=cli_test\n", encoding="utf-8")

    assert bind_admin_chat_id("oc_firstgroup123", env_path=env_path) is True
    assert "OOPZ_FEISHU_ADMIN_CHAT_ID=oc_firstgroup123" in env_path.read_text(encoding="utf-8")


def test_existing_admin_group_is_never_replaced(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_FEISHU_ADMIN_CHAT_ID", "oc_existing123")
    env_path = tmp_path / ".env"

    assert bind_admin_chat_id("oc_other456", env_path=env_path) is False
    assert not env_path.exists()


def test_automatic_group_binding_rejects_non_group_id(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OOPZ_FEISHU_ADMIN_CHAT_ID", raising=False)
    with pytest.raises(ValueError, match="invalid Feishu group chat_id"):
        bind_admin_chat_id("ou_person", env_path=tmp_path / ".env")


def test_bootstrap_waits_for_bot_added_event_and_disconnects() -> None:
    channels = []

    class FakeChannel:
        def __init__(self, **kwargs):
            self.handlers = {}
            self.disconnected = False
            channels.append(self)

        def on(self, event, handler):
            self.handlers[event] = handler

        async def connect_until_ready(self):
            await self.handlers["botAdded"](SimpleNamespace(chat_id="oc_invited789"))

        async def disconnect(self):
            self.disconnected = True

    result = asyncio.run(_wait_for_admin_group_invitation(
        app_id="cli_test",
        app_secret="secret_test",
        channel_type=FakeChannel,
        events=SimpleNamespace(BOT_ADDED="botAdded"),
        log_level=SimpleNamespace(WARNING="warning"),
    ))

    assert result == "oc_invited789"
    assert channels[0].disconnected is True


def test_inbound_text_falls_back_to_raw_text_content() -> None:
    message = SimpleNamespace(
        content_text="",
        content=SimpleNamespace(text="", raw={}),
        raw={"content": '{"text":"@_user_1 停止"}'},
    )
    assert inbound_message_text(message) == "@_user_1 停止"


def test_inbound_text_falls_back_to_rich_post_content() -> None:
    message = SimpleNamespace(
        content_text="",
        content=SimpleNamespace(text="", raw={}),
        raw={"content": {"zh_cn": {"content": [[
            {"tag": "at", "user_name": "OOPZ 管理机器人"},
            {"tag": "text", "text": "停止"},
        ]]}}},
    )
    assert "停止" in inbound_message_text(message)


def test_gateway_logs_append_without_erasing_existing_history(tmp_path) -> None:
    runtime_log = tmp_path / "runtime.log"
    error_log = tmp_path / "error.log"
    runtime_log.write_text("existing-message\n", encoding="utf-8")
    original_stdout, original_stderr = sys.stdout, sys.stderr
    handles = ()
    try:
        handles = _append_process_logs(str(runtime_log), str(error_log))
        print("new-message", flush=True)
        print("new-error", file=sys.stderr, flush=True)
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        for handle in handles:
            handle.close()
    assert runtime_log.read_text(encoding="utf-8") == "existing-message\nnew-message\n"
    assert error_log.read_text(encoding="utf-8") == "new-error\n"
