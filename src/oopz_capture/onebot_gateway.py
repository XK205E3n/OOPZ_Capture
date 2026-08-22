from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, uuid4, uuid5

from .qq_outbox import (
    acknowledge_delivery,
    delivery_is_due,
    expedite_pending_deliveries,
    list_outbox,
    reschedule_delivery,
)
from .qq_send_request import (
    acknowledge_send_request,
    enqueue_send_request,
    list_send_requests,
    expedite_pending_send_requests,
    reschedule_send_request,
    send_request_is_due,
)
from .qq_protocol import INBOUND_SCHEMA, parse_command
from .qq_controller import HELP_TEXT
from .workflow import _is_reparse_point


LOGGER = logging.getLogger(__name__)
GATEWAY_STATE_SCHEMA = "oopz.onebot.gateway.state.v1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _is_transient_qq_error(error: BaseException) -> bool:
    text = str(error).casefold()
    return any(marker in text for marker in (
        "retcode=1200", "timeout", "connection", "websocket", "network",
        "serviceandmethod", "sendmsg",
    ))


def _iso(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _qq_identifier(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text.isascii() or not text.isdigit() or not 5 <= len(text) <= 20:
        raise ValueError(f"{field_name} must contain 5 to 20 ASCII digits")
    return text


def _positive_float(value: str, field_name: str, *, minimum: float, maximum: float) -> float:
    parsed = float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field_name} must be {minimum} to {maximum}")
    return parsed


def _safe_onebot_text(value: Any) -> str:
    """Return text that can safely be encoded in a OneBot JSON text segment.

    QQ report text originates in model output and may occasionally contain a
    Unicode surrogate or an invisible control character. Those are not legal
    UTF-8 strings and can make NapCat's protobuf telemetry complain while it
    serializes a send operation. Normal newlines and Unicode are preserved.
    """
    source = str(value or "")
    safe = "".join(
        character
        if (
            character in "\n\r\t"
            or (ord(character) >= 0x20 and not 0x7F <= ord(character) <= 0x9F
                and not 0xD800 <= ord(character) <= 0xDFFF)
        )
        else "\uFFFD"
        for character in source
    )
    return safe.encode("utf-8", errors="strict").decode("utf-8")


def _onebot_text_message(value: Any) -> list[dict[str, dict[str, str]]]:
    """Create an explicit OneBot v11 text segment instead of a raw message."""
    return [{"type": "text", "data": {"text": _safe_onebot_text(value)}}]


@dataclass(frozen=True)
class OneBotGatewayConfig:
    websocket_url: str
    access_token: str
    admin_qq: str
    report_group_qq: str
    report_friend_qq: str
    state_root: Path
    report_group_enabled: bool = False
    controller_reply_timeout_seconds: float = 30.0
    action_timeout_seconds: float = 15.0
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0
    send_failure_cooldown_seconds: float = 30.0
    allow_remote_websocket: bool = False

    @classmethod
    def from_env(cls) -> "OneBotGatewayConfig":
        allow_remote = os.environ.get("OOPZ_ONEBOT_ALLOW_REMOTE", "false").strip().lower()
        if allow_remote not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise ValueError("OOPZ_ONEBOT_ALLOW_REMOTE must be true or false")
        config = cls(
            websocket_url=os.environ.get("OOPZ_ONEBOT_WS_URL", "ws://127.0.0.1:3001").strip(),
            access_token=os.environ.get("OOPZ_ONEBOT_ACCESS_TOKEN", "").strip(),
            admin_qq=os.environ.get("OOPZ_QQ_ADMIN_ID", "").strip(),
            report_group_qq=os.environ.get("OOPZ_QQ_REPORT_GROUP_ID", "").strip(),
            report_friend_qq=os.environ.get("OOPZ_QQ_REPORT_FRIEND_ID", "").strip(),
            report_group_enabled=os.environ.get("OOPZ_QQ_REPORT_GROUP_ENABLED", "false")
            .strip().lower() in {"true", "1", "yes", "on"},
            state_root=Path(os.environ.get("OOPZ_QQ_STATE_ROOT", "controller_state")),
            controller_reply_timeout_seconds=_positive_float(
                os.environ.get("OOPZ_ONEBOT_CONTROLLER_TIMEOUT_SECONDS", "30"),
                "OOPZ_ONEBOT_CONTROLLER_TIMEOUT_SECONDS", minimum=1, maximum=300,
            ),
            action_timeout_seconds=_positive_float(
                os.environ.get("OOPZ_ONEBOT_ACTION_TIMEOUT_SECONDS", "15"),
                "OOPZ_ONEBOT_ACTION_TIMEOUT_SECONDS", minimum=1, maximum=120,
            ),
            reconnect_min_seconds=_positive_float(
                os.environ.get("OOPZ_ONEBOT_RECONNECT_MIN_SECONDS", "1"),
                "OOPZ_ONEBOT_RECONNECT_MIN_SECONDS", minimum=0.25, maximum=60,
            ),
            reconnect_max_seconds=_positive_float(
                os.environ.get("OOPZ_ONEBOT_RECONNECT_MAX_SECONDS", "30"),
                "OOPZ_ONEBOT_RECONNECT_MAX_SECONDS", minimum=1, maximum=300,
            ),
            send_failure_cooldown_seconds=_positive_float(
                os.environ.get("OOPZ_ONEBOT_SEND_FAILURE_COOLDOWN_SECONDS", "30"),
                "OOPZ_ONEBOT_SEND_FAILURE_COOLDOWN_SECONDS", minimum=5, maximum=300,
            ),
            allow_remote_websocket=allow_remote in {"true", "1", "yes", "on"},
        )
        config.validate()
        return config

    def validate(self) -> None:
        parsed = urlparse(self.websocket_url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise ValueError("OOPZ_ONEBOT_WS_URL must be a ws:// or wss:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OOPZ_ONEBOT_WS_URL may not contain credentials, query, or fragment")
        if not self.allow_remote_websocket and parsed.hostname.casefold() not in _LOOPBACK_HOSTS:
            raise ValueError(
                "OneBot WebSocket must use loopback; set OOPZ_ONEBOT_ALLOW_REMOTE=true only "
                "when a protected private network is intentionally configured"
            )
        if len(self.access_token) < 24:
            raise ValueError("OOPZ_ONEBOT_ACCESS_TOKEN must contain at least 24 characters")
        _qq_identifier(self.admin_qq, "OOPZ_QQ_ADMIN_ID")
        _qq_identifier(self.report_group_qq, "OOPZ_QQ_REPORT_GROUP_ID")
        _qq_identifier(self.report_friend_qq, "OOPZ_QQ_REPORT_FRIEND_ID")
        if self.reconnect_max_seconds < self.reconnect_min_seconds:
            raise ValueError("reconnect maximum must not be smaller than reconnect minimum")
        root = self.state_root.resolve()
        if root.exists() and (not root.is_dir() or _is_reparse_point(root)):
            raise ValueError("OOPZ_QQ_STATE_ROOT must be a real directory, not a link")

    def public_summary(self) -> dict[str, Any]:
        parsed = urlparse(self.websocket_url)
        return {
            "status": "valid",
            "websocket": f"{parsed.scheme}://{parsed.hostname}:{parsed.port or ('443' if parsed.scheme == 'wss' else '80')}",
            "loopback_only": not self.allow_remote_websocket,
            "admin_qq_masked": "*" * max(0, len(self.admin_qq) - 4) + self.admin_qq[-4:],
            "report_group_masked": "*" * max(0, len(self.report_group_qq) - 4) + self.report_group_qq[-4:],
            "report_friend_masked": "*" * max(0, len(self.report_friend_qq) - 4) + self.report_friend_qq[-4:],
            "report_group_enabled": self.report_group_enabled,
            "state_root": str(self.state_root.resolve()),
            "token_configured": True,
        }


@dataclass(frozen=True)
class EventDecision:
    kind: str
    inbound: dict[str, Any] | None = None


def _event_time(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return _iso()
    try:
        return _iso(datetime.fromtimestamp(timestamp, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        return _iso()


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, list):
        return ""
    text_parts: list[str] = []
    for segment in value:
        if not isinstance(segment, dict) or segment.get("type") != "text":
            continue
        data = segment.get("data")
        if isinstance(data, dict) and isinstance(data.get("text"), str):
            text_parts.append(data["text"])
    return "".join(text_parts).strip()


def classify_event(
    event: dict[str, Any],
    *,
    admin_qqs: frozenset[str] | None = None,
    admin_qq: str | None = None,
) -> EventDecision:
    """Classify without inspecting group-message content.

    NapCat may transport group events on the same OneBot connection. They are
    rejected before the message/raw_message fields are accessed and are never
    written to disk.
    """
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return EventDecision("ignored_non_message")
    message_type = str(event.get("message_type") or "")
    if message_type == "group":
        return EventDecision("ignored_group")
    if message_type != "private":
        return EventDecision("ignored_non_private")
    sender_id = str(event.get("user_id") or "").strip()
    if admin_qqs is None:
        admin_qqs = frozenset({admin_qq}) if admin_qq else frozenset()
    if sender_id not in admin_qqs:
        return EventDecision("ignored_unauthorized_private")
    text = _message_text(event.get("message")) or _message_text(event.get("raw_message"))
    if not text:
        return EventDecision("ignored_empty_private")
    if len(text) > 200:
        return EventDecision("rejected_too_long")
    onebot_message_id = str(event.get("message_id") or "").strip()
    self_id = str(event.get("self_id") or "").strip()
    if not onebot_message_id:
        return EventDecision("ignored_missing_message_id")
    stable = uuid5(
        NAMESPACE_URL,
        f"oopz-onebot-v11:{self_id}:{sender_id}:{onebot_message_id}",
    )
    return EventDecision("accepted", {
        "schema_version": INBOUND_SCHEMA,
        "message_id": str(stable),
        "received_at": _event_time(event.get("time")),
        "sender_id": sender_id,
        "chat_type": "private",
        "chat_id": sender_id,
        "text": text,
    })


class ReplyBridge(Protocol):
    async def submit(self, message: dict[str, Any]) -> dict[str, Any]: ...


class ControllerDirectoryBridge:
    def __init__(self, state_root: Path, *, timeout_seconds: float = 30.0):
        self.state_root = state_root.resolve()
        self.timeout_seconds = timeout_seconds
        self.inbox = self.state_root / "inbox"
        self.replies = self.state_root / "replies"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.replies.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.state_root) or _is_reparse_point(self.inbox) or _is_reparse_point(self.replies):
            raise ValueError("controller bridge directories may not be links or reparse points")

    async def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        message_id = str(message["message_id"])
        reply_path = self.replies / f"{message_id}.json"
        if reply_path.is_file():
            if _is_reparse_point(reply_path):
                raise ValueError("unsafe controller reply")
            return json.loads(reply_path.read_text(encoding="utf-8"))
        inbox_path = self.inbox / f"{message_id}.json"
        if not inbox_path.exists():
            _atomic_json(inbox_path, message)
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            if reply_path.is_file():
                if _is_reparse_point(reply_path):
                    raise ValueError("unsafe controller reply")
                reply = json.loads(reply_path.read_text(encoding="utf-8"))
                if str(reply.get("message_id") or "") != message_id:
                    raise ValueError("controller reply message_id mismatch")
                return reply
            await asyncio.sleep(0.1)
        raise TimeoutError(
            "QQ 控制器未在规定时间内回复；请确认 oopz-qq-controller serve 正在运行。"
        )


class DiagnosticEchoBridge:
    """A QQ-only test bridge that cannot call OOPZ or DeepSeek."""

    async def submit(self, message: dict[str, Any]) -> dict[str, Any]:
        try:
            command = parse_command(str(message.get("text") or ""))
            detail = f"已识别指令：{command}。"
        except ValueError as error:
            detail = str(error)
        return {
            "schema_version": "oopz.qq.reply.v1",
            "message_id": str(message["message_id"]),
            "status": "completed",
            "text": (
                "QQ 网关隔离测试正常。\n"
                f"{detail}\n"
                "当前为 diagnostic-echo 模式，不会登录 OOPZ、录音、转写或调用 DeepSeek。"
            ),
        }


class OneBotActionError(RuntimeError):
    pass


class OneBotRPC:
    def __init__(self, websocket: Any, *, action_timeout_seconds: float):
        self.websocket = websocket
        self.action_timeout_seconds = action_timeout_seconds
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._send_lock = asyncio.Lock()

    async def action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        echo = str(uuid4())
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[echo] = future
        try:
            payload = {"action": action, "params": params, "echo": echo}
            async with self._send_lock:
                await self.websocket.send(json.dumps(payload, ensure_ascii=False))
            response = await asyncio.wait_for(future, timeout=self.action_timeout_seconds)
        finally:
            self._pending.pop(echo, None)
        if response.get("status") != "ok" or int(response.get("retcode", -1)) != 0:
            raise OneBotActionError(
                f"OneBot action {action} failed: retcode={response.get('retcode')} "
                f"message={str(response.get('message') or response.get('wording') or '')[:300]}"
            )
        data = response.get("data")
        return data if isinstance(data, dict) else {"value": data}

    async def notify(self, action: str, params: dict[str, Any]) -> None:
        """Send a best-effort action without waiting for an RPC reply."""
        payload = {"action": action, "params": params, "echo": str(uuid4())}
        async with self._send_lock:
            await self.websocket.send(json.dumps(payload, ensure_ascii=False))

    def accept_response(self, payload: dict[str, Any]) -> bool:
        echo = str(payload.get("echo") or "")
        if not echo:
            return False
        future = self._pending.get(echo)
        if future is None or future.done():
            return True
        future.set_result(payload)
        return True

    def fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)


@dataclass
class GatewayCounters:
    admin_private_accepted: int = 0
    replies_sent: int = 0
    reports_sent: int = 0
    friend_requests_approved: int = 0
    duplicate_events: int = 0
    group_events_discarded: int = 0
    unauthorized_private_discarded: int = 0
    other_events_discarded: int = 0
    failures: int = 0


@dataclass
class GatewayState:
    mode: str
    connected: bool = False
    started_at: str = field(default_factory=_iso)
    updated_at: str = field(default_factory=_iso)
    last_connected_at: str | None = None
    last_event_at: str | None = None
    last_reply_at: str | None = None
    last_error: str | None = None
    qq_send_available: bool | None = None
    last_qq_health_at: str | None = None
    last_qq_health_error: str | None = None
    consecutive_send_failures: int = 0
    last_send_failure_at: str | None = None
    counters: GatewayCounters = field(default_factory=GatewayCounters)
    schema_version: str = GATEWAY_STATE_SCHEMA

    def as_json(self) -> dict[str, Any]:
        value = asdict(self)
        value["updated_at"] = _iso()
        return value


class OneBotGateway:
    def __init__(self, config: OneBotGatewayConfig, bridge: ReplyBridge, *, mode: str = "controller"):
        config.validate()
        if mode not in {"controller", "diagnostic_echo"}:
            raise ValueError("unsupported gateway mode")
        self.config = config
        self.bridge = bridge
        self.state_root = config.state_root.resolve()
        self.delivery_root = self.state_root / "onebot_deliveries"
        self.state_path = self.state_root / "onebot_gateway.json"
        self.delivery_root.mkdir(parents=True, exist_ok=True)
        extra_admins = {
            item.strip() for item in os.environ.get("OOPZ_QQ_ADMIN_IDS", "").split(",") if item.strip().isdigit()
        }
        self._admin_set = {config.admin_qq} | extra_admins
        self._admins_mtime: int | None = None
        self._current_admin_ids()
        if _is_reparse_point(self.state_root) or _is_reparse_point(self.delivery_root):
            raise ValueError("gateway state directories may not be links or reparse points")
        self.state = GatewayState(mode=mode)
        self._event_tasks: set[asyncio.Task[None]] = set()
        # NapCat may redeliver the same event before the first reply has been
        # persisted.  Keep an in-memory guard in addition to the durable marker
        # so concurrent tasks cannot send two administrator replies.
        self._inflight: set[str] = set()
        self._startup_announced = False
        self._startup_lifecycle_marker = self.state_root / "gateway_startup_lifecycle.txt"
        self._startup_lifecycle = self._read_startup_lifecycle()
        self._last_qq_health_monotonic = 0.0
        self._send_blocked_until_monotonic = 0.0
        self._persist_state()

    def _mark_send_failure(self, error: BaseException) -> None:
        """Record a real QQ message-channel failure, not just WebSocket health."""
        detail = f"{type(error).__name__}: {str(error)[:500]}"
        self.state.consecutive_send_failures += 1
        self.state.last_send_failure_at = _iso()
        self.state.qq_send_available = False
        self.state.last_qq_health_error = f"QQ message delivery failed: {detail}"
        self.state.last_error = self.state.last_qq_health_error
        self._send_blocked_until_monotonic = (
            asyncio.get_running_loop().time() + self.config.send_failure_cooldown_seconds
        )

    def _mark_send_success(self) -> None:
        self.state.consecutive_send_failures = 0
        self.state.qq_send_available = True
        self.state.last_qq_health_error = None
        self.state.last_error = None
        self._send_blocked_until_monotonic = 0.0

    def _read_startup_lifecycle(self) -> str:
        try:
            value = self._startup_lifecycle_marker.read_text(encoding="ascii").strip().lower()
            if value in {"startup", "restarted"}:
                return value
        except OSError:
            pass
        return "startup"

    def _acknowledge_startup_lifecycle(self) -> None:
        if self._startup_lifecycle == "restarted":
            try:
                self._startup_lifecycle_marker.unlink(missing_ok=True)
            except OSError:
                LOGGER.warning("could not clear gateway startup lifecycle marker", exc_info=True)

    def _current_admin_ids(self) -> frozenset[str]:
        path = self.state_root / "admins.json"
        try:
            stamp = path.stat().st_mtime_ns if path.is_file() else None
        except OSError:
            stamp = None
        if stamp is not None and stamp != self._admins_mtime:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                ids = value.get("admin_ids") if isinstance(value, dict) else None
                if isinstance(ids, list):
                    self._admin_set = {self.config.admin_qq} | {str(i) for i in ids if str(i).isdigit()}
                    self._admins_mtime = stamp
            except (ValueError, OSError):
                pass
        return frozenset(self._admin_set)

    def _persist_state(self) -> None:
        _atomic_json(self.state_path, self.state.as_json())

    async def _notify_admins(self, rpc: OneBotRPC, text: str) -> None:
        for admin_id in sorted(self._current_admin_ids(), key=int):
            payload = {
                "user_id": int(admin_id),
                "message": _onebot_text_message(text),
                "auto_escape": True,
            }
            await rpc.notify("send_private_msg", payload)

    def _queue_admin_lifecycle_notice(self, text: str, *, source: str) -> None:
        for admin_id in sorted(self._current_admin_ids(), key=int):
            enqueue_send_request(
                self.state_root,
                target_type="private",
                target_id=admin_id,
                text=text,
                source=source,
            )

    def _delivery_path(self, message_id: str) -> Path:
        path = (self.delivery_root / f"{message_id}.json").resolve()
        if path.parent != self.delivery_root:
            raise ValueError("unsafe OneBot delivery path")
        return path

    async def handle_event(self, event: dict[str, Any], rpc: OneBotRPC) -> None:
        self.state.last_event_at = _iso()
        if event.get("post_type") == "request" and str(event.get("request_type") or "") == "friend":
            flag = str(event.get("flag") or "")
            if flag:
                try:
                    await rpc.action("set_friend_add_request", {"flag": flag, "approve": True})
                    self.state.counters.friend_requests_approved += 1
                except Exception as error:
                    self.state.counters.failures += 1
                    self.state.last_error = f"friend request: {type(error).__name__}: {str(error)[:500]}"
            self._persist_state()
            return
        decision = classify_event(event, admin_qqs=self._current_admin_ids())
        if decision.kind == "ignored_group":
            self.state.counters.group_events_discarded += 1
            self._persist_state()
            return
        if decision.kind == "ignored_unauthorized_private":
            self.state.counters.unauthorized_private_discarded += 1
            self._persist_state()
            return
        if decision.kind != "accepted" or decision.inbound is None:
            self.state.counters.other_events_discarded += 1
            self._persist_state()
            return
        inbound = decision.inbound
        message_id = str(inbound["message_id"])
        delivery_path = self._delivery_path(message_id)
        if delivery_path.is_file() or message_id in self._inflight:
            self.state.counters.duplicate_events += 1
            self._persist_state()
            return
        self._inflight.add(message_id)
        self.state.counters.admin_private_accepted += 1
        try:
            reply = await self.bridge.submit(inbound)
            text = str(reply.get("text") or "").strip()
            if not text:
                raise ValueError("controller returned an empty reply")
            response = await rpc.action("send_private_msg", {
                "user_id": int(inbound["sender_id"]),
                "message": _onebot_text_message(text),
                "auto_escape": True,
            })
            _atomic_json(delivery_path, {
                "schema_version": "oopz.onebot.delivery.v1",
                "normalized_message_id": message_id,
                "onebot_message_id": str(event.get("message_id") or ""),
                "sent_at": _iso(),
                "response_message_id": str(response.get("message_id") or ""),
            })
            self.state.counters.replies_sent += 1
            self.state.last_reply_at = _iso()
            self._mark_send_success()
        except Exception as error:
            self.state.counters.failures += 1
            self._mark_send_failure(error)
            LOGGER.error("failed to handle authorized private QQ command: %s", self.state.last_error)
            try:
                await rpc.action("send_private_msg", {
                    "user_id": int(inbound["sender_id"]),
                    "message": _onebot_text_message(f"指令处理失败。原因：{str(error)[:300]}"),
                    "auto_escape": True,
                })
            except Exception:
                LOGGER.exception("failed to deliver private failure feedback")
        finally:
            self._inflight.discard(message_id)
            self._persist_state()

    async def _deliver_send_requests(self, rpc: OneBotRPC) -> None:
        requests = list_send_requests(self.state_root, statuses={"pending"})
        for item in requests:
            if not send_request_is_due(item):
                continue
            request_id = str(item.get("send_request_id") or "")
            target_type = str(item.get("target_type") or "")
            target_id = str(item.get("target_id") or "")
            message = str(item.get("text") or "").strip()
            file_path = str(item.get("file_path") or "").strip()
            notify_admin_id = str(item.get("notify_admin_id") or "").strip()

            def queue_attachment_failure(reason: str) -> None:
                is_forward = bool(notify_admin_id)
                recipient_id = notify_admin_id if is_forward else target_id
                target_label = ("群" if target_type == "group" else "好友") + f" {target_id}"
                prefix = "报告转发失败" if is_forward else "附件发送失败"
                enqueue_send_request(
                    self.state_root,
                    target_type="private",
                    target_id=recipient_id,
                    text=f"{prefix}，无法发送到{target_label}。原因：{reason[:220]}",
                    source="qq_forward_failed" if is_forward else "qq_attachment_failed",
                )

            if not request_id or (not message and not file_path):
                continue
            if file_path:
                attachment = Path(file_path)
                if not attachment.is_file() or _is_reparse_point(attachment):
                    acknowledge_send_request(
                        self.state_root, request_id, status="failed",
                        error="attachment file is missing or unsafe",
                    )
                    queue_attachment_failure(f"{attachment.name or '附件'} 文件不存在或不可安全读取")
                    self.state.counters.failures += 1
                    self._persist_state()
                    LOGGER.warning("attachment send request %s failed before delivery: missing or unsafe file", request_id)
                    continue
            try:
                if file_path:
                    # NapCat exposes dedicated ordinary-file actions. Using a
                    # ``file`` message segment routes through QQ's rich-message
                    # path and, on current Windows QQ builds, emits spurious
                    # OpenTelemetry protobuf UTF-8 errors even when delivery
                    # succeeds. The upload actions are the native OneBot
                    # extension for PDF/Markdown attachments.
                    upload_params = {
                        "file": file_path.replace("\\", "/"),
                        "name": Path(file_path).name,
                    }
                    if target_type == "group":
                        await rpc.action(
                            "upload_group_file",
                            {"group_id": int(target_id), **upload_params},
                        )
                    else:
                        await rpc.action(
                            "upload_private_file",
                            {"user_id": int(target_id), **upload_params},
                        )
                elif target_type == "group":
                    await rpc.action(
                        "send_group_msg",
                        {"group_id": int(target_id), "message": _onebot_text_message(message)},
                    )
                else:
                    await rpc.action(
                        "send_private_msg",
                        {"user_id": int(target_id), "message": _onebot_text_message(message)},
                    )
            except Exception as error:
                retry = reschedule_send_request(
                    self.state_root, request_id, error=str(error)[:500],
                    # Attachments have a finite retry budget. A persistent file
                    # failure must eventually be visible to the administrator;
                    # ordinary text notices may continue through a short QQ
                    # outage without being dropped.
                    retry_indefinitely=not bool(file_path) and _is_transient_qq_error(error),
                )
                if file_path and retry["status"] == "failed":
                    filename = Path(file_path).name or "附件"
                    queue_attachment_failure(
                        f"{filename} 已重试 {retry['attempt_count']} 次仍失败；"
                        f"{type(error).__name__}: {str(error)[:180]}"
                    )
                self.state.counters.failures += 1
                self._mark_send_failure(error)
                self._persist_state()
                LOGGER.warning(
                    "send request %s attempt %s/%s failed; status=%s next=%s: %s",
                    request_id, retry["attempt_count"], retry["max_attempts"],
                    retry["status"], retry.get("next_attempt_at"), error,
                )
                continue
            acknowledge_send_request(self.state_root, request_id, status="sent")
            self.state.counters.reports_sent += 1
            self._mark_send_success()
            self._persist_state()
            LOGGER.info(
                "QQ send request delivered: source=%s target=%s:%s file=%s",
                item.get("source") or "unknown", target_type, target_id, bool(file_path),
            )
    async def _outbox_loop(self, rpc: OneBotRPC) -> None:
        while True:
            try:
                # Probe the actual QQ account even while the outbox is idle.
                # NapCat can keep its WebSocket server alive after
                # KickedOffLine, so port/WebSocket health alone is insufficient
                # and previously left the watchdog blind until the next send.
                qq_available = await self._refresh_qq_health(rpc)
                if self._has_pending_deliveries() and qq_available:
                    await self._deliver_outbox(rpc)
                    await self._deliver_send_requests(rpc)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state.last_error = f"outbox delivery: {type(error).__name__}: {str(error)[:500]}"
                self._persist_state()
                LOGGER.warning("outbox delivery pass failed: %s", error)
            await asyncio.sleep(2.0)

    def _has_pending_deliveries(self) -> bool:
        return bool(
            list_send_requests(self.state_root, statuses={"pending"})
            or list_outbox(self.state_root, statuses={"pending", "blocked"})
        )

    async def _deliver_outbox(self, rpc: OneBotRPC) -> None:
        records = list_outbox(self.state_root, statuses={"pending", "blocked"})
        if not records:
            return
        recipients: list[tuple[str, str]] = []
        for chat_type, chat_id in (
            ("private", self.config.admin_qq),
            ("private", self.config.report_friend_qq),
        ):
            if (chat_type, chat_id) not in recipients:
                recipients.append((chat_type, chat_id))
        if self.config.report_group_enabled:
            recipients.append(("group", self.config.report_group_qq))
        for record in records:
            if record.get("status") == "pending" and not delivery_is_due(record):
                continue
            message_id = str(record.get("message_id") or "")
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            text = str(message.get("text") or "").strip()
            if not text:
                continue
            try:
                for chat_type, chat_id in recipients:
                    if chat_type == "group":
                        await rpc.action("send_group_msg", {
                            "group_id": int(chat_id), "message": _onebot_text_message(text),
                        })
                    else:
                        await rpc.action("send_private_msg", {
                            "user_id": int(chat_id), "message": _onebot_text_message(text),
                        })
            except Exception as error:
                reschedule_delivery(self.state_root, message_id, error=str(error)[:500])
                self.state.counters.failures += 1
                self._mark_send_failure(error)
                self._persist_state()
                LOGGER.error("outbox message %s delivery failed: %s", message_id, error)
                continue
            acknowledge_delivery(self.state_root, message_id, status="sent")
            self.state.counters.reports_sent += len(recipients)
            self._mark_send_success()
            self._persist_state()
            LOGGER.info(
                "QQ report outbox delivered: message_id=%s recipients=%s",
                message_id, len(recipients),
            )

    async def _refresh_qq_health(self, rpc: OneBotRPC) -> bool:
        """Do not burn notification retries while NapCat's QQ account is offline."""
        now = asyncio.get_running_loop().time()
        if now < self._send_blocked_until_monotonic:
            return False
        if now - self._last_qq_health_monotonic < 30.0 and self.state.qq_send_available is not None:
            return self.state.qq_send_available
        self._last_qq_health_monotonic = now
        previously_available = self.state.qq_send_available is True
        try:
            status = await rpc.action("get_status", {})
            online = status.get("online")
            good = status.get("good")
            # OneBot 11 defines ``online`` as a boolean. Missing/unknown must
            # be treated as unhealthy rather than optimistically online.
            available = online is True and good is not False
            error = None if available else (
                f"NapCat reports QQ account offline or unavailable "
                f"(online={online!r}, good={good!r})"
            )
        except Exception as error_value:
            available = False
            error = f"QQ health check failed: {type(error_value).__name__}: {str(error_value)[:300]}"
        self.state.qq_send_available = available
        self.state.last_qq_health_at = _iso()
        self.state.last_qq_health_error = error
        if available and not previously_available:
            expedite_pending_send_requests(self.state_root)
            expedite_pending_deliveries(self.state_root)
            LOGGER.info("QQ send service recovered; pending notifications expedited")
        if not available:
            LOGGER.warning("QQ send service unavailable; pending notifications retained: %s", error)
        self._persist_state()
        return available
    async def run_connection(self, websocket: Any) -> None:
        rpc = OneBotRPC(websocket, action_timeout_seconds=self.config.action_timeout_seconds)
        self.state.connected = True
        self.state.last_connected_at = _iso()
        self.state.last_error = None
        self._persist_state()
        try:
            outbox_task = asyncio.create_task(self._outbox_loop(rpc))
            if self.state.mode == "controller" and not self._startup_announced:
                self._startup_announced = True
                restarted = self._startup_lifecycle == "restarted"
                if not restarted:
                    self._queue_admin_lifecycle_notice("OOPZ QQ 机器人已启动。", source="gateway_startup")
                    self._queue_admin_lifecycle_notice(HELP_TEXT, source="gateway_startup_help")
            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    self.state.counters.other_events_discarded += 1
                    self._persist_state()
                    continue
                if not isinstance(payload, dict):
                    continue
                if rpc.accept_response(payload):
                    continue
                task = asyncio.create_task(self.handle_event(payload, rpc))
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)
        finally:
            current_task = asyncio.current_task()
            if self.state.mode == "controller" and self._startup_announced and current_task is not None and current_task.cancelling():
                try:
                    self._queue_admin_lifecycle_notice("OOPZ QQ 机器人已关闭。", source="gateway_shutdown")
                except Exception:
                    LOGGER.warning("failed to send QQ gateway shutdown notification", exc_info=True)
            outbox_task.cancel()
            await asyncio.gather(outbox_task, return_exceptions=True)
            rpc.fail_pending(ConnectionError("OneBot WebSocket disconnected"))
            if self._event_tasks:
                await asyncio.gather(*tuple(self._event_tasks), return_exceptions=True)
            self.state.connected = False
            self._persist_state()

    async def serve_forever(self) -> None:
        try:
            from websockets.asyncio.client import connect
        except ImportError as error:
            raise RuntimeError('OneBot support is missing; install with pip install -e ".[qq]"') from error
        headers = {"Authorization": f"Bearer {self.config.access_token}"}
        delay = self.config.reconnect_min_seconds
        while True:
            try:
                async with connect(
                    self.config.websocket_url,
                    additional_headers=headers,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=10,
                    max_size=1024 * 1024,
                    proxy=None,
                ) as websocket:
                    delay = self.config.reconnect_min_seconds
                    await self.run_connection(websocket)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.state.connected = False
                self.state.last_error = f"{type(error).__name__}: {str(error)[:500]}"
                self._persist_state()
                LOGGER.warning("OneBot connection failed; retrying in %.1fs: %s", delay, error)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.config.reconnect_max_seconds)


