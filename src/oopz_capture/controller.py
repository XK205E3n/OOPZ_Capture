from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .continuous import (
    ContinuousRequest, repair_continuous_session, request_stop, run_continuous_capture,
)
from .identifiers import new_session_id
from .jsonio import atomic_json as _atomic_json, iso_utc as _iso, read_json_or_none
from .controller_protocol import SenderPolicy, ControllerInboundMessage, make_reply, parse_command
from .digest_job import run_digest
from .sessions import digest_png
from .send_request import enqueue_send_request
from .settings import SETTABLE_KEYS, apply_setting, canonical_setting_key, setting_status
from .workflow import _is_reparse_point, _validate_tree_no_links


START_FLOW_SCHEMA = "oopz.controller.start_flow.v1"
START_FLOW_TTL_SECONDS = 600

# Settings that only affect the next analysis run, not the next recording.
_ANALYSIS_SETTING_KEYS = frozenset({"OOPZ_ANALYZER_MODEL", "OOPZ_ANALYZER_TIMEOUT_SECONDS"})

# env key -> (ControllerConfig field, parser); applied live on `/oopz 设置`.
LIVE_CONFIG_FIELDS: dict[str, tuple[str, Callable[[str], Any]]] = {
    "OOPZ_CUTOFF_LOCAL_HOUR": ("cutoff_local_hour", int),
    "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS": ("empty_channel_timeout_seconds", float),
    "OOPZ_CHUNK_SECONDS": ("chunk_seconds", int),
    "OOPZ_LANGUAGE": ("language", str),
    "OOPZ_RETAIN_AUDIO": ("retain_audio", lambda raw: raw == "true"),
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS": ("transcription_repair_attempts", int),
    "OOPZ_RETENTION_HOURS": ("retention_hours", int),
    "OOPZ_DEVICE": ("device", str),
    "OOPZ_PROCESSING_DEADLINE_SECONDS": ("processing_deadline_seconds", int),
    "OOPZ_POLL_INTERVAL_SECONDS": ("poll_interval_seconds", float),
    "OOPZ_MEMBERSHIP_REFRESH_SECONDS": ("membership_refresh_seconds", float),
    "OOPZ_MEMBERSHIP_TIMEOUT_SECONDS": ("membership_timeout_seconds", float),
    "OOPZ_CONNECTION_CHECK_SECONDS": ("connection_check_seconds", float),
    "OOPZ_DISCONNECT_GRACE_SECONDS": ("disconnect_grace_seconds", float),
    "OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS": ("browser_operation_timeout_seconds", float),
    "OOPZ_RECONNECT_WINDOW_SECONDS": ("reconnect_window_seconds", float),
    "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS": ("reconnect_initial_delay_seconds", float),
    "OOPZ_RECONNECT_MAX_DELAY_SECONDS": ("reconnect_max_delay_seconds", float),
    "OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS": ("reconnect_attempt_timeout_seconds", float),
}
HELP_TEXT = "\n".join([
    "/oopz 开始 [秒数]：依次选择域和语音频道后开始录音，可指定时长（秒，或 5m/1h）",
    "/oopz 离开：结束录音；转写后自动分析，完成后把图发到群里",
    "/oopz 状态：查看当前录音任务状态",
    "/oopz 设置 变量=值：修改运行设置；先用 /oopz 设置状态 查看可用变量、当前值和说明",
    "/oopz 设置状态：查看可修改的变量（密码、手机号和密钥打码）",
    "/oopz 帮助：显示本帮助",
])


_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*$", re.IGNORECASE)
_DURATION_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def _parse_duration_seconds(value: str) -> float | None:
    value = str(value or "").strip()
    if not value:
        return None
    match = _DURATION_RE.fullmatch(value)
    if not match:
        raise ValueError("时长格式无效；示例：/oopz开始 300（秒）、5m、1h")
    seconds = float(match.group(1)) * _DURATION_UNIT_SECONDS[match.group(2).casefold() or "s"]
    if not 5 <= seconds <= 86400:
        raise ValueError("时长必须在 5 秒到 24 小时之间")
    return seconds


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class ControllerConfig:
    output_root: Path
    state_root: Path
    authorization: SenderPolicy
    consent_confirmed: bool
    chunk_seconds: int = 300
    cutoff_local_hour: int = 4
    language: str = "auto"
    retain_audio: bool = False
    transcription_repair_attempts: int = 1
    processing_deadline_seconds: int = 900
    retention_hours: int = 360
    poll_interval_seconds: float = 0.25
    membership_refresh_seconds: float = 30.0
    membership_timeout_seconds: float = 10.0
    empty_channel_timeout_seconds: float = 300.0
    connection_check_seconds: float = 2.0
    disconnect_grace_seconds: float = 15.0
    browser_operation_timeout_seconds: float = 2.0
    reconnect_window_seconds: float = 300.0
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    reconnect_attempt_timeout_seconds: float = 30.0
    device: str = "cpu"
    capture_only: bool = False

    def validate(self) -> None:
        if not self.authorization.allowed_sender_ids:
            raise ValueError("controller authorization requires at least one allowed sender")
        if self.consent_confirmed is not True and not self.capture_only:
            raise ValueError("recording consent must be confirmed by the caller")
        if self.device not in {"cpu", "cuda:0"}:
            raise ValueError("OOPZ_DEVICE must be cpu or cuda:0")
        if not 0 <= self.transcription_repair_attempts <= 3:
            raise ValueError("OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS must be 0 to 3")
        ContinuousRequest(
            request_id=str(uuid4()), area_id="selection-required", channel_id="selection-required",
            consent_confirmed=True, chunk_seconds=self.chunk_seconds,
            cutoff_local_hour=self.cutoff_local_hour, language=self.language,
            processing_deadline_seconds=self.processing_deadline_seconds,
            retention_hours=self.retention_hours, poll_interval_seconds=self.poll_interval_seconds,
            membership_refresh_seconds=self.membership_refresh_seconds,
            membership_timeout_seconds=self.membership_timeout_seconds,
            empty_channel_timeout_seconds=self.empty_channel_timeout_seconds,
            retain_audio=self.retain_audio,
            connection_check_seconds=self.connection_check_seconds,
            disconnect_grace_seconds=self.disconnect_grace_seconds,
            browser_operation_timeout_seconds=self.browser_operation_timeout_seconds,
            reconnect_window_seconds=self.reconnect_window_seconds,
            reconnect_initial_delay_seconds=self.reconnect_initial_delay_seconds,
            reconnect_max_delay_seconds=self.reconnect_max_delay_seconds,
            reconnect_attempt_timeout_seconds=self.reconnect_attempt_timeout_seconds,
        ).validate()


