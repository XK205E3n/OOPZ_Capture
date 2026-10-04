"""Feishu M1/M2 adapter using the official ``lark-oapi`` Channel SDK."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .feishu_protocol import HELP_TEXT, FeishuInbound, display_intent, normalize_intent, synthetic_controller_id
from .jsonio import atomic_json as _atomic_json, iso_utc as _iso, read_json_or_none as _read_json_or_none
from .controller import ControllerConfig, ControllerService, _env_bool
from .controller_protocol import SenderPolicy
from .analyzer.backend import QoderCli
from .sessions import digest_md, digest_png, find_pending_sessions, find_recent_digests
from .settings import canonical_setting_key, setting_description, setting_is_configured, setting_status
from .send_request import acknowledge_send_request, list_send_requests, reschedule_send_request, send_request_is_due


CAPTURE_ONLY_HELP_TEXT = "仅录音转写模式：@我 发“开始录音 [时长]”并点选频道即开始录音；“结束录音”；“状态”。分析和出图已关闭。"

def _capture_only_command_allowed(command: str) -> bool:
    return (command in {"/oopz 帮助", "/oopz help", "/oopz 状态", "/oopz 离开", "取消", "退出", "cancel"}
            or command.isdigit()
            or re.fullmatch(r"/oopz\s*(?:开始|start)(?:\s+\d+(?:\.\d+)?\s*(?:秒|分钟|分|小时|时|h|m|s)?)?", command, re.I) is not None)


# Keep the operational settings that make sense for a Feishu-only group.
# Secret values are intentionally local-only.
FEISHU_SETTING_KEYS = frozenset({
    "OOPZ_CUTOFF_LOCAL_HOUR", "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS", "OOPZ_CHUNK_SECONDS",
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS", "OOPZ_LANGUAGE", "OOPZ_RETAIN_AUDIO",
    "OOPZ_RETENTION_HOURS", "OOPZ_DEVICE", "OOPZ_PROCESSING_DEADLINE_SECONDS", "OOPZ_POLL_INTERVAL_SECONDS",
    "OOPZ_MEMBERSHIP_REFRESH_SECONDS", "OOPZ_MEMBERSHIP_TIMEOUT_SECONDS",
    "OOPZ_CONNECTION_CHECK_SECONDS", "OOPZ_DISCONNECT_GRACE_SECONDS",
    "OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS", "OOPZ_RECONNECT_WINDOW_SECONDS",
    "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS", "OOPZ_RECONNECT_MAX_DELAY_SECONDS",
    "OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS",
    "OOPZ_ANALYZER_MODEL", "OOPZ_ANALYZER_TIMEOUT_SECONDS",
})

# These settings affect credentials, trust boundaries, storage paths, gateway
# identity, or low-level connectivity.  The status command may name them so an
# operator knows where to look, but it must never read or echo their values.
LOCAL_ONLY_SETTING_INFO: dict[str, tuple[str, str]] = {
    "OOPZ_ANALYZER_CLI": ("Qoder CLI 可执行文件路径", "未设置，分析不可用"),
    "OOPZ_ANALYZER_HOME": ("Qoder CLI 的 HOME（保存其登录）", "未设置，分析不可用"),
    "OOPZ_FEISHU_ADMIN_CHAT_ID": (
        "唯一接受控制指令的飞书群 ID",
        "未设置，启动后等待首次群邀请并自动绑定",
    ),
    "OOPZ_FEISHU_APP_ID": ("飞书机器人应用 ID", "未设置，网关无法启动"),
    "OOPZ_FEISHU_APP_SECRET": ("飞书机器人应用密钥", "未设置，网关无法启动"),
    "OOPZ_FEISHU_STATE_ROOT": (
        "飞书事件去重和控制状态目录",
        "未设置，使用 feishu_state",
    ),
    "OOPZ_LOGIN_PASSWORD": (
        "OOPZ 账号登录密码",
        "未设置，凭据登录可不需要",
    ),
    "OOPZ_LOGIN_PHONE": (
        "OOPZ 登录手机号",
        "未设置，凭据登录可不需要",
    ),
    "OOPZ_APP_VERSION": (
        "可选的 OOPZ 客户端版本覆盖值",
        "未设置，使用 SDK 默认值",
    ),
    "OOPZ_OUTPUT_ROOT": (
        "本地录音、转写和报告根目录",
        "未设置，使用 output",
    ),
}
LOCAL_ONLY_SETTING_KEYS = tuple(LOCAL_ONLY_SETTING_INFO)
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# The shared controller requires a non-empty sender policy.
# This placeholder grants nothing by itself: every Feishu sender is admitted in
# memory only after their message has passed the configured-group boundary.
_CONTROLLER_PLACEHOLDER_ID = "feishu-placeholder"


class FeishuChannel(Protocol):
    async def send(self, to: str, message: Any, opts: Any = None) -> Any: ...


@dataclass(frozen=True)
class FeishuGatewayConfig:
    app_id: str
    app_secret: str
    admin_chat_id: str
    state_root: Path
    controller_config: ControllerConfig
    capture_only: bool = False

    @classmethod
    def from_env(cls) -> "FeishuGatewayConfig":
        app_id = os.environ.get("OOPZ_FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("OOPZ_FEISHU_APP_SECRET", "").strip()
        chat_id = os.environ.get("OOPZ_FEISHU_ADMIN_CHAT_ID", "").strip()
        if not app_id or not app_secret or not chat_id:
            raise ValueError("OOPZ_FEISHU_APP_ID, OOPZ_FEISHU_APP_SECRET and OOPZ_FEISHU_ADMIN_CHAT_ID are required")
        capture_only = _env_bool("OOPZ_CAPTURE_ONLY")
        state_root = Path(os.environ.get("OOPZ_FEISHU_STATE_ROOT", "feishu_state"))
        output_root = Path(os.environ.get("OOPZ_OUTPUT_ROOT", "output"))
        if capture_only:
            names = ("OOPZ_CAPTURE_ONLY_STATE_ROOT", "OOPZ_CAPTURE_ONLY_OUTPUT_ROOT")
            roots = []
            for name in names:
                value = os.environ.get(name, "").strip()
                if not value or not Path(value).is_absolute():
                    raise ValueError(f"{name} requires an explicit absolute fresh directory")
                path = Path(value).resolve()
                if path.exists() and (not path.is_dir() or any(path.iterdir())):
                    raise ValueError(f"{name} must be fresh and empty; choose a new directory")
                roots.append(path)
            def overlaps(a: Path, b: Path) -> bool:
                return a == b or a in b.parents or b in a.parents
            if overlaps(*roots) or any(overlaps(p, old.resolve()) for p in roots for old in (state_root, output_root)):
                raise ValueError("capture-only roots must be separate from each other and normal state/output")
            state_root, output_root = roots
        else:
            QoderCli.from_env()      # fail at startup, not after the first recording, when the CLI is not configured
        # Keep recording settings in their existing OOPZ_* variables.
        controller = ControllerConfig(
            output_root=output_root, state_root=state_root,
            capture_only=capture_only,
            authorization=SenderPolicy(frozenset({_CONTROLLER_PLACEHOLDER_ID})),
            chunk_seconds=int(os.environ.get("OOPZ_CHUNK_SECONDS", "300")),
            cutoff_local_hour=int(os.environ.get("OOPZ_CUTOFF_LOCAL_HOUR", "4")),
            language=os.environ.get("OOPZ_LANGUAGE", "auto").strip(),
            retain_audio=True if capture_only else _env_bool("OOPZ_RETAIN_AUDIO"),
            transcription_repair_attempts=int(os.environ.get("OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS", "1")),
            processing_deadline_seconds=int(os.environ.get("OOPZ_PROCESSING_DEADLINE_SECONDS", "900")),
            retention_hours=int(os.environ.get("OOPZ_RETENTION_HOURS", "360")),
            poll_interval_seconds=float(os.environ.get("OOPZ_POLL_INTERVAL_SECONDS", "0.25")),
            membership_refresh_seconds=float(os.environ.get("OOPZ_MEMBERSHIP_REFRESH_SECONDS", "30")),
            membership_timeout_seconds=float(os.environ.get("OOPZ_MEMBERSHIP_TIMEOUT_SECONDS", "10")),
            empty_channel_timeout_seconds=float(os.environ.get("OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS", "300")),
            connection_check_seconds=float(os.environ.get("OOPZ_CONNECTION_CHECK_SECONDS", "2")),
            disconnect_grace_seconds=float(os.environ.get("OOPZ_DISCONNECT_GRACE_SECONDS", "15")),
            browser_operation_timeout_seconds=float(os.environ.get("OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS", "2")),
            reconnect_window_seconds=float(os.environ.get("OOPZ_RECONNECT_WINDOW_SECONDS", "300")),
            reconnect_initial_delay_seconds=float(os.environ.get("OOPZ_RECONNECT_INITIAL_DELAY_SECONDS", "1")),
            reconnect_max_delay_seconds=float(os.environ.get("OOPZ_RECONNECT_MAX_DELAY_SECONDS", "30")),
            reconnect_attempt_timeout_seconds=float(os.environ.get("OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS", "30")),
            device="cpu" if capture_only else os.environ.get("OOPZ_DEVICE", "cpu").strip(),
        )
        controller.validate()
        return cls(app_id, app_secret, chat_id, state_root, controller, capture_only)


class FeishuGateway:
    """Routes configured-group Feishu messages into the existing controller core.

    The controller uses the Feishu state directory. Outbound messages are
    always sent to the configured Feishu group.
    """

    def __init__(self, config: FeishuGatewayConfig, channel: FeishuChannel, *, controller: ControllerService | None = None):
        self.config = config
        self.channel = channel
        self.state_root = config.state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.controller = controller or ControllerService(config.controller_config)
        self._lock = asyncio.Lock()

    def _audit(self, kind: str, **fields: Any) -> None:
        path = self.state_root / "feishu_audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": _iso(), "kind": kind, **fields}, ensure_ascii=False) + "\n")

    @staticmethod
    def _console(kind: str, text: str) -> None:
        print(f"[飞书{kind}] {text}", flush=True)

    async def send_lifecycle_notice(self, text: str) -> None:
        """Send an explicit service-lifecycle notice to the configured group."""
        self._audit("lifecycle_notice", text=text)
        await self._send_text(text)

    def _event_path(self, message_id: str) -> Path:
        safe = "".join(ch for ch in message_id if ch.isalnum() or ch in "_-")
        if not safe or safe != message_id or len(safe) > 256:
            raise ValueError("invalid Feishu message id")
        return self.state_root / "feishu_events" / f"{safe}.json"

    def _allow_group_member_in_controller(self, open_id: str) -> str:
        """Grant a configured-group member a non-persistent controller identity."""
        surrogate = synthetic_controller_id(open_id)
        controller_config = getattr(self.controller, "config", None)
        if isinstance(controller_config, ControllerConfig) and surrogate not in controller_config.authorization.allowed_sender_ids:
            self.controller.config = replace(
                controller_config,
                authorization=SenderPolicy(
                    controller_config.authorization.allowed_sender_ids | frozenset({surrogate})
                ),
            )
        return surrogate

    def _session_dir(self, session_id: str) -> Path:
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("Session ID 格式无效")
        root = self.config.controller_config.output_root.resolve()
        target = (root / session_id).resolve()
        if target.parent != root:
            raise ValueError("Session 路径不在输出目录内")
        return target

    @staticmethod
    def _session_label(session_id: str) -> str:
        """2026-10-03_14-32-31_BJT -> "2026-10-03 14:32 录音"; anything else is shown as is."""
        match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})-\d{2}_BJT", session_id)
        return f"{match.group(1)} {match.group(2)}:{match.group(3)} 录音" if match else f"Session={session_id}"

    def _selection_card(self, *, title: str, hint: str, action: str, sessions: list[dict], style: str) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}},
            "elements": [
                {"tag": "markdown", "content": hint},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": self._session_label(str(item["session_id"]))[:80]},
                     "type": style, "value": {"action_id": f"{action}:{item['session_id']}"}}
                    for item in sessions
                ]},
            ],
        }

    def _digest_selection_card(self) -> dict[str, Any] | None:
        recent = find_recent_digests(self.config.controller_config.output_root, 7)
        return self._selection_card(title="选择要重发的图片", hint="选一条录音，把它的图片再发到本群。",
                                    action="digest:send", sessions=recent, style="primary") if recent else None

    def _pending_selection_card(self) -> dict[str, Any] | None:
        pending = find_pending_sessions(self.config.controller_config.output_root, self.controller.busy_sessions())
        return self._selection_card(title="选择要重新出图的录音", hint="选择后开始分析，完成后图片会发到本群。",
                                    action="pending:analyze", sessions=pending[:7], style="primary") if pending else None

    def _delete_selection_card(self) -> dict[str, Any] | None:
        root = self.config.controller_config.output_root
        merged = {str(i["session_id"]): float(i["modified_ts"]) for i in find_recent_digests(root, 7)}
        for item in find_pending_sessions(root)[:7]:
            merged.setdefault(str(item["session_id"]), float(item["modified_ts"]))
        ids = sorted(merged, key=merged.__getitem__, reverse=True)[:7]
        return self._selection_card(title="选择要删除的录音", hint="下一步还会要求确认；会永久删除本地录音、转写和图片。",
                                    action="delete:request", sessions=[{"session_id": i} for i in ids],
                                    style="danger") if ids else None

    def _delete_confirmation_card(self, session_id: str) -> dict[str, Any]:
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "确认删除录音"}},
            "elements": [
                {"tag": "markdown", "content": f"将永久删除 **{self._session_label(session_id)}** 的本地录音、转写和图片。\n\nSession ID：`{session_id}`"},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"tag": "plain_text", "content": "确认删除"}, "type": "danger", "value": {"action_id": f"delete:confirm:{session_id}"}},
                    {"tag": "button", "text": {"tag": "plain_text", "content": "取消"}, "value": {"action_id": f"delete:cancel:{session_id}"}},
                ]},
            ],
        }

    def _settings_status_text(self) -> str:
        values = setting_status()
        lines = [
            f"{key} = {values[key]}（{setting_description(key)}）"
            for key in sorted(FEISHU_SETTING_KEYS)
        ]
        local_lines = []
        for key, (purpose, unset_behavior) in LOCAL_ONLY_SETTING_INFO.items():
            status = "已设置" if setting_is_configured(key) else unset_behavior
            local_lines.append(f"{key}（{purpose}，{status}）")
        return (
            "当前可通过飞书调整的运行设置：\n"
            + "\n".join(lines)
            + "\n\n以下变量仅支持在本机 .env 修改（不显示具体值）：\n"
            + "\n".join(local_lines)
        )

    async def _controller_reply(self, command: str, open_id: str) -> dict[str, Any]:
        surrogate = self._allow_group_member_in_controller(open_id)
        raw = {
            "schema_version": "oopz.controller.inbound.v1",
            "message_id": str(uuid4()),
            "received_at": _iso(),
            "sender_id": surrogate,
            "chat_type": "private",
            "chat_id": surrogate,
            "text": command,
        }
        reply = await self.controller.handle(raw)
        return {"reply": reply, "controller_message_id": raw["message_id"]}

    async def _direct_command(self, command: str, open_id: str) -> dict[str, Any] | None:
        if command == "/oopz 重发图片":
            card = self._digest_selection_card()
            return {"card": card} if card else {"text": "还没有出过图的录音。"}
        if command == "/oopz 重新出图":
            card = self._pending_selection_card()
            return {"card": card} if card else {"text": "没有需要重新出图的录音。"}
        if command == "/oopz 删除录音":
            card = self._delete_selection_card()
            return {"card": card} if card else {"text": "没有可删除的录音。"}
        if command.startswith("/oopz 删除录音 "):
            session_id = command.removeprefix("/oopz 删除录音 ").strip()
            try:
                if not self._session_dir(session_id).is_dir():
                    return {"text": "找不到这条录音，未显示删除确认。"}
            except ValueError as error:
                return {"text": str(error)}
            return {"card": self._delete_confirmation_card(session_id)}
        if command == "/oopz 设置状态":
            return {"text": self._settings_status_text()}
        if command.startswith("/oopz 设置") or command.casefold().startswith("/oopz set"):
            match = re.match(r"^/oopz\s*(?:设置|set)\s*(.*)$", command, re.IGNORECASE)
            args = match.group(1).strip() if match else ""
            if not args or args.casefold() in {"状态", "status"}:
                return {"text": self._settings_status_text()}
            key = args.partition("=")[0].strip() if "=" in args else args.split(None, 1)[0]
            canonical_key = canonical_setting_key(key)
            if canonical_key not in FEISHU_SETTING_KEYS:
                return {"text": "该变量不能在飞书群内修改。请发送“设置状态”查看可用的非敏感运行参数。"}
            result = await self._controller_reply(command, open_id)
            return {"text": str(result["reply"].get("text") or "已处理。"), "controller_message_id": result["controller_message_id"]}
        return None

    async def handle_message(self, inbound: FeishuInbound) -> None:
        self._console("收信", f"chat={inbound.chat_id} sender={inbound.sender_open_id} text={inbound.text[:300]}")
        if inbound.chat_id != self.config.admin_chat_id:
            self._audit("rejected_message", message_id=inbound.message_id, chat_id=inbound.chat_id)
            return
        command = normalize_intent(inbound.text)
        if self.config.capture_only and (command is None or not _capture_only_command_allowed(command)):
            await self._send_text(CAPTURE_ONLY_HELP_TEXT)
            return
        self._console("识别", display_intent(command))
        if command is None:
            self._audit("ambiguous_message", message_id=inbound.message_id, sender_open_id=inbound.sender_open_id)
            await self._send_text("未能可靠识别该命令。请发送“帮助”查看飞书可用指令，或点击后续卡片中的选项。")
            return
        path = self._event_path(inbound.message_id)
        outbound: dict[str, Any]
        async with self._lock:
            if path.exists():
                self._audit("duplicate_message", message_id=inbound.message_id)
                return
            if command in {"/oopz 帮助", "/oopz help"}:
                _atomic_json(path, {"feishu_message_id": inbound.message_id, "handled_as": "feishu_help", "received_at": _iso()})
                self._audit("accepted_command", message_id=inbound.message_id, sender_open_id=inbound.sender_open_id, command="feishu_help")
                outbound = {"text": CAPTURE_ONLY_HELP_TEXT if self.config.capture_only else HELP_TEXT}
            else:
                outbound = await self._direct_command(command, inbound.sender_open_id) or {}
                if not outbound:
                    dispatched = await self._controller_reply(command, inbound.sender_open_id)
                    outbound = {"text": str(dispatched["reply"].get("text") or "已处理。"), "controller_message_id": dispatched["controller_message_id"]}
                _atomic_json(path, {
                    "feishu_message_id": inbound.message_id,
                    "controller_message_id": outbound.get("controller_message_id"),
                    "received_at": _iso(),
                })
                self._audit("accepted_command", message_id=inbound.message_id, sender_open_id=inbound.sender_open_id, command=command)
        try:
            if outbound.get("card"):
                await self._send_card(outbound["card"])
            else:
                await self._send_reply(str(outbound.get("text") or "已处理。"))
        except Exception:
            # Feishu redelivers unacked events; dropping the dedup mark lets the
            # retry re-run instead of being swallowed as a duplicate.
            path.unlink(missing_ok=True)
            raise

    async def handle_card_action(self, *, action_id: str, open_id: str, event_id: str, chat_id: str) -> None:
        if chat_id != self.config.admin_chat_id:
            self._audit("rejected_card_action", action_id=action_id, chat_id=chat_id)
            return
        if self.config.capture_only and not action_id.startswith("selection:"):
            await self._send_text(CAPTURE_ONLY_HELP_TEXT)
            return
        if action_id.startswith("selection:"):
            number = action_id.removeprefix("selection:")
            if number.isdigit():
                await self.handle_message(FeishuInbound(event_id, self.config.admin_chat_id, open_id, number))
            elif number == "cancel":
                await self.handle_message(FeishuInbound(event_id, self.config.admin_chat_id, open_id, "取消"))
            return
        if action_id.startswith(("digest:", "pending:", "delete:")):
            await self._handle_extended_card_action(action_id=action_id, open_id=open_id, event_id=event_id)

    async def _handle_extended_card_action(self, *, action_id: str, open_id: str, event_id: str) -> None:
        """Handle Feishu-native report, pending-analysis and delete cards."""
        try:
            path = self._event_path(event_id)
        except ValueError:
            self._audit("invalid_extended_card_event", action_id=action_id, event_id=event_id)
            return
        outbound: dict[str, Any]
        async with self._lock:
            if path.exists():
                self._audit("duplicate_card_action", action_id=action_id, event_id=event_id)
                return
            parts = action_id.split(":", 2)
            if len(parts) != 3 or not _SESSION_ID.fullmatch(parts[2]):
                self._audit("invalid_extended_card_action", action_id=action_id, open_id=open_id)
                return
            family, action, session_id = parts
            try:
                session_dir = self._session_dir(session_id)
            except ValueError as error:
                outbound = {"text": str(error)}
            else:
                outbound = await self._extended_card_outbound(
                    family=family, action=action, session_id=session_id, session_dir=session_dir, open_id=open_id,
                )
            _atomic_json(path, {
                "feishu_card_event_id": event_id,
                "action_id": action_id,
                "sender_open_id": open_id,
                "received_at": _iso(),
            })
            self._audit("accepted_card_action", action_id=action_id, sender_open_id=open_id)
        try:
            if outbound.get("card"):
                await self._send_card(outbound["card"])
            elif outbound.get("image_path"):
                await self._send_image(Path(str(outbound["image_path"])))
                if outbound.get("file_path"):
                    await self._send_file(Path(str(outbound["file_path"])))
            else:
                await self._send_text(str(outbound.get("text") or "已处理。"))
        except Exception:
            # See handle_message: allow redelivery of a click whose reply was lost.
            path.unlink(missing_ok=True)
            raise

    async def _extended_card_outbound(self, *, family: str, action: str, session_id: str, session_dir: Path, open_id: str) -> dict[str, Any]:
        if family == "digest" and action == "send":
            png, md = digest_png(session_dir), digest_md(session_dir)
            if not png.is_file():
                return {"text": "这条录音还没有图片。"}
            return {"image_path": str(png)} | ({"file_path": str(md)} if md.is_file() else {})

        if family == "pending" and action == "analyze":
            if not session_dir.is_dir():
                return {"text": "找不到这条录音，无法重新出图。"}
            if not self.controller._start_analysis_and_deliver(session_dir):
                return {"text": f"{self._session_label(session_id)} 正在分析，发送“状态”可看进度。"}
            return {"text": f"已开始重新出图：{self._session_label(session_id)}，完成后图片会发到本群。"}

        if family == "delete":
            if not session_dir.is_dir():
                return {"text": "找不到这条录音，未执行删除。"}
            if action == "request":
                return {"card": self._delete_confirmation_card(session_id)}
            if action == "cancel":
                return {"text": "已取消删除。"}
            if action != "confirm":
                return {"text": "未知的删除操作。"}
            try:
                self.controller._delete_session(session_id)
            except ValueError as error:
                return {"text": f"删除失败：{error}"}
            self._audit("session_deleted", session_id=session_id, deleted_by_open_id=open_id)
            return {"text": f"已删除 {self._session_label(session_id)} 的本地录音、转写和图片。"}

        return {"text": "未知的卡片操作。"}

    async def drain_outbox(self) -> int:
        if self.config.capture_only:
            return 0
        sent = 0
        for item in list_send_requests(self.state_root, statuses={"pending"}):
            if not send_request_is_due(item):
                continue
            try:
                if item.get("image_path"):
                    await self._send_image(Path(str(item["image_path"])))
                elif item.get("file_path"):
                    await self._send_file(Path(str(item["file_path"])))
                else:
                    await self._send_text(str(item.get("text") or ""))
                acknowledge_send_request(self.state_root, str(item["send_request_id"]), status="sent")
                sent += 1
            except Exception as error:
                reschedule_send_request(self.state_root, str(item["send_request_id"]), error=f"Feishu: {type(error).__name__}: {error}")
                self._audit("outbound_retry", send_request_id=str(item["send_request_id"]), error=f"{type(error).__name__}: {error}")
        return sent

    @staticmethod
    def _expired_session_ids(output_root: Path, *, now: datetime | None = None) -> list[str]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expired: list[str] = []
        for session_dir in output_root.iterdir() if output_root.is_dir() else ():
            if not session_dir.is_dir() or not _SESSION_ID.fullmatch(session_dir.name):
                continue
            lifecycle_path = session_dir / "lifecycle.json"
            if not lifecycle_path.is_file():
                continue
            try:
                lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
                raw = str(lifecycle.get("delete_after") or "")
                delete_after = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if delete_after.tzinfo is None:
                    continue
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if delete_after.astimezone(timezone.utc) <= current:
                expired.append(session_dir.name)
        return sorted(expired)

    def _purge_stale_state_files(self) -> int:
        """Delete long-finished control-plane files past the session retention horizon.

        Replies, dedup markers and finished send requests only need to outlive
        Feishu's redelivery window; pending send requests are never removed.
        """
        cutoff = time.time() - self.config.controller_config.retention_hours * 3600
        removed = 0
        for folder in ("replies", "feishu_events", "send_requests"):
            root = self.state_root / folder
            if not root.is_dir():
                continue
            for path in root.glob("*.json"):
                try:
                    if not path.is_file() or path.stat().st_mtime >= cutoff:
                        continue
                    if folder == "send_requests":
                        payload = _read_json_or_none(path)
                        if isinstance(payload, dict) and payload.get("status") not in {"sent", "failed", "cancelled"}:
                            continue
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    async def cleanup_expired_sessions(self) -> int:
        """Delete local sessions past their retention time, then old control-plane files."""
        if self.config.capture_only:
            return 0
        removed = 0
        async with self._lock:
            for session_id in self._expired_session_ids(self.config.controller_config.output_root):
                try:
                    self.controller._delete_session(session_id)
                except Exception as error:
                    # A locked file must not kill the gateway loop; the session stays and is retried next minute.
                    self._audit("retention_local_delete_failed", session_id=session_id, error=f"{type(error).__name__}: {error}")
                    continue
                self._audit("retention_session_deleted", session_id=session_id)
                removed += 1
            removed += self._purge_stale_state_files()
        return removed

    async def _send_text(self, text: str) -> None:
        self._console("发信", text[:500])
        result = await self.channel.send(self.config.admin_chat_id, {"text": text})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu send failed"))

    async def _send_reply(self, text: str) -> None:
        choices = re.findall(r"(?m)^(\d+)\.\s+(.+)$", text)
        if not choices or "请选择" not in text:
            await self._send_text(text)
            return
        # The controller's text response contains a numbered list followed by a
        # reply hint.  The list becomes buttons, so remove its surrounding blank
        # lines instead of leaving a large empty Markdown area in the card.
        prompt_lines: list[str] = []
        for line in text.splitlines():
            if re.fullmatch(r"\d+\.\s+.+", line):
                continue
            if line.strip().startswith("点选按钮"):
                continue
            elif line.strip():
                prompt_lines.append(line.strip())
        actions = [
            {"tag": "button", "text": {"tag": "plain_text", "content": label[:80]}, "value": {"action_id": f"selection:{number}"}}
            for number, label in choices[:20]
        ]
        actions.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "取消选择"},
            "value": {"action_id": "selection:cancel"},
        })
        elements: list[dict[str, Any]] = [
            {"tag": "markdown", "content": "\n".join(prompt_lines)},
            {"tag": "action", "actions": actions},
        ]
        await self._send_card({
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "OOPZ 请选择录音目标"}},
            "elements": elements,
        })

    async def _send_file(self, path: Path) -> None:
        self._console("发件", f"文件={path.name}")
        result = await self.channel.send(self.config.admin_chat_id, {"file": {"source": str(path), "file_name": path.name}})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu file send failed"))

    async def _send_image(self, path: Path) -> None:
        self._console("发图", path.name)
        result = await self.channel.send(self.config.admin_chat_id, {"image": {"source": str(path)}})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu image send failed"))

    async def _send_card(self, card: dict[str, Any]) -> None:
        header = (((card.get("header") or {}).get("title") or {}).get("content") or "操作卡片")
        self._console("发卡", str(header))
        result = await self.channel.send(self.config.admin_chat_id, {"card": card})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu card send failed"))