async def diagnose_connection(config: OneBotGatewayConfig) -> dict[str, Any]:
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:
        raise RuntimeError('OneBot support is missing; install with pip install -e ".[qq]"') from error
    headers = {"Authorization": f"Bearer {config.access_token}"}
    async with connect(
        config.websocket_url,
        additional_headers=headers,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=1024 * 1024,
        proxy=None,
    ) as websocket:
        rpc = OneBotRPC(websocket, action_timeout_seconds=config.action_timeout_seconds)

        async def receive() -> None:
            async for raw in websocket:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    rpc.accept_response(payload)

        receiver = asyncio.create_task(receive())
        try:
            login = await rpc.action("get_login_info", {})
            groups = await rpc.action("get_group_list", {})
            friends = await rpc.action("get_friend_list", {})
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
        group_values = groups.get("value")
        group_list = group_values if isinstance(group_values, list) else []
        configured_group_present = any(
            str(group.get("group_id") or "") == config.report_group_qq
            for group in group_list if isinstance(group, dict)
        )
        friend_values = friends.get("value")
        friend_list = friend_values if isinstance(friend_values, list) else []
        configured_admin_present = any(
            str(friend.get("user_id") or "") == config.admin_qq
            for friend in friend_list if isinstance(friend, dict)
        )
        configured_friend_present = any(
            str(friend.get("user_id") or "") == config.report_friend_qq
            for friend in friend_list if isinstance(friend, dict)
        )
        return {
            "status": "connected",
            "logged_in_qq_masked": "*" * max(0, len(str(login.get("user_id") or "")) - 4)
            + str(login.get("user_id") or "")[-4:],
            "nickname": str(login.get("nickname") or ""),
            "group_count": len(group_list),
            "configured_report_group_present": configured_group_present,
            "configured_admin_present": configured_admin_present,
            "configured_report_friend_present": configured_friend_present,
            "oopz_or_deepseek_called": False,
        }