ConfigLoader = Callable[[bool], Awaitable[Any]]
CaptureRunner = Callable[..., Awaitable[Path]]
AnalysisRunner = Callable[[Path], dict[str, Any]]


async def _default_config_loader(show_browser: bool) -> Any:
    from .main import _config
    return await _config(show_browser=show_browser)


class ControllerService:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        config_loader: ConfigLoader = _default_config_loader,
        capture_runner: CaptureRunner = run_continuous_capture,
        analysis_runner: AnalysisRunner = run_digest,
    ):
        config.validate()
        self.config = config
        self.config_loader = config_loader
        self.capture_runner = capture_runner
        self.analysis_runner = analysis_runner
        self.state_root = config.state_root.resolve()
        self.output_root = config.output_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.state_root) or _is_reparse_point(self.output_root):
            raise ValueError("controller state and output roots may not be links or reparse points")
        self.state_path = self.state_root / "controller.json"
        self._start_flow_path = self.state_root / "start_flow.json"
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._analysis_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._stopping = False
        self._active_task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        self._recover_active_session()
        self._reconcile_last_job()

    def request_shutdown(self) -> None:
        """Stop admission and ask the recorder to finalize its current chunks."""
        self._stopping = True
        active = self._state.get("active")
        if not isinstance(active, dict):
            return
        # The capture coroutine may still be loading config and not have a
        # lifecycle file. Its startup handshake checks this durable flag.
        active["stop_requested_before_capture"] = True
        self._save_state()
        session_id = str(active.get("session_id") or "")
        if session_id:
            try:
                request_stop(self.output_root, session_id, reason="service_shutdown")
            except (ValueError, FileNotFoundError):
                # Already transcribing, or not started yet: await the driver.
                pass

    async def shutdown(self) -> None:
        """Drain owned work without abandoning analysis running in a thread.

        Completed reports remain in the durable outbox for the next gateway;
        shutdown never retries sends or automatically launches another analysis.
        systemd supplies the final bounded cgroup timeout if a provider hangs.
        """
        self.request_shutdown()
        if self._active_task is not None:
            await asyncio.shield(self._active_task)
        if self._background_tasks:
            await asyncio.gather(*tuple(self._background_tasks), return_exceptions=True)

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            if _is_reparse_point(self.state_path):
                raise ValueError("unsafe controller state file")
            value = read_json_or_none(self.state_path)
            if value and value.get("schema_version") == "oopz.controller.controller.state.v1":
                active = value.get("active")
                if isinstance(active, dict):
                    value["active"] = None
                    value["last_job"] = {**active, "status": "controller_restarted", "updated_at": _iso()}
                return value
        return {
            "schema_version": "oopz.controller.controller.state.v1",
            "active": None,
            "last_job": None,
            "updated_at": _iso(),
        }

    def _save_state(self) -> None:
        self._state["updated_at"] = _iso()
        _atomic_json(self.state_path, self._state)

    def _recover_active_session(self) -> dict[str, Any] | None:
        """Recover control from the worker lifecycle when controller memory is stale.

        An active-status lifecycle is only honored while this process still drives
        its capture task.  After a restart the same bytes describe a dead run with
        no driver; adopting it would block every new recording until retention
        cleanup, so the orphan is retired into an explicit interrupted state.
        """
        active_statuses = {"connecting", "recording", "reconnecting", "stopping"}
        driven = self._active_task is not None and not self._active_task.done()
        active = self._state.get("active")
        if isinstance(active, dict):
            session_id = str(active.get("session_id") or "")
            path = self.output_root / session_id / "lifecycle.json"
            lifecycle = self._load_json_object(path) if session_id else None
            if not driven:
                self._retire_orphan_capture(session_id, path, lifecycle, active)
                return None
            if lifecycle is None and active.get("status") in {"starting", "connecting"}:
                return active
            if isinstance(lifecycle, dict) and lifecycle.get("status") in active_statuses:
                status = str(lifecycle["status"])
                if active.get("status") != status:
                    active["status"] = status
                    active["lifecycle_updated_at"] = _iso()
                    self._save_state()
                return active
            self._state["last_job"] = {**active, "status": str((lifecycle or {}).get("status") or "inactive"), "updated_at": _iso()}
            self._state["active"] = None

        candidates: list[tuple[str, Path, dict[str, Any]]] = []
        for session_dir in self.output_root.iterdir():
            if not session_dir.is_dir() or _is_reparse_point(session_dir):
                continue
            lifecycle = self._load_json_object(session_dir / "lifecycle.json")
            if not isinstance(lifecycle, dict):
                continue
            if lifecycle.get("managed_by") != "oopz-worker-v1" or lifecycle.get("mode") != "continuous":
                continue
            if lifecycle.get("status") not in active_statuses:
                continue
            sort_key = str(lifecycle.get("capture_started_at") or lifecycle.get("started_at") or session_dir.name)
            candidates.append((sort_key, session_dir, lifecycle))
        if not candidates:
            self._save_state()
            return None
        _, session_dir, lifecycle = max(candidates, key=lambda item: item[0])
        if not driven:
            self._retire_orphan_capture(session_dir.name, session_dir / "lifecycle.json", lifecycle, None)
            self._save_state()
            return None
        request = self._load_json_object(session_dir / "request.json") or {}
        recovered = {
            "session_id": session_dir.name,
            "request_id": str(lifecycle.get("request_id") or request.get("request_id") or ""),
            "status": str(lifecycle.get("status") or "recording"),
            "started_at": str(lifecycle.get("started_at") or _iso()),
            "requested_by": request.get("requested_by") or {"source": "recovered_worker_lifecycle"},
            "area_name": str(request.get("area_id") or ""),
            "channel_name": str(request.get("channel_id") or ""),
            "recovered_from_lifecycle": True,
        }
        self._state["active"] = recovered
        self._save_state()
        return recovered

    def _retire_orphan_capture(
        self, session_id: str, lifecycle_path: Path, lifecycle: dict[str, Any] | None, active: dict[str, Any] | None,
    ) -> None:
        """Move a capture this process no longer drives out of the active set."""
        now = _iso()
        if lifecycle is not None:
            lifecycle.update({
                "status": "interrupted",
                "interrupted_at": now,
                "interrupted_reason": "controller_restarted_without_capture_driver",
                "previous_status": str(lifecycle.get("status") or "unknown"),
            })
            _atomic_json(lifecycle_path, lifecycle)
        if isinstance(active, dict):
            self._state["last_job"] = {**active, "status": "interrupted", "updated_at": now}
        self._state["active"] = None

    def _reconcile_last_job(self) -> bool:
        """Synchronize stale controller state with the authoritative worker lifecycle and the digest file."""
        last = self._state.get("last_job")
        session_id = str(last.get("session_id") or "") if isinstance(last, dict) else ""
        if not session_id:
            return False
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root or not session_dir.is_dir() or _is_reparse_point(session_dir):
            return False
        capture = self._load_json_object(session_dir / "lifecycle.json") or {}
        status = str(capture.get("status") or "")
        updates: dict[str, Any] = {}
        if not self.config.capture_only and digest_png(session_dir).is_file():
            updates = {"status": "analysis_completed"}
        elif last.get("status") == "analyzing" and session_id not in self._analysis_sessions:
            updates = {"status": "analysis_interrupted"}      # the analysis died with the previous process; "待分析" retries it
        elif status in {"ready_for_analysis", "ready_for_analysis_with_errors"}:
            updates = {
                "status": ("capture_transcription_completed_with_errors" if status.endswith("with_errors") else "capture_transcription_completed") if self.config.capture_only else status,
                "stop_reason": str(capture.get("stop_reason") or ""),
                "stopped_at": str(capture.get("stopped_at") or ""),
                "chunks_total": int(capture.get("chunks_total", 0) or 0),
                "chunks_transcribed": int(capture.get("chunks_transcribed", 0) or 0),
                "chunks_failed": int(capture.get("chunks_failed", 0) or 0),
            }
        if not updates or all(last.get(key) == item for key, item in updates.items()):
            return False
        reconciled = {**last, **updates}
        if updates["status"] in {"ready_for_analysis", "ready_for_analysis_with_errors"}:
            for stale in ("error_type", "error", "finished_at"):
                reconciled.pop(stale, None)
        self._state["last_job"] = reconciled
        self._save_state()
        return True

    def _reply_path(self, message_id: str) -> Path:
        return self.state_root / "replies" / f"{message_id}.json"

    def _saved_reply(self, message_id: str) -> dict[str, Any] | None:
        path = self._reply_path(message_id)
        if not path.is_file():
            return None
        if _is_reparse_point(path):
            raise ValueError("unsafe reply file")
        return read_json_or_none(path)

    def _store_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        _atomic_json(self._reply_path(str(reply["message_id"])), reply)
        return reply

    async def handle(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        message = ControllerInboundMessage.from_dict(raw_message)
        async with self._lock:
            if self._stopping:
                return make_reply(message, command="shutdown", status="rejected", at=_iso(),
                                  text="服务正在停止，请在重启后重试。")
            existing = self._saved_reply(message.message_id)
            if existing is not None:
                return existing
            if not self._authorize(message):
                return self._store_reply(make_reply(
                    message, command="unauthorized", status="rejected", at=_iso(),
                    text="拒绝执行：发送者或会话不在授权名单中。",
                ))
            try:
                command = parse_command(message.text)
            except ValueError:
                start_reply = await self._process_start_flow(message, "start_capture")
                if start_reply is not None:
                    return self._store_reply(start_reply)
                return self._store_reply(make_reply(
                    message, command="invalid", status="rejected", at=_iso(),
                    text="不支持的指令；发送 /oopz 帮助 查看可用指令。",
                ))
            if self.config.capture_only and command not in {"help", "start_capture", "leave_channel", "status"}:
                return self._store_reply(make_reply(message, command=command, status="rejected", at=_iso(), text="仅录音转写模式不支持此操作。"))
            if command == "help":
                return self._store_reply(make_reply(
                    message, command=command, status="completed", at=_iso(),
            text=HELP_TEXT,
                ))
            if command == "start_capture":
                return self._store_reply(await self._start(message, command))
            if command == "leave_channel":
                return self._store_reply(self._leave(message, command))
            if command == "status":
                return self._store_reply(self._status(message, command))
            if command == "set_config":
                return self._store_reply(self._set_config(message, command))
            if command == "settings_status":
                return self._store_reply(self._settings_status(message, command))
            raise AssertionError(command)

    def _authorize(self, message: ControllerInboundMessage) -> bool:
        return self.config.authorization.authorize(message)

    @staticmethod
    def _load_json_object(path: Path) -> dict[str, Any] | None:
        if not path.is_file() or _is_reparse_point(path):
            return None
        return read_json_or_none(path)

    async def _start(self, message: ControllerInboundMessage, command: str) -> dict[str, Any]:
        self._recover_active_session()
        active = self._state.get("active")
        if isinstance(active, dict):
            session_id = str(active.get("session_id") or "")
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text=f"录音已占用：已有录音任务在运行；Session ID={session_id}。如需结束当前录音，请发送 /oopz 离开。",
                session_id=session_id,
            )
        duration_match = re.match(r"^/oopz\s*(?:start|开始)\s*(.*)$", message.text, re.IGNORECASE)
        duration_text = duration_match.group(1).strip() if duration_match else ""
        try:
            max_runtime = _parse_duration_seconds(duration_text)
        except ValueError as error:
            return make_reply(
                message, command=command, status="rejected", at=_iso(), text=str(error),
            )
        existing_flow = self._load_start_flow()
        if existing_flow is not None and str(existing_flow.get("admin_id")) != message.sender_id:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="另一位管理员正在选择录音目标；请稍后重试。",
            )
        try:
            areas = await self._load_area_choices()
        except Exception as error:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text=f"无法读取 OOPZ 域列表：{error}",
            )
        if not areas:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="当前账号没有可供选择的已加入域。",
            )
        self._save_start_flow({
            "schema_version": START_FLOW_SCHEMA,
            "admin_id": message.sender_id,
            "stage": "awaiting_area_selection",
            "max_runtime_seconds": max_runtime,
            "areas": areas,
        })
        lines = [f"{index}. {item['name']}" for index, item in enumerate(areas, start=1)]
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="请选择要进入的域：\n" + "\n".join(lines) + "\n\n回复编号；回复 取消 可退出选择。",
        )

    async def _load_area_choices(self) -> list[dict[str, str]]:
        from oopz_sdk import OopzBot
        config = await self.config_loader(False)
        bot = OopzBot(config)
        try:
            values = await bot.areas.get_joined_areas()
            return [
                {"area_id": str(item.area_id), "name": str(item.name).strip()}
                for item in values if str(item.name).strip() and str(item.area_id).strip()
            ]
        finally:
            await bot.stop()

    async def _load_channel_choices(self, area_id: str) -> list[dict[str, str]]:
        from oopz_sdk import OopzBot
        config = await self.config_loader(False)
        bot = OopzBot(config)
        try:
            groups = await bot.areas.get_area_channels(area_id)
            choices: list[dict[str, str]] = []
            duplicate_counts: dict[str, int] = {}
            for group in groups:
                group_name = str(group.name).strip()
                for channel in group.channels:
                    if str(channel.channel_type).upper() not in {"VOICE", "AUDIO"}:
                        continue
                    channel_name = str(channel.name).strip()
                    channel_id = str(channel.channel_id).strip()
                    if not channel_name or not channel_id:
                        continue
                    base_name = f"{group_name} / {channel_name}" if group_name else channel_name
                    duplicate_counts[base_name] = duplicate_counts.get(base_name, 0) + 1
                    ordinal = duplicate_counts[base_name]
                    display_name = base_name if ordinal == 1 else f"{base_name}（同名频道 {ordinal}）"
                    choices.append({
                        "channel_id": channel_id,
                        "name": channel_name,
                        "display_name": display_name,
                    })
            return choices
        finally:
            await bot.stop()

    def _save_start_flow(self, value: dict[str, Any]) -> None:
        value["updated_at"] = _iso()
        _atomic_json(self._start_flow_path, value)

    def _load_start_flow(self) -> dict[str, Any] | None:
        if not self._start_flow_path.is_file() or _is_reparse_point(self._start_flow_path):
            return None
        value = read_json_or_none(self._start_flow_path)
        if not value or value.get("schema_version") != START_FLOW_SCHEMA:
            return None
        # A selection flow abandoned by its initiator must never wedge the whole
        # group's start command, so it expires after ten minutes.
        try:
            updated = datetime.fromisoformat(str(value.get("updated_at") or "").replace("Z", "+00:00"))
        except ValueError:
            self._clear_start_flow()
            return None
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated > timedelta(seconds=START_FLOW_TTL_SECONDS):
            self._clear_start_flow()
            return None
        return value

    def _clear_start_flow(self) -> None:
        if self._start_flow_path.is_file() and not _is_reparse_point(self._start_flow_path):
            self._start_flow_path.unlink()

    async def _process_start_flow(
        self, message: ControllerInboundMessage, command: str,
    ) -> dict[str, Any] | None:
        flow = self._load_start_flow()
        if flow is None or str(flow.get("admin_id")) != message.sender_id:
            return None
        text = message.text.strip()
        if text.casefold() in {"取消", "退出", "cancel"}:
            self._clear_start_flow()
            return make_reply(message, command=command, status="completed", at=_iso(), text="已取消录音目标选择。")
        if isinstance(self._state.get("active"), dict):
            self._clear_start_flow()
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="录音任务已被占用，本次选择已取消。发送 /oopz 状态 可查看详情。",
            )
        stage = str(flow.get("stage") or "")
        if self.config.capture_only and stage == "awaiting_recording_consent":
            token = str(flow.get("consent_token") or "")
            if not token or text != "capture_consent:" + token:
                return make_reply(message, command=command, status="rejected", at=_iso(),
                                  text="请由发起人点击“已告知参与者并开始录音”，或取消。")
            area, channel = flow["selected_area"], flow["selected_channel"]
            self._clear_start_flow()
            return await self._launch_capture(
                message, command, area_id=str(area["area_id"]), channel_id=str(channel["channel_id"]),
                area_name=str(area["name"]), channel_name=str(channel["display_name"]),
                max_runtime=flow.get("max_runtime_seconds"), consent_confirmed=True,
            )
        if not text.isdigit():
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="请回复列表中的编号，或回复 取消。",
            )
        stage = str(flow.get("stage") or "")
        if stage == "awaiting_area_selection":
            areas = flow.get("areas") or []
            index = int(text)
            if not 1 <= index <= len(areas):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"域编号必须在 1-{len(areas)} 之间。",
                )
            area = areas[index - 1]
            try:
                channels = await self._load_channel_choices(str(area["area_id"]))
            except Exception as error:
                self._clear_start_flow()
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"无法读取“{area['name']}”的频道列表：{error}",
                )
            if not channels:
                self._clear_start_flow()
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"“{area['name']}”中没有可供选择的语音频道。",
                )
            flow["selected_area"] = area
            flow["channels"] = channels
            flow["stage"] = "awaiting_channel_selection"
            self._save_start_flow(flow)
            lines = [f"{number}. {item['display_name']}" for number, item in enumerate(channels, start=1)]
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已选择域：{area['name']}\n请选择语音频道：\n" + "\n".join(lines) + "\n\n回复编号；回复 取消 可退出选择。",
            )
        if stage == "awaiting_channel_selection":
            channels = flow.get("channels") or []
            index = int(text)
            if not 1 <= index <= len(channels):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"频道编号必须在 1-{len(channels)} 之间。",
                )
            area = flow.get("selected_area") or {}
            channel = channels[index - 1]
            if self.config.capture_only:
                token = uuid4().hex
                flow.update(stage="awaiting_recording_consent", selected_channel=channel, consent_token=token)
                self._save_start_flow(flow)
                return make_reply(message, command=command, status="completed", at=_iso(),
                                  text=f"即将录音：{area['name']} / {channel['display_name']}。尚未开始录音；请先告知所有参与者，再点击确认。",
                                  capture_consent_token=token)
            self._clear_start_flow()
            return await self._launch_capture(
                message, command,
                area_id=str(area["area_id"]), channel_id=str(channel["channel_id"]),
                area_name=str(area["name"]), channel_name=str(channel["display_name"]),
                max_runtime=flow.get("max_runtime_seconds"),
            )
        self._clear_start_flow()
        return make_reply(message, command=command, status="rejected", at=_iso(), text="录音目标选择状态已失效，请重新发送 /oopz 开始。")

    async def _launch_capture(
        self, message: ControllerInboundMessage, command: str, *, area_id: str,
        channel_id: str, area_name: str, channel_name: str,
        max_runtime: float | None, consent_confirmed: bool = False,
    ) -> dict[str, Any]:
        if self.config.capture_only and consent_confirmed is not True:
            raise ValueError("capture-only requires explicit initiator confirmation before recording")
        session_id = new_session_id(self.output_root)
        request_id = str(uuid4())
        active = {
            "session_id": session_id,
            "request_id": request_id,
            "status": "starting",
            "started_at": _iso(),
            "requested_by": message.requested_by,
            "area_name": area_name,
            "channel_name": channel_name,
        }
        self._state["active"] = active
        self._save_state()
        request = ContinuousRequest(
            request_id=request_id,
            area_id=area_id,
            channel_id=channel_id,
            consent_confirmed=consent_confirmed if self.config.capture_only else True,
            max_runtime_seconds=max_runtime,
            chunk_seconds=self.config.chunk_seconds,
            cutoff_local_hour=self.config.cutoff_local_hour,
            language=self.config.language,
            processing_deadline_seconds=self.config.processing_deadline_seconds,
            retention_hours=self.config.retention_hours,
            poll_interval_seconds=self.config.poll_interval_seconds,
            membership_refresh_seconds=self.config.membership_refresh_seconds,
            membership_timeout_seconds=self.config.membership_timeout_seconds,
            empty_channel_timeout_seconds=self.config.empty_channel_timeout_seconds,
            retain_audio=self.config.capture_only or self.config.retain_audio,
            connection_check_seconds=self.config.connection_check_seconds,
            disconnect_grace_seconds=self.config.disconnect_grace_seconds,
            browser_operation_timeout_seconds=self.config.browser_operation_timeout_seconds,
            reconnect_window_seconds=self.config.reconnect_window_seconds,
            reconnect_initial_delay_seconds=self.config.reconnect_initial_delay_seconds,
            reconnect_max_delay_seconds=self.config.reconnect_max_delay_seconds,
            reconnect_attempt_timeout_seconds=self.config.reconnect_attempt_timeout_seconds,
            requested_by=message.requested_by,
        )
        self._active_task = asyncio.create_task(self._run_session(session_id, request))
        await asyncio.sleep(0)
        return make_reply(
            message, command=command, status="accepted", at=_iso(),
            text=f"录音任务已启动；域：{area_name}；频道：{channel_name}；Session ID={session_id}{('；时长=' + str(int(max_runtime)) + ' 秒') if max_runtime is not None else ''}。发送 /oopz 离开 可提前结束录音。",
            request_id=request_id, session_id=session_id,
        )

    def _leave(self, message: ControllerInboundMessage, command: str) -> dict[str, Any]:
        active = self._recover_active_session()
        if not isinstance(active, dict):
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text="当前没有正在运行的录音任务。",
            )
        session_id = str(active["session_id"])
        lifecycle_path = self.output_root / session_id / "lifecycle.json"
        if not lifecycle_path.is_file() and active.get("status") in {"starting", "connecting"}:
            active["status"] = "stop_requested"
            active["stop_requested_before_capture"] = True
            active["stop_requested_at"] = _iso()
            active["stop_requested_by"] = message.requested_by
            self._save_state()
            return make_reply(
                message, command=command, status="accepted", at=_iso(),
                text=f"已登记离开指令；Session ID={session_id}。连接建立后将立即安全退出。",
                session_id=session_id,
            )
        try:
            request_stop(
                self.output_root, session_id, requested_by=message.requested_by,
                reason="operator_stop_command",
            )
        except ValueError as error:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text=f"暂时无法提交离开指令；Session ID={session_id}；原因={error}",
                session_id=session_id,
            )
        active["status"] = "stop_requested"
        active["stop_requested_at"] = _iso()
        active["stop_requested_by"] = message.requested_by
        self._save_state()
        return make_reply(
            message, command=command, status="accepted", at=_iso(),
            text=f"已提交离开指令；Session ID={session_id}。转写后会自动分析，完成后把图发到本群。",
            session_id=session_id,
        )

    def _set_config(self, message: ControllerInboundMessage, command: str) -> dict[str, Any]:
        match = re.match(r"^/oopz\s*(?:设置|set)\s*(.*)$", message.text, re.IGNORECASE)
        args = match.group(1).strip() if match else ""
        if not args:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="格式：/oopz设置 变量名=值；可用变量见 /oopz 设置状态。",
            )
        if args.casefold() in {"状态", "status"}:
            return self._settings_status(message, command)
        if "=" in args:
            key, _, value = args.partition("=")
        else:
            parts = args.split(None, 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ""
        try:
            canonical_key = canonical_setting_key(key)
            masked = apply_setting(key.strip(), value.strip())
            if canonical_key in LIVE_CONFIG_FIELDS:
                field_name, parser = LIVE_CONFIG_FIELDS[canonical_key]
                self.config = replace(self.config, **{field_name: parser(os.environ[canonical_key])})
                self.config.validate()
        except ValueError as error:
            return make_reply(
                message, command=command, status="rejected", at=_iso(), text=str(error),
            )
        effect_note = "下一次分析生效" if canonical_key in _ANALYSIS_SETTING_KEYS else "下一次录音生效"
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text=f"已设置 {canonical_key}：{masked}。已保存到 .env；{effect_note}。",
        )

    def _settings_status(self, message: ControllerInboundMessage, command: str) -> dict[str, Any]:
        lines = [
            f"{key} = {value}（{SETTABLE_KEYS[key]['description']}）"
            for key, value in setting_status().items()
        ]
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="当前设置（密码/手机号已打码）：\n" + "\n".join(lines),
        )

    def _status(self, message: ControllerInboundMessage, command: str) -> dict[str, Any]:
        active = self._recover_active_session()
        if not isinstance(active, dict):
            self._reconcile_last_job()
            last = self._state.get("last_job")
            suffix = ""
            if isinstance(last, dict) and last.get("session_id"):
                raw_status = str(last.get("status") or "unknown")
                status_text = {
                    "capture_transcription_completed": "录音与转写已结束（仅录音转写模式）",
                    "capture_transcription_completed_with_errors": "录音已结束，转写存在错误（仅录音转写模式）",
                    "analyzing": "正在分析",
                    "ready_for_analysis": "录音和转写已完成，尚未分析；可发送“待分析”",
                    "analysis_completed": "分析已完成，图已发送",
                    "analysis_failed": "分析失败；可发送“待分析”重试",
                    "analysis_interrupted": "分析被机器人重启中断；可发送“待分析”重试",
                }.get(raw_status, raw_status)
                suffix = f"；最近 Session ID={last['session_id']}；状态={status_text}"
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text="当前没有正在运行的录音任务" + suffix + "。",
            )
        session_id = str(active["session_id"])
        lifecycle_path = self.output_root / session_id / "lifecycle.json"
        lifecycle_status = str(active.get("status") or "starting")
        lifecycle: dict[str, Any] = {}
        if lifecycle_path.is_file() and not _is_reparse_point(lifecycle_path):
            lifecycle = read_json_or_none(lifecycle_path) or {}
            lifecycle_status = str(lifecycle.get("status") or lifecycle_status)
        status_text = {
            "starting": "正在启动",
            "connecting": "正在连接频道",
            "recording": "正在录音",
            "reconnecting": "语音连接中断，正在重连",
            "stop_requested": "已收到离开指令，正在安全结束",
            "stopping": "正在结束并等待转写",
            "interrupted": "录音因机器人重启而中断",
            "ready_for_analysis": "录音和转写已完成",
            "ready_for_analysis_with_errors": "转写完成但仍有失败分片",
        }.get(lifecycle_status, lifecycle_status)
        details = [f"录音任务状态={status_text}", f"Session ID={session_id}"]
        area_name = str(active.get("area_name") or "")
        channel_name = str(active.get("channel_name") or "")
        if area_name:
            details.append(f"域={area_name}")
        if channel_name:
            details.append(f"频道={channel_name}")
        try:
            total = int(lifecycle.get("chunks_total", 0) or 0)
            transcribed = int(lifecycle.get("chunks_transcribed", 0) or 0)
            failed = int(lifecycle.get("chunks_failed", 0) or 0)
        except (TypeError, ValueError):
            total = transcribed = failed = 0
        if total or transcribed or failed:
            details.append(f"分片转写={transcribed}/{total or transcribed}")
            if failed:
                details.append(f"失败={failed}")
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="；".join(details) + "。",
            session_id=session_id, session_status=lifecycle_status,
        )

    async def _run_session(self, session_id: str, request: ContinuousRequest) -> None:
        final_status = "failed"
        details: dict[str, Any] = {}
        try:
            oopz_config = await self.config_loader(False)
            async with self._lock:
                if isinstance(self._state.get("active"), dict):
                    self._state["active"]["status"] = "connecting"
                    self._save_state()
            capture_task = asyncio.create_task(self.capture_runner(
                oopz_config, request, output_root=self.output_root,
                device=self.config.device, session_id=session_id,
            ))
            await asyncio.sleep(0)
            async with self._lock:
                active = self._state.get("active")
                stop_early = (
                    isinstance(active, dict)
                    and active.get("session_id") == session_id
                    and active.get("stop_requested_before_capture") is True
                )
                stop_requested_by = (
                    active.get("stop_requested_by")
                    if isinstance(active, dict) and active.get("session_id") == session_id
                    else None
                )
            if stop_early:
                try:
                    request_stop(
                        self.output_root, session_id,
                        requested_by=stop_requested_by or request.requested_by,
                        reason="service_shutdown" if self._stopping else "operator_stop_command",
                    )
                except (ValueError, FileNotFoundError):
                    # Startup may not yet have created its lifecycle; retry
                    # from the monitoring loop instead of orphaning the task.
                    pass
            sync_interval = min(1.0, max(0.05, self.config.poll_interval_seconds))
            progress_state: dict[str, str] = {}
            next_heartbeat_at = 0.0
            while True:
                if self._stopping:
                    self.request_shutdown()
                elif stop_early:
                    try:
                        request_stop(self.output_root, session_id,
                                     requested_by=stop_requested_by or request.requested_by)
                        stop_early = False
                    except (ValueError, FileNotFoundError):
                        pass
                try:
                    session_dir = await asyncio.wait_for(
                        asyncio.shield(capture_task), timeout=sync_interval,
                    )
                    break
                except asyncio.TimeoutError:
                    # Progress reporting is observational only.  A bug in the
                    # console/status path must never terminate the capture task
                    # or discard the active Session used by the stop command.
                    try:
                        await self._sync_active_lifecycle_status(session_id)
                        now = time.monotonic()
                        progress_state = self._print_capture_progress(
                            session_id, progress_state, heartbeat=now >= next_heartbeat_at,
                        )
                        if now >= next_heartbeat_at:
                            next_heartbeat_at = now + 60.0
                    except Exception as error:
                        print(
                            f"[录制进度] 监控更新失败（录音继续）："
                            f"{type(error).__name__}: {str(error)[:240]}",
                            flush=True,
                        )
            await self._sync_active_lifecycle_status(session_id)
            self._print_capture_progress(session_id, progress_state, heartbeat=True)
            for repair_number in range(1, self.config.transcription_repair_attempts + 1):
                failed_count = self._failed_chunk_count(session_dir)
                if failed_count <= 0:
                    break
                print(
                    f"[转写修复] Session={session_id}：发现 {failed_count} 个失败分片；"
                    f"开始第 {repair_number}/{self.config.transcription_repair_attempts} 轮自动重试。",
                    flush=True,
                )
                try:
                    session_dir = await repair_continuous_session(
                        self.output_root, session_id, device=self.config.device,
                    )
                except Exception as repair_error:
                    print(
                        f"[转写修复] Session={session_id}：自动重试异常；"
                        f"{type(repair_error).__name__}: {str(repair_error)[:300]}。音频保持不删除。",
                        flush=True,
                    )
                    break
            stop_reason = self._session_stop_reason(session_dir)
            details = {"session_id": session_id, "stop_reason": stop_reason}
            if self.config.capture_only:
                final_status = "capture_transcription_completed"
            elif self._stopping:
                final_status = "ready_for_analysis"
            elif self._start_analysis_and_deliver(session_dir):
                final_status = "analyzing"
            else:
                final_status = "ready_for_analysis"
        except BaseException as error:
            final_status = "cancelled" if isinstance(error, asyncio.CancelledError) else "failed"
            details = {"error_type": type(error).__name__, "error": str(error)[:1000]}
        finally:
            async with self._lock:
                active = self._state.get("active")
                if isinstance(active, dict) and active.get("session_id") == session_id:
                    self._state["last_job"] = {
                        **active, **details, "status": final_status, "finished_at": _iso(),
                    }
                    self._state["active"] = None
                    self._save_state()
                self._active_task = None

    def _print_capture_progress(
        self, session_id: str, previous: dict[str, str], *, heartbeat: bool,
    ) -> dict[str, str]:
        """Print file-backed capture/transcription progress in the controller console.

        The continuous recorder remains authoritative.  This observer only reads
        its lifecycle files, so console reporting can never delay or alter audio
        capture.  A state-change line is printed immediately; a compact heartbeat
        is printed once per minute while recording continues.
        """
        session_dir = self.output_root / session_id
        lifecycle_path = session_dir / "lifecycle.json"
        if not lifecycle_path.is_file() or _is_reparse_point(lifecycle_path):
            return previous
        lifecycle = read_json_or_none(lifecycle_path)
        if lifecycle is None:
            return previous
        status = str(lifecycle.get("status") or "unknown")
        current: dict[str, str] = {"session": status}
        chunks_root = session_dir / "chunks"
        chunks: list[Path] = []
        if chunks_root.is_dir() and not _is_reparse_point(chunks_root):
            chunks = sorted(
                (item for item in chunks_root.iterdir() if item.is_dir() and not _is_reparse_point(item)),
                key=lambda item: item.name,
            )
        transcribed = failed = transcribing = recording = 0
        for chunk_dir in chunks:
            index = chunk_dir.name.split("-", 1)[0].lstrip("0") or "0"
            chunk_lifecycle = chunk_dir / "lifecycle.json"
            chunk_status = "recording"
            segments = ""
            audio_note = ""
            if chunk_lifecycle.is_file() and not _is_reparse_point(chunk_lifecycle):
                try:
                    value = json.loads(chunk_lifecycle.read_text(encoding="utf-8"))
                    chunk_status = str(value.get("status") or "unknown")
                    if chunk_status == "transcribed":
                        segments = str(int(value.get("transcript_segments", 0) or 0))
                        audio_note = "；音频已删除" if value.get("audio_deleted") else "；音频已保留"
                except (OSError, ValueError, TypeError):
                    chunk_status = "unknown"
            current[f"chunk:{index}"] = f"{chunk_status}:{segments}:{audio_note}"
            if chunk_status == "transcribed":
                transcribed += 1
            elif chunk_status == "failed":
                failed += 1
            elif chunk_status == "transcribing":
                transcribing += 1
            elif chunk_status == "recording":
                recording += 1
        for key, value in current.items():
            if previous.get(key) == value:
                continue
            if key == "session":
                print(f"[录制进度] Session={session_id}：状态={value}。", flush=True)
                continue
            index = key.split(":", 1)[1]
            chunk_status, segments, audio_note = value.split(":", 2)
            if chunk_status == "recording":
                text = f"[录制进度] 分片 {index}：正在录音。"
            elif chunk_status == "transcribing":
                text = f"[转写进度] 分片 {index}：开始转写。"
            elif chunk_status == "transcribed":
                text = f"[转写进度] 分片 {index}：完成；段落={segments}{audio_note}。"
            elif chunk_status == "failed":
                text = f"[转写进度] 分片 {index}：失败；音频保留，待重试。"
            else:
                text = f"[录制进度] 分片 {index}：状态={chunk_status}。"
            print(text, flush=True)
        if heartbeat and status in {"connecting", "recording", "reconnecting", "stopping"}:
            print(
                f"[录制进度] 心跳：状态={status}；分片总数={len(chunks)}；"
                f"已转写={transcribed}；转写中={transcribing}；录音中={recording}；失败={failed}。",
                flush=True,
            )
        return current

    async def _sync_active_lifecycle_status(self, session_id: str) -> None:
        """Mirror the worker's authoritative lifecycle status into controller state."""
        path = self.output_root / session_id / "lifecycle.json"
        if not path.is_file() or _is_reparse_point(path):
            return
        lifecycle = read_json_or_none(path)
        status = str((lifecycle or {}).get("status") or "").strip()
        if not status:
            return
        async with self._lock:
            active = self._state.get("active")
            if not isinstance(active, dict) or active.get("session_id") != session_id:
                return
            if active.get("status") == status:
                return
            active["status"] = status
            active["lifecycle_updated_at"] = _iso()
            self._save_state()

    def _session_stop_reason(self, session_dir: Path) -> str:
        path = session_dir / "lifecycle.json"
        if not path.is_file() or _is_reparse_point(path):
            return ""
        value = read_json_or_none(path) or {}
        return str(value.get("stop_reason") or "")

    def _transcription_completion_note(self, session_dir: Path) -> str:
        path = session_dir / "lifecycle.json"
        if not path.is_file() or _is_reparse_point(path):
            return "转写状态未知；"
        try:
            lifecycle = json.loads(path.read_text(encoding="utf-8"))
            total = int(lifecycle.get("chunks_total", 0) or 0)
            completed = int(lifecycle.get("chunks_transcribed", 0) or 0)
            failed = int(lifecycle.get("chunks_failed", 0) or 0)
        except (ValueError, OSError, TypeError):
            return "转写状态未知；"
        if failed > 0:
            return (
                f"转写完成：{completed}/{total or completed}；失败分片：{failed}；"
                "对应音频已保留，可先修复再分析。"
            )
        if total > 0:
            return f"转写完成：{completed}/{total}；"
        return "转写已完成；"

    def _failed_chunk_count(self, session_dir: Path) -> int:
        lifecycle = self._load_json_object(session_dir / "lifecycle.json")
        if lifecycle is None:
            return 0
        try:
            return max(0, int(lifecycle.get("chunks_failed", 0) or 0))
        except (TypeError, ValueError):
            return 0

    def _start_analysis_and_deliver(self, session_dir: Path) -> bool:
        """Analyse a finished recording in the background; the card is queued for the group when it is done."""
        if self.config.capture_only or self._stopping or session_dir.name in self._analysis_sessions:
            return False
        self._analysis_sessions.add(session_dir.name)
        last = self._state.get("last_job")
        if isinstance(last, dict) and last.get("session_id") == session_dir.name:
            self._state["last_job"] = {**last, "status": "analyzing", "analysis_started_at": _iso()}
            self._save_state()
        task = asyncio.create_task(self._analyze_and_deliver(session_dir))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    def busy_sessions(self) -> frozenset[str]:
        return frozenset(self._analysis_sessions)

    def _queue_to_group(self, **fields: Any) -> None:
        enqueue_send_request(self.state_root, target_type="group", target_id="oopz-group", **fields)

    async def _analyze_and_deliver(self, session_dir: Path) -> None:
        if self.config.capture_only:
            return
        status, extra = "analysis_completed", {}
        try:
            print(f"[分析进度] 开始分析 Session={session_dir.name}。", flush=True)
            output = await asyncio.to_thread(self.analysis_runner, session_dir)
            self._queue_to_group(text="", source="digest:image", image_path=str(output["png"]))
            print(f"[分析进度] Session={session_dir.name} 的图已排队发送到飞书。", flush=True)
        except Exception as error:
            status = "analysis_failed"
            reason = f"{type(error).__name__}: {str(error)[:300]}"
            extra = {"analysis_error": reason}
            print(f"[分析进度] Session={session_dir.name} 分析失败：{reason}", flush=True)
            self._queue_to_group(
                text=f"分析失败（{session_dir.name}）：{reason}。可发送“待分析”重试。", source="analysis_error")
        finally:
            self._analysis_sessions.discard(session_dir.name)
        async with self._lock:
            last = self._state.get("last_job")
            if isinstance(last, dict) and last.get("session_id") == session_dir.name:
                self._state["last_job"] = {**last, "status": status, "analysis_finished_at": _iso(), **extra}
                self._save_state()

    def _delete_session(self, session_id: str) -> None:
        root = self.output_root.resolve()
        target = (root / session_id).resolve()
        if target.parent != root:
            raise ValueError("拒绝删除：会话路径不在输出目录内")
        if _is_reparse_point(target):
            raise ValueError("拒绝删除：目标为链接")
        if not target.is_dir():
            raise ValueError("会话目录不存在")
        _validate_tree_no_links(target)
        shutil.rmtree(target)
