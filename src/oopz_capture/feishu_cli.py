from __future__ import annotations

import argparse
import asyncio
import json
from hashlib import sha256
import logging
import os
from pathlib import Path
import re
import sys
from typing import Sequence

from .feishu_gateway import FEISHU_HELP_TEXT, FeishuGateway, FeishuGatewayConfig
from .feishu_publisher import FeishuPublisher, LarkPublishingClient
from .settings import upsert_env


_FEISHU_GROUP_CHAT_ID = re.compile(r"^oc_[A-Za-z0-9]+$")


def _payload_text(value: object) -> str:
    """Extract user-entered text from a Feishu wire-content fragment."""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ""
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            return text
        return _payload_text(decoded)
    if isinstance(value, list):
        return "\n".join(filter(None, (_payload_text(item) for item in value))).strip()
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    # These cover text messages and every locale/post AST used by Feishu.
    for key in ("text", "title", "content"):
        if key in value:
            item = _payload_text(value.get(key))
            if item:
                parts.append(item)
    if parts:
        return "\n".join(parts).strip()
    for item in value.values():
        text = _payload_text(item)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def inbound_message_text(message: object) -> str:
    """Return command text even when the Channel SDK leaves content_text empty."""
    candidates = [
        getattr(message, "content_text", ""),
        getattr(getattr(message, "content", None), "text", ""),
        getattr(getattr(message, "content", None), "raw", None),
    ]
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict):
        candidates.append(raw.get("content"))
        nested_message = raw.get("message")
        if isinstance(nested_message, dict):
            candidates.append(nested_message.get("content"))
    for candidate in candidates:
        text = _payload_text(candidate)
        if text:
            return text
    return ""


def lifecycle_notices(lifecycle: str | None) -> tuple[str, ...]:
    """Return the notices appropriate for a newly connected gateway."""
    if lifecycle == "started":
        return (
            "OOPZ 飞书机器人已启动：正在监听本群 @OOPZ 指令，并显示录音、转写、分析和文件投递状态。",
            FEISHU_HELP_TEXT,
        )
    if lifecycle == "restarted":
        return ("OOPZ 飞书机器人重启完成：已恢复本群指令监听与状态更新。",)
    return ()