async def notify_administrators(config: OneBotGatewayConfig, text: str) -> dict[str, Any]:
    """Deliver an acknowledged lifecycle notice before a gateway process exits."""
    content = text.strip()
    if not content:
        raise ValueError("notification text must not be empty")
    try:
        from websockets.asyncio.client import connect
    except ImportError as error:
        raise RuntimeError('OneBot support is missing; install with pip install -e ".[qq]"') from error

    admin_ids = {config.admin_qq}
    admins_path = config.state_root.resolve() / "admins.json"
    try:
        value = json.loads(admins_path.read_text(encoding="utf-8")) if admins_path.is_file() else {}
        extra = value.get("admin_ids") if isinstance(value, dict) else []
        if isinstance(extra, list):
            admin_ids.update(str(item) for item in extra if str(item).isdigit())
    except (OSError, ValueError):
        pass

    headers = {"Authorization": f"Bearer {config.access_token}"}
    async with connect(
        config.websocket_url,
        additional_headers=headers,
        open_timeout=10,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=10,
        max_size=1024 * 1024,
        proxy=None,
    ) as websocket:
        rpc = OneBotRPC(websocket, action_timeout_seconds=config.action_timeout_seconds)

        async def receive() -> None:
            async for raw in websocket:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    rpc.accept_response(payload)

        receiver = asyncio.create_task(receive())
        try:
            for admin_id in sorted(admin_ids, key=int):
                await rpc.action("send_private_msg", {
                    "user_id": int(admin_id),
                    "message": _onebot_text_message(content),
                    "auto_escape": True,
                })
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    return {"status": "sent", "administrator_count": len(admin_ids)}
