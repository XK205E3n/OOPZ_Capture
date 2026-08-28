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
from .pdf_reports import render_session_reports
from .controller_protocol import SenderPolicy, ControllerInboundMessage, make_reply, parse_command
from .reports import recover_interrupted_analysis_sessions, report_text, split_text
from .send_request import enqueue_send_request
from .settings import SETTABLE_KEYS, apply_setting, canonical_setting_key, setting_status
from .workflow import _delete_archived_reports, _is_reparse_point, _validate_tree_no_links


DECISION_SCHEMA = "oopz.controller.analysis_decision.v1"
START_FLOW_SCHEMA = "oopz.controller.start_flow.v1"

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
    "/oopz 离开：结束录音，结束后询问是否开始分析",
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


def _fmt_beijing(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def _analysis_usage_notice(analysis_output: Any) -> str | None:
    """Build the concise usage line delivered with a newly completed report."""
    if not isinstance(analysis_output, dict):
        return None
    result = analysis_output.get("result")
    if not isinstance(result, dict):
        return None
    model = result.get("model")
    if not isinstance(model, dict):
        return None
    usage = model.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        total_tokens = int(usage.get("total_tokens", 0) or 0)
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        reasoning_tokens = int(usage.get("reasoning_tokens", 0) or 0)
    except (TypeError, ValueError):
        return None
    text = (
        f"本次分析使用 Token：{total_tokens:,}（输入 {prompt_tokens:,}；输出 {completion_tokens:,}；"
        f"其中推理 {reasoning_tokens:,}）。"
    )
    estimate = model.get("cost_estimate")
    if not isinstance(estimate, dict):
        return text
    status = str(estimate.get("status") or "")
    if status == "subscription_estimate":
        value = estimate.get("total_estimated_cost_usd")
        try:
            return text + f"参考等价值：US${float(value):.6f}（OpenCode Go 公布参考单价估算，不代表实际套餐扣费）。"
        except (TypeError, ValueError):
            return text + "参考等价值：暂不可估算。"
    if status == "estimated":
        stages = estimate.get("stages")
        total = stages.get("total") if isinstance(stages, dict) else None
        value = total.get("estimated_cost_rmb") if isinstance(total, dict) else None
        try:
            return text + f"参考等价值：¥{float(value):.6f}（按报告内标注的参考单价估算）。"
        except (TypeError, ValueError):
            return text + "参考等价值：暂不可估算。"
    return text + "参考等价值：当前模型没有已核验参考单价，未估算。"


def _configured_analysis_label() -> str:
    provider = os.environ.get("ANALYZER_PROVIDER", "").strip()
    model = os.environ.get("ANALYZER_MODEL", "").strip()
    if provider and model:
        return f"{provider} / {model}"
    return provider or model or "已配置分析 API"

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

    def validate(self) -> None:
        if not self.authorization.allowed_sender_ids:
            raise ValueError("controller authorization requires at least one allowed sender")
        if self.consent_confirmed is not True:
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
AnalysisRunner = Callable[[Path, Any], dict[str, Any]]


async def _default_config_loader(show_browser: bool) -> Any:
    from .main import _config
    return await _config(show_browser=show_browser)


def _default_model_client() -> Any:
    from .deepseek_client import configured_analysis_client
    return configured_analysis_client()



def _default_analysis_runner(handoff_path: Path, client: Any) -> dict[str, Any]:
    from .analysis_pipeline import run_analysis

    def progress(event: dict[str, Any]) -> None:
        stage = str(event.get("stage") or "")
        if stage in {"short", "long"}:
            print(
                f"[分析进度] {stage}：{event.get('completed', 0)}/{event.get('total', 0)} "
                f"（窗口 {event.get('window_index', '?')}）",
                flush=True,
            )
        elif stage == "started":
            print(
                f"[分析进度] 已启动：Session={event.get('session_id')}；"
                f"分析接口={_configured_analysis_label()}；"
                f"300秒窗口={event.get('short_total', 0)}，60分钟窗口={event.get('long_total', 0)}；"
                f"300秒API请求={event.get('short_request_total', 0)}（每个窗口单独请求）；"
                f"并行任务={event.get('parallelism', 1)}。",
                flush=True,
            )
        elif stage == "long_started":
            print(
                f"[分析进度] 开始60分钟摘要：0/{event.get('total', 0)}；"
                f"并行任务={event.get('parallelism', 1)}。",
                flush=True,
            )
        elif stage == "final_started":
            print("[分析进度] 开始最终综合。", flush=True)
        elif stage == "report_rendered":
            print("[分析进度] 已生成报告，正在准备飞书投递。", flush=True)
        elif stage == "completed":
            print(f"[分析进度] 完成：{event.get('report_path')}", flush=True)

    return run_analysis(
        handoff_path,
        client,
        variant="configured-api",
        render_pdf=True,
        progress_reporter=progress,
    )


class ControllerService:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        config_loader: ConfigLoader = _default_config_loader,
        capture_runner: CaptureRunner = run_continuous_capture,
        analysis_runner: AnalysisRunner = _default_analysis_runner,
        model_client_factory: Callable[[], Any] = _default_model_client,
    ):
        config.validate()
        self.config = config
        self.config_loader = config_loader
        self.capture_runner = capture_runner
        self.analysis_runner = analysis_runner
        self.model_client_factory = model_client_factory
        self.state_root = config.state_root.resolve()
        self.output_root = config.output_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.output_root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self.state_root) or _is_reparse_point(self.output_root):
            raise ValueError("controller state and output roots may not be links or reparse points")
        self.state_path = self.state_root / "controller.json"
        self._decision_path = self.state_root / "analysis_decision.json"
        self._start_flow_path = self.state_root / "start_flow.json"
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._analysis_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        self._recover_active_session()
        self._recover_interrupted_analyses()
        self._reconcile_last_job()

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
        """Recover control from the worker lifecycle when controller memory is stale."""
        active_statuses = {"connecting", "recording", "reconnecting", "stopping"}
        active = self._state.get("active")
        if isinstance(active, dict):
            session_id = str(active.get("session_id") or "")
            path = self.output_root / session_id / "lifecycle.json"
            lifecycle = self._load_json_object(path) if session_id else None
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

    def _latest_analysis_lifecycle(self, session_dir: Path) -> dict[str, Any] | None:
        """Read the newest valid analysis lifecycle for a completed capture."""
        candidates = [session_dir / "analysis" / "lifecycle.json"]
        variants = session_dir / "analysis_variants"
        if variants.is_dir() and not _is_reparse_point(variants):
            candidates.extend(path / "lifecycle.json" for path in variants.iterdir() if path.is_dir())
        records: list[dict[str, Any]] = []
        for path in candidates:
            if not path.is_file() or _is_reparse_point(path):
                continue
            value = read_json_or_none(path)
            if value and str(value.get("status") or "").strip():
                records.append(value)
        if not records:
            return None
        return max(records, key=lambda item: str(item.get("updated_at") or item.get("completed_at") or ""))

    def _recover_interrupted_analyses(self) -> None:
        """Make dead analysis jobs visible and resumable after a controller restart."""
        recovered = recover_interrupted_analysis_sessions(self.output_root)
        if not recovered:
            return
        latest = recovered[0]
        session_id = str(latest["session_id"])
        last = self._state.get("last_job")
        if not isinstance(last, dict) or last.get("session_id") == session_id:
            self._state["last_job"] = {
                **(last if isinstance(last, dict) else {}),
                "session_id": session_id,
                "status": "analysis_interrupted_recoverable",
                "analysis_interrupted_at": _iso(),
                "recovered_locks": int(latest["recovered_locks"]),
            }
            self._save_state()

    def _reconcile_last_job(self) -> bool:
        """Synchronize stale controller state with authoritative worker lifecycles."""
        last = self._state.get("last_job")
        if not isinstance(last, dict):
            return False
        session_id = str(last.get("session_id") or "")
        if not session_id:
            return False
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root or not session_dir.is_dir() or _is_reparse_point(session_dir):
            return False
        updates: dict[str, Any] = {}
        capture = self._load_json_object(session_dir / "lifecycle.json")
        capture_status = str((capture or {}).get("status") or "")
        if capture_status in {"ready_for_analysis", "ready_for_analysis_with_errors"}:
            updates = {
                "status": capture_status,
                "stop_reason": str((capture or {}).get("stop_reason") or ""),
                "stopped_at": str((capture or {}).get("stopped_at") or ""),
                "chunks_total": int((capture or {}).get("chunks_total", 0) or 0),
                "chunks_transcribed": int((capture or {}).get("chunks_transcribed", 0) or 0),
                "chunks_failed": int((capture or {}).get("chunks_failed", 0) or 0),
            }

        lifecycle = self._latest_analysis_lifecycle(session_dir)
        analysis_status = str((lifecycle or {}).get("status") or "")
        if analysis_status == "ready_for_delivery":
            updates = {
                "status": "analysis_completed_report_queued",
                "analysis_completed_at": str((lifecycle or {}).get("completed_at") or (lifecycle or {}).get("updated_at") or ""),
                "report_id": str((lifecycle or {}).get("report_id") or ""),
            }
        elif analysis_status.startswith("analyzing_") or analysis_status in {"prepared", "building_final_report"}:
            updates = {"status": "analyzing", "analysis_lifecycle_status": analysis_status}
        elif analysis_status == "failed":
            updates = {"status": "analysis_failed", "analysis_failure": (lifecycle or {}).get("failure")}
        elif analysis_status == "interrupted":
            updates = {
                "status": "analysis_interrupted_recoverable",
                "analysis_recovery_reason": str((lifecycle or {}).get("recovery_reason") or ""),
            }
        has_stale_capture_error = (
            updates.get("status") in {"ready_for_analysis", "ready_for_analysis_with_errors"}
            and any(field in last for field in ("error_type", "error", "finished_at"))
        )
        if not updates or (
            all(last.get(key) == item for key, item in updates.items())
            and not has_stale_capture_error
        ):
            return False
        reconciled = {**last, **updates}
        if updates.get("status") in {"ready_for_analysis", "ready_for_analysis_with_errors"}:
            for stale_error_field in ("error_type", "error", "finished_at"):
                reconciled.pop(stale_error_field, None)
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
                decision_reply = self._process_analysis_decision(message, "leave")
                if decision_reply is not None:
                    return self._store_reply(decision_reply)
                return self._store_reply(make_reply(
                    message, command="invalid", status="rejected", at=_iso(),
                    text="不支持的指令；发送 /oopz 帮助 查看可用指令。",
                ))
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
        max_runtime: float | None,
    ) -> dict[str, Any]:
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
            consent_confirmed=True,
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
            retain_audio=self.config.retain_audio,
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
            active["analysis_admin_id"] = message.sender_id
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
        active["analysis_admin_id"] = message.sender_id
        self._save_state()
        return make_reply(
            message, command=command, status="accepted", at=_iso(),
            text=f"已提交离开指令；Session ID={session_id}。转写和最终分析完成后会进入报告 Outbox。",
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
        analysis_keys = {
            "OOPZ_ANALYSIS_MAX_PARALLELISM", "ANALYZER_PROVIDER", "ANALYZER_API_KEY",
            "ANALYZER_BASE_URL", "ANALYZER_MODEL", "ANALYZER_TIMEOUT_SECONDS",
            "ANALYZER_MAX_RETRIES", "ANALYZER_MIN_INTERVAL_SECONDS", "ANALYZER_MAX_TOKENS",
            "ANALYZER_THINKING_MAX_TOKENS", "ANALYZER_THINKING_MODE", "ANALYZER_JSON_MODE",
        }
        effect_note = "下一次分析生效" if canonical_key in analysis_keys else "下一次录音生效"
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
                    "waiting_analysis_decision": "等待管理员确认是否分析",
                    "analyzing": "正在分析",
                    "analysis_interrupted_recoverable": "分析因机器人重启或异常退出而中断；可发送“待分析”恢复",
                    "analysis_completed_report_queued": "分析已完成，报告已排队发送",
                    "analysis_failed": "分析失败",
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
                request_stop(
                    self.output_root, session_id,
                    requested_by=stop_requested_by or request.requested_by,
                    reason="operator_stop_command",
                )
            sync_interval = min(1.0, max(0.05, self.config.poll_interval_seconds))
            progress_state: dict[str, str] = {}
            next_heartbeat_at = 0.0
            while True:
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
            handoff = session_dir / "handoff" / "analyzer_request.json"
            stop_reason = self._session_stop_reason(session_dir)
            async with self._lock:
                active = self._state.get("active")
                analysis_admin_id = (
                    str(active.get("analysis_admin_id") or "")
                    if isinstance(active, dict) and active.get("session_id") == session_id
                    else ""
                )
            # A deliberate stop command transfers ownership of the analysis
            # confirmation to the member who stopped the recording.
            # Automatic exits have no override and therefore return to the
            # administrator who originally started the session.
            requester = analysis_admin_id or str((request.requested_by or {}).get("sender_id") or "")
            if (
                requester
                and handoff.is_file()
                and not _is_reparse_point(handoff)
            ):
                self._save_decision({
                    "schema_version": DECISION_SCHEMA,
                    "session_id": session_id,
                    "admin_id": requester,
                    "stage": "awaiting_analysis_decision",
                    "created_at": _iso(),
                })
                reason_note = f"（原因：{stop_reason}）" if stop_reason else ""
                transcription_note = self._transcription_completion_note(session_dir)
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=requester,
                    text=(f"录音已结束{reason_note}；Session ID={session_id}。{transcription_note}"
                          f"是否开始分析（{_configured_analysis_label()}）？回复：是 / 否。"),
                    source="analysis_decision",
                )
                final_status = "waiting_analysis_decision"
                details = {"session_id": session_id, "stop_reason": stop_reason}
            else:
                client = self.model_client_factory()
                analysis = await asyncio.to_thread(self.analysis_runner, handoff, client)
                pdf_path = analysis.get("pdf_path") if isinstance(analysis, dict) else None
                start_text, end_text = self._lifecycle_window_text(session_dir)
                _atomic_json(session_dir / "report_delivery.json", {
                    "schema_version": "oopz.controller.report_delivery.v1",
                    "pdf_path": str(pdf_path or ""),
                    "started_at_text": start_text,
                    "ended_at_text": end_text,
                    "updated_at": _iso(),
                })
                final_status = "analysis_completed"
                details = {
                    "report_id": analysis["result"]["report_id"],
                    "pdf_path": str(pdf_path or ""),
                }
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

    def _save_decision(self, value: dict[str, Any]) -> None:
        _atomic_json(self._decision_path, value)

    def _load_decision(self) -> dict[str, Any] | None:
        value = self._load_json_object(self._decision_path)
        if not isinstance(value, dict) or value.get("schema_version") != DECISION_SCHEMA:
            return None
        return value

    def _clear_decision(self) -> None:
        if self._decision_path.is_file() and not _is_reparse_point(self._decision_path):
            self._decision_path.unlink()

    def _process_analysis_decision(self, message: ControllerInboundMessage, command: str) -> dict[str, Any] | None:
        decision = self._load_decision()
        if decision is None:
            return None
        session_id = str(decision.get("session_id") or "")
        admin_id = str(decision.get("admin_id") or "")
        if not session_id or message.sender_id != admin_id:
            return None
        text = message.text.strip().casefold()
        if text in {"是", "yes", "y", "确定", "确认"}:
            session_dir = (self.output_root / session_id).resolve()
            if session_dir.parent != self.output_root.resolve() or not session_dir.is_dir():
                self._clear_decision()
                return make_reply(message, command=command, status="rejected", at=_iso(), text="会话目录不存在，已取消。")
            if not self._start_analysis_and_deliver(session_dir, admin_id):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"Session={session_id} 已在分析中，请用 /oopz 状态 查看进度。",
                )
            self._clear_decision()
            return make_reply(
                message, command=command, status="accepted", at=_iso(),
                text=f"已开始分析 Session={session_id}（{_configured_analysis_label()}）。完成后会发送报告。",
            )
        if text in {"否", "不", "no", "n", "跳过"}:
            self._clear_decision()
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已跳过分析 Session={session_id}。可用 /oopz待分析 查看未分析会话。",
            )
        return make_reply(message, command=command, status="rejected", at=_iso(), text="请回复：是 / 否。")

    def _start_analysis_and_deliver(self, session_dir: Path, admin_id: str) -> bool:
        if session_dir.name in self._analysis_sessions:
            return False
        self._analysis_sessions.add(session_dir.name)
        last = self._state.get("last_job")
        if isinstance(last, dict) and last.get("session_id") == session_dir.name:
            self._state["last_job"] = {
                **last,
                "status": "analyzing",
                "analysis_started_at": _iso(),
            }
            self._save_state()
        task = asyncio.create_task(self._analyze_and_deliver(session_dir, admin_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return True

    async def _analyze_and_deliver(self, session_dir: Path, admin_id: str) -> None:
        try:
            handoff = session_dir / "handoff" / "analyzer_request.json"
            if not handoff.is_file() or _is_reparse_point(handoff):
                raise ValueError("handoff/analyzer_request.json 不存在")
            print(
                f"[分析进度] 正在初始化 Session={session_dir.name}；"
                f"分析接口={_configured_analysis_label()}。",
                flush=True,
            )
            client = self.model_client_factory()
            analysis_output = await asyncio.to_thread(self.analysis_runner, handoff, client)
            pdf_path = analysis_output.get("pdf_path") if isinstance(analysis_output, dict) else None
            start_text, end_text = self._lifecycle_window_text(session_dir)
            _atomic_json(session_dir / "report_delivery.json", {
                "schema_version": "oopz.controller.report_delivery.v1",
                "pdf_path": str(pdf_path or ""),
                "started_at_text": start_text,
                "ended_at_text": end_text,
                "updated_at": _iso(),
            })
            pieces, pdf_path, _ = self._delivery_for_session(session_dir.name)
            usage_notice = _analysis_usage_notice(analysis_output)
            if usage_notice:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text=usage_notice, source="analysis_delivery:usage",
                )
            for piece in pieces:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text=piece, source="analysis_delivery:summary",
                )
            attachment_labels: list[str] = []
            if pdf_path:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="", source="analysis_delivery:pdf", file_path=pdf_path,
                )
                attachment_labels.append("PDF")
            internal_report = self._internal_report_path(session_dir)
            if internal_report is not None:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="", source="analysis_delivery:internal_md", file_path=str(internal_report),
                )
                attachment_labels.append("完整.md")
            if not pdf_path:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="分析已完成，但 PDF 生成失败或暂不可用；摘要和完整 Markdown 仍可正常使用。",
                    source="analysis_delivery:pdf_unavailable",
                )
            enqueue_send_request(
                self.state_root, target_type="private", target_id=admin_id,
                text=(f"报告已发送到本群（{len(pieces)} 段"
                      f"{(' + ' + ' + '.join(attachment_labels)) if attachment_labels else ''}）。"
                      "请使用随后的审查卡片决定是否发布公开版。"),
                source="publication_review:prompt",
            )
            async with self._lock:
                last = self._state.get("last_job")
                if isinstance(last, dict) and last.get("session_id") == session_dir.name:
                    report_id = ""
                    if isinstance(analysis_output, dict) and isinstance(analysis_output.get("result"), dict):
                        report_id = str(analysis_output["result"].get("report_id") or "")
                    self._state["last_job"] = {
                        **last,
                        "status": "analysis_completed_report_queued",
                        "analysis_completed_at": _iso(),
                        "report_id": report_id or str(last.get("report_id") or ""),
                    }
                    self._save_state()
            print(f"[分析进度] Session={session_dir.name} 的报告已排队发送到飞书。", flush=True)
        except Exception as error:
            print(
                f"[分析进度] Session={session_dir.name} 分析失败：{type(error).__name__}: {str(error)[:300]}",
                flush=True,
            )
            enqueue_send_request(
                self.state_root, target_type="private", target_id=admin_id,
                text=(f"分析失败；Session={session_dir.name}；{type(error).__name__}: "
                      f"{str(error)[:260]}。可稍后使用 /oopz 待分析 重试。"),
                source="analysis_error",
            )
            async with self._lock:
                last = self._state.get("last_job")
                if isinstance(last, dict) and last.get("session_id") == session_dir.name:
                    self._state["last_job"] = {
                        **last,
                        "status": "analysis_failed",
                        "analysis_failed_at": _iso(),
                        "analysis_error": f"{type(error).__name__}: {str(error)[:500]}",
                    }
                    self._save_state()
        finally:
            self._analysis_sessions.discard(session_dir.name)

    def _session_report_pieces(self, session_dir: Path) -> list[str]:
        # Prefer the newest complete report bundle. A generic handoff
        # report_messages.jsonl can belong to an older analysis route and would
        # otherwise make /oopz报告 silently send stale content.
        full_report = self._internal_report_path(session_dir)
        if full_report is not None:
            for candidate in (
                full_report.with_name("summary.text.md"),
                full_report.with_name("summary.public.md"),
                full_report,
            ):
                if candidate.is_file() and not _is_reparse_point(candidate):
                    return split_text(report_text(candidate))
        report_messages_path = session_dir / "handoff" / "report_messages.jsonl"
        if report_messages_path.is_file() and not _is_reparse_point(report_messages_path):
            pieces: list[str] = []
            for line in report_messages_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except (ValueError, TypeError):
                    continue
                text = str((value or {}).get("text") or "").strip()
                if text:
                    pieces.append(text)
            if pieces:
                return pieces
        candidates: list[Path] = []
        variants = session_dir / "analysis_variants"
        if variants.is_dir():
            for variant_dir in sorted(variants.iterdir()):
                if variant_dir.is_dir():
                    candidates.append(variant_dir / "summary.text.md")
                    candidates.append(variant_dir / "summary.public.md")
                    candidates.append(variant_dir / "summary.md")
        candidates.append(session_dir / "analysis" / "summary.text.md")
        candidates.append(session_dir / "analysis" / "summary.public.md")
        candidates.append(session_dir / "analysis" / "summary.md")
        for candidate in candidates:
            if candidate.is_file() and not _is_reparse_point(candidate):
                return split_text(report_text(candidate))
        raise ValueError("报告文本不存在")

    def _lifecycle_window_text(self, session_dir: Path) -> tuple[str, str]:
        path = session_dir / "lifecycle.json"
        if not path.is_file() or _is_reparse_point(path):
            return "", ""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return "", ""
        if not isinstance(value, dict):
            return "", ""
        start = str(value.get("capture_started_at") or value.get("started_at") or "")
        end = str(value.get("stopped_at") or value.get("finished_at") or "")
        return _fmt_beijing(start), _fmt_beijing(end)

    def _delivery_for_session(self, session_id: str) -> tuple[list[str], str | None, str]:
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root.resolve() or not session_dir.is_dir():
            raise ValueError("会话目录不存在")
        pieces = self._session_report_pieces(session_dir)
        pdf_path: str | None = None
        start_text = ""
        end_text = ""
        meta_path = session_dir / "report_delivery.json"
        if meta_path.is_file() and not _is_reparse_point(meta_path):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                meta = {}
            if isinstance(meta, dict):
                pdf_path = str(meta.get("pdf_path") or "").strip() or None
                start_text = str(meta.get("started_at_text") or "")
                end_text = str(meta.get("ended_at_text") or "")
        if not start_text or not end_text:
            start_text, end_text = self._lifecycle_window_text(session_dir)
        header = ""
        if start_text or end_text:
            header = f"录音开始：{start_text}\n录音结束：{end_text}\n\n"
        if header and pieces:
            pieces = [header + pieces[0]] + pieces[1:]
        if pdf_path is not None:
            pdf = Path(pdf_path)
            if not pdf.is_file() or _is_reparse_point(pdf):
                pdf_path = None
        if pdf_path is None:
            pdf_path = self._render_missing_pdf(session_dir)
        return pieces, pdf_path, session_id

    def _render_missing_pdf(self, session_dir: Path) -> str | None:
        """Create a historical PDF on demand when a report predates PDF output."""
        full_report = self._internal_report_path(session_dir)
        source = None
        if full_report is not None:
            public_report = full_report.with_name("summary.public.md")
            source = public_report if public_report.is_file() and not _is_reparse_point(public_report) else full_report
        if source is None:
            return None
        try:
            return str(render_session_reports(session_dir, [(source, "report")])[0])
        except Exception:
            # The text and internal Markdown remain deliverable. The selection
            # reply accurately states that the PDF is unavailable.
            return None

    def _internal_report_path(self, session_dir: Path) -> Path | None:
        """Return the newest complete internal report, never the public PDF source."""
        try:
            root = session_dir.resolve()
        except OSError:
            return None
        if root.parent != self.output_root.resolve() or not root.is_dir() or _is_reparse_point(root):
            return None
        candidates: list[Path] = [root / "analysis" / "summary.md"]
        variants = root / "analysis_variants"
        if variants.is_dir() and not _is_reparse_point(variants):
            candidates.extend(path / "summary.md" for path in variants.iterdir() if path.is_dir())
        valid = [path for path in candidates if path.is_file() and not _is_reparse_point(path)]
        if not valid:
            return None
        return max(valid, key=lambda path: (path.stat().st_mtime_ns, str(path).casefold()))
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
        _delete_archived_reports(root, target)
        shutil.rmtree(target)