def _append_process_logs(runtime_log: str | None, error_log: str | None) -> tuple[object, ...]:
    """Route gateway output to durable, line-buffered append-only log files."""
    handles: list[object] = []
    if runtime_log:
        path = Path(runtime_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        stdout = path.open("a", encoding="utf-8", buffering=1)
        sys.stdout = stdout
        handles.append(stdout)
    if error_log:
        path = Path(error_log)
        path.parent.mkdir(parents=True, exist_ok=True)
        stderr = path.open("a", encoding="utf-8", buffering=1)
        sys.stderr = stderr
        handles.append(stderr)
    return tuple(handles)


def bind_admin_chat_id(chat_id: str, *, env_path: Path | None = None) -> bool:
    """Persist the first invited group without replacing an existing binding."""
    current = os.environ.get("OOPZ_FEISHU_ADMIN_CHAT_ID", "").strip()
    if current:
        return False
    candidate = str(chat_id or "").strip()
    if not _FEISHU_GROUP_CHAT_ID.fullmatch(candidate):
        raise ValueError("invalid Feishu group chat_id")
    upsert_env("OOPZ_FEISHU_ADMIN_CHAT_ID", candidate, env_path=env_path)
    return True


async def _wait_for_admin_group_invitation(*, app_id: str, app_secret: str, channel_type: object, events: object, log_level: object) -> str:
    """Wait in read-only bootstrap mode until the bot is invited to a group."""
    channel = channel_type(app_id=app_id, app_secret=app_secret, log_level=log_level.WARNING)
    discovered: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def on_bot_added(event: object) -> None:
        chat_id = str(getattr(event, "chat_id", "") or "").strip()
        if not _FEISHU_GROUP_CHAT_ID.fullmatch(chat_id):
            return
        if not discovered.done():
            discovered.set_result(chat_id)

    channel.on(events.BOT_ADDED, on_bot_added)
    await channel.connect_until_ready()
    print("尚未绑定控制群；请将机器人邀请至目标群聊，程序将自动保存群 ID。", flush=True)
    try:
        return await discovered
    finally:
        await channel.disconnect()


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    parser = argparse.ArgumentParser(prog="oopz-feishu", description="OOPZ Feishu group-control gateway")
    parser.add_argument("command", choices=["serve", "drain", "notify", "reconcile-publications", "repair-publication-index", "backfill-publications", "discover-ids"])
    parser.add_argument("message", nargs="?", help="message text for notify")
    parser.add_argument("--lifecycle", choices=["started", "restarted"], help="send this lifecycle status once the long connection is ready")
    parser.add_argument("--runtime-log", help="append stdout to this UTF-8 log (serve only)")
    parser.add_argument("--error-log", help="append stderr to this UTF-8 log (serve only)")
    args = parser.parse_args(argv)
    log_handles: tuple[object, ...] = ()
    if args.command == "serve":
        log_handles = _append_process_logs(args.runtime_log, args.error_log)
    try:
        from lark_oapi.channel import Events, FeishuChannel, PolicyConfig
        from lark_oapi.core.enum import LogLevel
    except ImportError as error:
        raise RuntimeError('Feishu support is missing; install with pip install -e ".[feishu]"') from error
    # The SDK's INFO-level connection diagnostic includes the ephemeral WebSocket
    # URL. Keep local operational logs free of connection credentials.
    logging.getLogger("Lark").setLevel(logging.WARNING)

    # Manual discovery is deliberately read-only: a first group @ event supplies
    # the chat_id. The open_id is
    # printed only for diagnostics; group members are not individually listed.
    # It cannot execute controller commands or send any messages.
    if args.command == "discover-ids":
        app_id = os.environ.get("OOPZ_FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("OOPZ_FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise ValueError("OOPZ_FEISHU_APP_ID and OOPZ_FEISHU_APP_SECRET are required for discover-ids")
        channel = FeishuChannel(app_id=app_id, app_secret=app_secret, log_level=LogLevel.WARNING)
        discovered = asyncio.Event()

        async def on_discovery_message(message) -> None:
            print(f"OOPZ_FEISHU_ADMIN_CHAT_ID={message.chat_id}", flush=True)
            print(f"FEISHU_SENDER_OPEN_ID={message.sender_id}  # informational; not required for group-wide control", flush=True)
            discovered.set()

        channel.on(Events.MESSAGE, on_discovery_message)

        async def discover() -> None:
            await channel.connect_until_ready()
            print("飞书长连接已就绪。请在目标受控群发送：@OOPZ 管理机器人 帮助", flush=True)
            try:
                await discovered.wait()
            finally:
                await channel.disconnect()

        asyncio.run(discover())
        return 0

    if args.command == "serve" and not os.environ.get("OOPZ_FEISHU_ADMIN_CHAT_ID", "").strip():
        app_id = os.environ.get("OOPZ_FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("OOPZ_FEISHU_APP_SECRET", "").strip()
        if not app_id or not app_secret:
            raise ValueError("OOPZ_FEISHU_APP_ID and OOPZ_FEISHU_APP_SECRET are required before automatic group binding")
        chat_id = asyncio.run(_wait_for_admin_group_invitation(
            app_id=app_id,
            app_secret=app_secret,
            channel_type=FeishuChannel,
            events=Events,
            log_level=LogLevel,
        ))
        if bind_admin_chat_id(chat_id):
            print("已自动绑定机器人首次加入的飞书群；后续邀请不会覆盖该设置。", flush=True)

    config = FeishuGatewayConfig.from_env()
    channel = FeishuChannel(
        app_id=config.app_id,
        app_secret=config.app_secret,
        log_level=LogLevel.WARNING,
        policy=PolicyConfig(
            dm_policy="disabled",
            group_policy="allowlist",
            group_allowlist=[config.admin_chat_id],
            require_mention=True,
        ),
    )
    publisher = (FeishuPublisher(config.publication, LarkPublishingClient(config.app_id, config.app_secret), output_root=config.controller_config.output_root) if config.publication else None)
    gateway = FeishuGateway(config, channel, publisher=publisher)
    if args.command == "drain":
        print(asyncio.run(gateway.drain_outbox()))
        return 0
    if args.command == "notify":
        text = str(args.message or "").strip()
        if not text:
            raise ValueError("notify requires a non-empty message")

        async def notify() -> None:
            await channel.connect_until_ready()
            try:
                await gateway.send_lifecycle_notice(text)
            finally:
                await channel.disconnect()
        asyncio.run(notify())
        return 0
    if args.command == "reconcile-publications":
        print(asyncio.run(gateway.reconcile_publications()))
        return 0
    if args.command == "repair-publication-index":
        print(asyncio.run(gateway.repair_publication_index()))
        return 0
    if args.command == "backfill-publications":
        print(asyncio.run(gateway.backfill_publications()))
        return 0

    async def on_message(message):
        await gateway.handle_message(type("Inbound", (), {
            "message_id": message.message_id,
            "chat_id": message.chat_id,
            "sender_open_id": message.sender_id,
            "text": inbound_message_text(message),
        })())

    async def on_card(event):
        if str(getattr(event, "chat_id", "")) != config.admin_chat_id:
            return
        value = getattr(getattr(event, "action", None), "value", {}) or {}
        action_id = str(value.get("action_id") or "")
        event_id = "card_" + sha256(
            f"{getattr(event, 'message_id', '')}:{getattr(event.operator, 'open_id', '')}:{action_id}".encode("utf-8")
        ).hexdigest()
        await gateway.handle_card_action(
            action_id=action_id,
            open_id=str(event.operator.open_id),
            event_id=event_id,
            chat_id=str(getattr(event, "chat_id", "")),
        )

    channel.on(Events.MESSAGE, on_message)
    channel.on(Events.CARD_ACTION, on_card)

    async def serve() -> None:
        last_reconcile = 0.0
        last_retention_cleanup = 0.0
        try:
            await channel.connect_until_ready()
            print("飞书长连接已就绪；正在监听受控群的 @OOPZ 指令。", flush=True)
            for notice in lifecycle_notices(args.lifecycle):
                await gateway.send_lifecycle_notice(notice)
            while True:
                await gateway.drain_outbox()
                now = asyncio.get_running_loop().time()
                if now - last_reconcile >= 3600:
                    await gateway.reconcile_publications()
                    last_reconcile = now
                if now - last_retention_cleanup >= 60:
                    await gateway.cleanup_expired_sessions()
                    last_retention_cleanup = now
                await asyncio.sleep(1)
        finally:
            await channel.disconnect()
    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
