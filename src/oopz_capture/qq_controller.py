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

from .analysis_pipeline import run_analysis
from .continuous import (
    ContinuousRequest, repair_continuous_session, request_stop, run_continuous_capture,
)
from .deepseek_client import DeepSeekClient, DeepSeekConfig
from .identifiers import new_session_id
from .pdf_reports import render_session_reports
from .process_utils import pid_is_running
from .qq_outbox import cleanup_outbox, enqueue_session_messages
from .qq_protocol import AuthorizationPolicy, QQInboundMessage, make_reply, parse_command
from .qq_reports import find_pending_sessions, find_recent_reports, report_text, split_text
from .qq_send_request import enqueue_send_request
from .qq_settings import SETTABLE_KEYS, apply_setting, canonical_setting_key, setting_status, upsert_env
from .workflow import _delete_archived_reports, _is_reparse_point, _validate_tree_no_links


FLOW_SCHEMA = "oopz.qq.report_flow.v1"
ADMINS_SCHEMA = "oopz.qq.admins.v1"
DECISION_SCHEMA = "oopz.qq.analysis_decision.v1"
PENDING_SCHEMA = "oopz.qq.pending_flow.v1"
START_FLOW_SCHEMA = "oopz.qq.start_flow.v1"


HELP_TEXT = "\n".join([
    "/oopz 开始 [秒数]：依次选择域和语音频道后开始录音，可指定时长（秒，或 5m/1h）",
    "/oopz 离开：结束录音，结束后询问是否开始分析",
    "/oopz 状态：查看当前录音任务状态",
    "/oopz 报告：列出最近 7 份报告，选择后发送精简版（最终总结+60分钟摘要+PDF），可转发",
    "/oopz 详细报告：列出最近 7 份报告，选择后发送正常总结并附带内部完整 .md 文件",
    "/oopz 待分析：查看未分析的录制会话，可选择分析或删除",
    "/oopz 设置 变量=值：修改运行设置；先用 /oopz 设置状态 查看可用变量、当前值和说明",
    "/oopz 设置状态：查看可由 QQ 修改的变量（密码、手机号和密钥打码）",
    "/oopz 增加管理员 QQ号：添加管理员（仅管理员可用）",
    "/oopz 帮助：显示本帮助",
    "多步流程提示：报告转发时回复 群聊/好友/跳过，再输入群号或 QQ 号。",
])


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def acquire_instance_lock(state_root: Path) -> Path:
    state_root = state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / "controller.lock"
    if path.exists():
        if not path.is_file() or _is_reparse_point(path):
            raise RuntimeError("unsafe controller instance lock")
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            pid = int(current.get("pid", 0))
            if pid_is_running(pid):
                raise RuntimeError(f"QQ controller is already running with PID={pid}")
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        path.unlink()
    try:
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "created_at": _iso()}, stream)
            stream.write("\n")
    except FileExistsError as error:
        raise RuntimeError("QQ controller instance lock was acquired concurrently") from error
    return path


def release_instance_lock(path: Path) -> None:
    if path.is_file() and not _is_reparse_point(path):
        path.unlink()


def _csv(name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())


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
    provider = os.environ.get("ANALYZER_PROVIDER", "openai-compatible").strip()
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
class QQControllerConfig:
    output_root: Path
    state_root: Path
    authorization: AuthorizationPolicy
    consent_confirmed: bool
    chunk_seconds: int = 300
    cutoff_local_hour: int = 4
    language: str = "auto"
    retain_audio: bool = False
    transcription_repair_attempts: int = 1
    report_flow_timeout_seconds: int = 180
    processing_deadline_seconds: int = 900
    retention_hours: int = 168
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
    show_browser: bool = False

    @classmethod
    def from_env(cls) -> "QQControllerConfig":
        allowed_senders = _csv("OOPZ_QQ_ALLOWED_SENDERS")
        allowed_chats = _csv("OOPZ_QQ_ALLOWED_CHATS")
        config = cls(
            output_root=Path(os.environ.get("OOPZ_OUTPUT_ROOT", "output")),
            state_root=Path(os.environ.get("OOPZ_QQ_STATE_ROOT", "controller_state")),
            authorization=AuthorizationPolicy(allowed_senders, allowed_chats),
            consent_confirmed=_env_bool("OOPZ_RECORDING_CONSENT_CONFIRMED"),
            chunk_seconds=int(os.environ.get("OOPZ_CHUNK_SECONDS", "300")),
            cutoff_local_hour=int(os.environ.get("OOPZ_CUTOFF_LOCAL_HOUR", "4")),
            language=os.environ.get("OOPZ_LANGUAGE", "auto").strip(),
            retain_audio=_env_bool("OOPZ_RETAIN_AUDIO"),
            transcription_repair_attempts=int(os.environ.get("OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS", "1")),
            report_flow_timeout_seconds=int(os.environ.get("OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS", "180")),
            processing_deadline_seconds=int(os.environ.get("OOPZ_PROCESSING_DEADLINE_SECONDS", "900")),
            retention_hours=int(os.environ.get("OOPZ_RETENTION_HOURS", "168")),
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
            device=os.environ.get("OOPZ_DEVICE", "cpu").strip(),
            show_browser=_env_bool("OOPZ_SHOW_BROWSER"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if not self.authorization.allowed_sender_ids:
            raise ValueError("OOPZ_QQ_ALLOWED_SENDERS is required; the controller defaults to deny-all")
        if self.consent_confirmed is not True:
            raise ValueError("OOPZ_RECORDING_CONSENT_CONFIRMED must be true before remote recording can start")
        if self.device not in {"cpu", "cuda:0"}:
            raise ValueError("OOPZ_DEVICE must be cpu or cuda:0")
        if not 0 <= self.transcription_repair_attempts <= 3:
            raise ValueError("OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS must be 0 to 3")
        if not 30 <= self.report_flow_timeout_seconds <= 1800:
            raise ValueError("OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS must be 30 to 1800")
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
                f"300秒批次={event.get('short_batch_total', 0)}（每批最多{event.get('short_batch_size', 1)}个窗口）；"
                f"并行任务={event.get('parallelism', 1)}。",
                flush=True,
            )
        elif stage == "long_started":
            print(
                f"[分析进度] 开始60分钟摘要：0/{event.get('total', 0)}；"
                f"并行任务={event.get('parallelism', 1)}。",
                flush=True,
            )
        elif stage == "short_batch_fallback":
            print(
                "[分析进度] 300秒批量结果无效，已自动降级为逐窗口重试；"
                f"本批窗口={event.get('batch_size', 0)}；原因={event.get('error', 'unknown')}。",
                flush=True,
            )
        elif stage == "final_started":
            print("[分析进度] 开始最终总结（思考模式）。", flush=True)
        elif stage == "report_rendered":
            print("[分析进度] 已生成报告，正在准备 QQ 投递。", flush=True)
        elif stage == "completed":
            print(f"[分析进度] 完成：{event.get('report_path')}", flush=True)

    return run_analysis(
        handoff_path,
        client,
        variant="configured-api",
        render_pdf=True,
        progress_reporter=progress,
    )


class QQControllerService:
    def __init__(
        self,
        config: QQControllerConfig,
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
        self._flow_path = self.state_root / "report_flow.json"
        self._decision_path = self.state_root / "analysis_decision.json"
        self._pending_flow_path = self.state_root / "pending_flow.json"
        self._start_flow_path = self.state_root / "start_flow.json"
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._analysis_sessions: set[str] = set()
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._state = self._load_state()
        self._reconcile_last_job()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            if _is_reparse_point(self.state_path):
                raise ValueError("unsafe controller state file")
            try:
                value = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                value = None
            if isinstance(value, dict) and value.get("schema_version") == "oopz.qq.controller.state.v1":
                active = value.get("active")
                if isinstance(active, dict):
                    value["active"] = None
                    value["last_job"] = {**active, "status": "controller_restarted", "updated_at": _iso()}
                return value
        return {
            "schema_version": "oopz.qq.controller.state.v1",
            "active": None,
            "last_job": None,
            "updated_at": _iso(),
        }

    def _save_state(self) -> None:
        self._state["updated_at"] = _iso()
        _atomic_json(self.state_path, self._state)

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
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(value, dict) and str(value.get("status") or "").strip():
                records.append(value)
        if not records:
            return None
        return max(records, key=lambda item: str(item.get("updated_at") or item.get("completed_at") or ""))

    def _reconcile_last_job(self) -> bool:
        """Synchronize stale controller state with the authoritative analysis lifecycle."""
        last = self._state.get("last_job")
        if not isinstance(last, dict):
            return False
        session_id = str(last.get("session_id") or "")
        if not session_id:
            return False
        session_dir = (self.output_root / session_id).resolve()
        if session_dir.parent != self.output_root or not session_dir.is_dir() or _is_reparse_point(session_dir):
            return False
        lifecycle = self._latest_analysis_lifecycle(session_dir)
        if lifecycle is None:
            return False
        analysis_status = str(lifecycle.get("status") or "")
        updates: dict[str, Any] = {}
        if analysis_status == "ready_for_qq":
            updates = {
                "status": "analysis_completed_report_queued",
                "analysis_completed_at": str(lifecycle.get("completed_at") or lifecycle.get("updated_at") or ""),
                "report_id": str(lifecycle.get("report_id") or ""),
            }
        elif analysis_status.startswith("analyzing_") or analysis_status in {"prepared", "building_final_report"}:
            updates = {"status": "analyzing", "analysis_lifecycle_status": analysis_status}
        elif analysis_status == "failed":
            updates = {"status": "analysis_failed", "analysis_failure": lifecycle.get("failure")}
        if not updates or all(last.get(key) == item for key, item in updates.items()):
            return False
        self._state["last_job"] = {**last, **updates}
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
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _store_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        _atomic_json(self._reply_path(str(reply["message_id"])), reply)
        return reply

    async def handle(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        message = QQInboundMessage.from_dict(raw_message)
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
                flow_reply = self._process_flow(message, "reports")
                if flow_reply is not None:
                    return self._store_reply(flow_reply)
                pending_reply = self._process_pending_flow(message, "pending_sessions")
                if pending_reply is not None:
                    return self._store_reply(pending_reply)
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
            if command == "reports":
                return self._store_reply(self._reports(message, command))
            if command == "report_full":
                return self._store_reply(self._reports_full(message, command))
            if command == "add_admin":
                return self._store_reply(self._add_admin(message, command))
            if command == "pending_sessions":
                return self._store_reply(self._pending_sessions(message, command))
            raise AssertionError(command)

    def _authorize(self, message: QQInboundMessage) -> bool:
        # The command surface is private-chat only. Enforce this again in the
        # controller even though the OneBot gateway already discards groups;
        # directory submissions and future adapters must not bypass the rule.
        return (
            message.chat_type == "private"
            and message.chat_id == message.sender_id
            and message.sender_id in self._read_admins()
        )

    def _read_admins(self) -> set[str]:
        admins: set[str] = set(self.config.authorization.allowed_sender_ids)
        path = self.state_root / "admins.json"
        if path.is_file() and not _is_reparse_point(path):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                value = {}
            ids = value.get("admin_ids") if isinstance(value, dict) else None
            if isinstance(ids, list):
                admins.update(str(item) for item in ids if str(item).isdigit())
        return admins

    def _save_admins(self, admins: set[str]) -> None:
        _atomic_json(self.state_root / "admins.json", {
            "schema_version": ADMINS_SCHEMA,
            "admin_ids": sorted(admins),
            "updated_at": _iso(),
        })

    @staticmethod
    def _admin_flow_path(base: Path, admin_id: str) -> Path:
        if not admin_id.isascii() or not admin_id.isdigit():
            raise ValueError("invalid administrator id for flow state")
        return base.with_name(f"{base.stem}.{admin_id}{base.suffix}")

    @staticmethod
    def _load_json_object(path: Path) -> dict[str, Any] | None:
        if not path.is_file() or _is_reparse_point(path):
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _load_flow(self, admin_id: str | None = None) -> dict[str, Any] | None:
        candidates: list[Path] = []
        if admin_id:
            candidates.extend((self._admin_flow_path(self._flow_path, admin_id), self._flow_path))
        else:
            candidates.append(self._flow_path)
            candidates.extend(sorted(self.state_root.glob("report_flow.*.json")))
        for path in candidates:
            value = self._load_json_object(path)
            if value is None or value.get("schema_version") != FLOW_SCHEMA:
                continue
            if admin_id and str(value.get("admin_id") or "") != admin_id:
                continue
            if self._expire_report_flow(path, value):
                continue
            return value
        return None

    def _save_flow(self, value: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc)
        value["updated_at"] = now.isoformat(timespec="milliseconds")
        value["expires_at"] = (
            now + timedelta(seconds=self.config.report_flow_timeout_seconds)
        ).isoformat(timespec="milliseconds")
        admin_id = str(value.get("admin_id") or "")
        _atomic_json(self._admin_flow_path(self._flow_path, admin_id), value)
        legacy = self._load_json_object(self._flow_path)
        if legacy is not None and str(legacy.get("admin_id") or "") == admin_id:
            self._flow_path.unlink(missing_ok=True)

    def _clear_flow(self, admin_id: str | None = None) -> None:
        paths = [self._admin_flow_path(self._flow_path, admin_id)] if admin_id else []
        if not admin_id:
            paths.extend(self.state_root.glob("report_flow.*.json"))
        legacy = self._load_json_object(self._flow_path)
        if not admin_id or (legacy is not None and str(legacy.get("admin_id") or "") == admin_id):
            paths.append(self._flow_path)
        for path in paths:
            if path.is_file() and not _is_reparse_point(path):
                path.unlink()

    def _expire_report_flow(
        self,
        path: Path,
        value: dict[str, Any],
        *,
        now: datetime | None = None,
        notified_admins: set[str] | None = None,
    ) -> bool:
        expires_text = str(value.get("expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            # Legacy flow files did not have expires_at. Age them from their
            # last update so an old prompt cannot capture an unrelated reply.
            try:
                updated_at = datetime.fromisoformat(
                    str(value.get("updated_at") or "").replace("Z", "+00:00")
                )
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                expires_at = updated_at + timedelta(seconds=self.config.report_flow_timeout_seconds)
            except (TypeError, ValueError):
                return False
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if current < expires_at.astimezone(timezone.utc):
            return False
        if path.is_file() and not _is_reparse_point(path):
            path.unlink()
        admin_id = str(value.get("admin_id") or "")
        already_notified = notified_admins is not None and admin_id in notified_admins
        if admin_id in self._read_admins() and not already_notified:
            stage = str(value.get("stage") or "")
            transfer_stage = stage in {"awaiting_target_type", "awaiting_target_id"}
            enqueue_send_request(
                self.state_root,
                target_type="private",
                target_id=admin_id,
                text=(
                    "3分钟内未收到有效的转发回复，本次转发已自动跳过。"
                    if transfer_stage else
                    "3分钟内未收到有效的报告选择，本次操作已自动取消。"
                ),
                source="report_flow_timeout",
            )
            if notified_admins is not None:
                notified_admins.add(admin_id)
        return True

    def _expire_report_flows(self, *, now: datetime | None = None) -> int:
        paths = [self._flow_path, *sorted(self.state_root.glob("report_flow.*.json"))]
        expired = 0
        notified_admins: set[str] = set()
        for path in paths:
            value = self._load_json_object(path)
            if value is None or value.get("schema_version") != FLOW_SCHEMA:
                continue
            if self._expire_report_flow(
                path, value, now=now, notified_admins=notified_admins,
            ):
                expired += 1
        return expired

    def _reports(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        self._clear_pending_flow(message.sender_id)
        reports = find_recent_reports(self.output_root, 7)
        if not reports:
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text="没有找到最终报告。",
            )
        lines: list[str] = []
        for index, item in enumerate(reports, start=1):
            report_id = item.get("report_id") or "无"
            start_text, end_text = self._lifecycle_window_text(
                self.output_root / str(item["session_id"]),
            )
            recording_window = (
                f"{start_text} 至 {end_text}" if start_text and end_text
                else (_fmt_beijing(str(item["modified"])) or "时间未知")
            )
            lines.append(
                f"{index}. 录音 {recording_window} | Session={item['session_id']} | Report={report_id}"
            )
        self._save_flow({
            "schema_version": FLOW_SCHEMA,
            "admin_id": message.sender_id,
            "stage": "awaiting_report_selection",
            "reports": [
                {
                    "session_id": item["session_id"],
                    "summary_path": str(item["summary_path"]),
                    "report_id": item.get("report_id"),
                }
                for item in reports
            ],
            "selected_index": None,
            "target_type": None,
        })
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="最近 7 份最终报告：\n" + "\n".join(lines)
            + "\n\n回复编号选择要输出的报告；回复 跳过 取消。",
        )

    def _reports_full(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        self._clear_pending_flow(message.sender_id)
        reports = find_recent_reports(self.output_root, 7)
        if not reports:
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text="没有找到最终报告。",
            )
        lines = [f"{index}. Session={item['session_id']}" for index, item in enumerate(reports, start=1)]
        self._save_flow({
            "schema_version": FLOW_SCHEMA,
            "admin_id": message.sender_id,
            "kind": "full_report",
            "stage": "awaiting_full_report_selection",
            "reports": [
                {
                    "session_id": item["session_id"],
                    "full_summary_path": str(item["full_summary_path"]),
                    "report_id": item.get("report_id"),
                }
                for item in reports
            ],
            "selected_index": None,
        })
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="最近 7 份内部完整报告（.md，含 300 秒短总结与 Token 明细）：\n"
            + "\n".join(lines) + "\n\n回复编号选择要输出的报告；回复 跳过 取消。",
        )
    def _process_flow(self, message: QQInboundMessage, command: str) -> dict[str, Any] | None:
        flow = self._load_flow(message.sender_id)
        if flow is None:
            return None
        text = message.text.strip()
        stage = str(flow.get("stage") or "")
        if stage == "awaiting_full_report_selection":
            if text.casefold() in {"跳过", "取消", "no", "cancel"}:
                self._clear_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="已取消。")
            if not text.isdigit():
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="请回复报告编号（1-7），或回复 跳过 取消。",
                )
            index = int(text)
            reports = flow.get("reports") or []
            if not 1 <= index <= len(reports):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"编号必须在 1-{len(reports)} 之间。",
                )
            selected = reports[index - 1]
            full_path = Path(str(selected.get("full_summary_path") or ""))
            if _is_reparse_point(full_path) or not full_path.is_file():
                self._clear_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="内部报告文件不存在，已取消。",
                )
            session_id = str(selected.get("session_id") or "")
            try:
                pieces, _, _ = self._delivery_for_session(session_id)
            except ValueError as error:
                self._clear_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="rejected", at=_iso(), text=str(error),
                )
            for piece in pieces:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=message.sender_id,
                    text=piece, source="report_full:text",
                )
            enqueue_send_request(
                self.state_root, target_type="private", target_id=message.sender_id,
                text="", source="report_full:md", file_path=str(full_path),
            )
            self._clear_flow(message.sender_id)
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已发送总结（{len(pieces)} 段）和内部完整 .md 文件。",
            )
        if stage == "awaiting_report_selection":
            if text.casefold() in {"跳过", "取消", "no", "cancel"}:
                self._clear_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="已取消。")
            if not text.isdigit():
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="请回复报告编号（1-7），或回复 跳过 取消。",
                )
            index = int(text)
            reports = flow.get("reports") or []
            if not 1 <= index <= len(reports):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"编号必须在 1-{len(reports)} 之间。",
                )
            selected = reports[index - 1]
            try:
                pieces, pdf_path, _ = self._delivery_for_session(str(selected["session_id"]))
            except ValueError as error:
                self._clear_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="rejected", at=_iso(), text=str(error),
                )
            reports[index - 1] = {**selected, "pieces": pieces, "pdf_path": pdf_path}
            for piece in pieces:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=message.sender_id,
                    text=piece, source="report_forward:admin",
                )
            if pdf_path:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=message.sender_id,
                    text="", source="report_forward:admin_pdf", file_path=pdf_path,
                )
            full_report_path = self._internal_report_path(self.output_root / str(selected["session_id"]))
            if full_report_path is not None:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=message.sender_id,
                    text="", source="report_forward:admin_internal_md", file_path=str(full_report_path),
                )
            flow["stage"] = "awaiting_target_type"
            flow["selected_index"] = index
            self._save_flow(flow)
            attachment_text = " + PDF + 完整.md" if pdf_path else " + 完整.md（PDF 暂不可用）"
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已发送报告（{len(pieces)} 段{attachment_text}）到管理员。是否同时输出到其他目标？回复：群聊 / 好友 / 跳过；3分钟未回复将自动跳过。",
            )
        if stage == "awaiting_target_type":
            choice = text.casefold()
            if choice in {"跳过", "不需要", "不", "no", "cancel"}:
                self._clear_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="完成，未转发。")
            if choice in {"群聊", "群", "group"}:
                flow["target_type"] = "group"
                flow["stage"] = "awaiting_target_id"
                self._save_flow(flow)
                return make_reply(message, command=command, status="completed", at=_iso(), text="请输入群号（5-20 位数字）；3分钟未回复将自动跳过。")
            if choice in {"好友", "私聊", "friend", "private"}:
                flow["target_type"] = "private"
                flow["stage"] = "awaiting_target_id"
                self._save_flow(flow)
                return make_reply(message, command=command, status="completed", at=_iso(), text="请输入好友 QQ 号（5-20 位数字）；3分钟未回复将自动跳过。")
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="请回复：群聊 / 好友 / 跳过。",
            )
        if stage == "awaiting_target_id":
            target_id = text.strip()
            if not target_id.isascii() or not target_id.isdigit() or not 5 <= len(target_id) <= 20:
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="目标号格式无效（5-20 位数字），请重新输入。",
                )
            target_type = str(flow.get("target_type") or "private")
            selected_index = int(flow.get("selected_index") or 1)
            reports = flow.get("reports") or []
            selected = reports[selected_index - 1] if 1 <= selected_index <= len(reports) else None
            if selected is None:
                self._clear_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="报告信息不存在，已取消。",
                )
            pdf_path = str(selected.get("pdf_path") or "").strip() or None
            if not pdf_path or _is_reparse_point(Path(pdf_path)) or not Path(pdf_path).is_file():
                self._clear_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="PDF 报告不存在，已取消转发。",
                )
            label = "群" if target_type == "group" else "好友"
            pieces = self._flow_pieces(selected)
            for piece in pieces:
                enqueue_send_request(
                    self.state_root, target_type=target_type, target_id=target_id,
                    text=piece, source="report_forward:summary",
                )
            enqueue_send_request(
                self.state_root, target_type=target_type, target_id=target_id,
                text="", source="report_forward:pdf", file_path=pdf_path,
                notify_admin_id=message.sender_id,
            )
            self._clear_flow(message.sender_id)
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已提交基本总结（{len(pieces)} 段）和 PDF 报告到{label} {target_id}。",
            )
        return None

    def _add_admin(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        match = re.match(r"^/oopz\s*(?:增加管理员|addadmin)\s*(\d{5,20})\s*$", message.text, re.IGNORECASE)
        if not match:
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="格式：/oopz增加管理员 QQ号",
            )
        new_id = match.group(1)
        admins = self._read_admins()
        admins.add(new_id)
        self._save_admins(admins)
        upsert_env("OOPZ_QQ_ALLOWED_SENDERS", ",".join(sorted(admins)))
        upsert_env("OOPZ_QQ_ADMIN_IDS", ",".join(sorted(admins)))
        enqueue_send_request(
            self.state_root, target_type="private", target_id=new_id,
            text="你已成为管理员。", source="admin_welcome",
        )
        enqueue_send_request(
            self.state_root, target_type="private", target_id=new_id,
            text=HELP_TEXT, source="admin_help",
        )
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="已添加管理员 " + new_id + "；当前管理员：" + "、".join(sorted(admins)),
        )
    async def _start(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        # An explicit top-level command supersedes this administrator's stale
        # report/pending-session conversation, without touching another admin.
        self._clear_flow(message.sender_id)
        self._clear_pending_flow(message.sender_id)
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
        config = await self.config_loader(self.config.show_browser)
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
        config = await self.config_loader(self.config.show_browser)
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
        try:
            value = json.loads(self._start_flow_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if not isinstance(value, dict) or value.get("schema_version") != START_FLOW_SCHEMA:
            return None
        return value

    def _clear_start_flow(self) -> None:
        if self._start_flow_path.is_file() and not _is_reparse_point(self._start_flow_path):
            self._start_flow_path.unlink()

    async def _process_start_flow(
        self, message: QQInboundMessage, command: str,
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
        self, message: QQInboundMessage, command: str, *, area_id: str,
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

    def _leave(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        active = self._state.get("active")
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
                reason="qq_leave_command",
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

    def _set_config(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
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
            live_config_fields: dict[str, tuple[str, Callable[[str], Any]]] = {
                "OOPZ_CUTOFF_LOCAL_HOUR": ("cutoff_local_hour", int),
                "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS": ("empty_channel_timeout_seconds", float),
                "OOPZ_CHUNK_SECONDS": ("chunk_seconds", int),
                "OOPZ_LANGUAGE": ("language", str),
                "OOPZ_RETAIN_AUDIO": ("retain_audio", lambda raw: raw == "true"),
                "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS": ("transcription_repair_attempts", int),
                "OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS": ("report_flow_timeout_seconds", int),
                "OOPZ_RETENTION_HOURS": ("retention_hours", int),
                "OOPZ_DEVICE": ("device", str),
            }
            if canonical_key in live_config_fields:
                field_name, parser = live_config_fields[canonical_key]
                self.config = replace(self.config, **{field_name: parser(os.environ[canonical_key])})
                self.config.validate()
        except ValueError as error:
            return make_reply(
                message, command=command, status="rejected", at=_iso(), text=str(error),
            )
        restart_keys = {
            "OOPZ_ONEBOT_SEND_FAILURE_COOLDOWN_SECONDS",
            "OOPZ_QQ_WATCHDOG_POLL_SECONDS",
            "OOPZ_QQ_WATCHDOG_PORT_GRACE_SECONDS",
            "OOPZ_QQ_WATCHDOG_GATEWAY_GRACE_SECONDS",
            "OOPZ_QQ_WATCHDOG_ESCALATION_SECONDS",
        }
        analysis_keys = {
            "OOPZ_ANALYSIS_MAX_PARALLELISM", "ANALYZER_PROVIDER", "ANALYZER_API_KEY",
            "ANALYZER_BASE_URL", "ANALYZER_MODEL", "ANALYZER_TIMEOUT_SECONDS",
            "ANALYZER_MAX_RETRIES", "ANALYZER_MIN_INTERVAL_SECONDS", "ANALYZER_MAX_TOKENS",
            "ANALYZER_THINKING_MAX_TOKENS", "ANALYZER_THINKING_MODE", "ANALYZER_JSON_MODE",
        }
        immediate_keys = {"OOPZ_QQ_REPORT_FLOW_TIMEOUT_SECONDS"}
        effect_note = (
            "需重启全流程后生效" if canonical_key in restart_keys
            else ("下一次分析生效" if canonical_key in analysis_keys else "下一次录音生效")
        )
        if canonical_key in immediate_keys:
            effect_note = "立即生效"
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text=f"已设置 {canonical_key}：{masked}。已保存到 .env；{effect_note}。",
        )

    def _settings_status(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        lines = [
            f"{key} = {value}（{SETTABLE_KEYS[key]['description']}）"
            for key, value in setting_status().items()
        ]
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="当前设置（密码/手机号已打码）：\n" + "\n".join(lines),
        )
    def _status(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        active = self._state.get("active")
        if not isinstance(active, dict):
            self._reconcile_last_job()
            last = self._state.get("last_job")
            suffix = ""
            if isinstance(last, dict) and last.get("session_id"):
                raw_status = str(last.get("status") or "unknown")
                status_text = {
                    "waiting_analysis_decision": "等待管理员确认是否分析",
                    "analyzing": "正在分析",
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
            try:
                loaded = json.loads(lifecycle_path.read_text(encoding="utf-8"))
                lifecycle = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError, TypeError):
                lifecycle = {}
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
            oopz_config = await self.config_loader(self.config.show_browser)
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
                    reason="qq_leave_command",
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
                    await self._sync_active_lifecycle_status(session_id)
                    now = time.monotonic()
                    progress_state = self._print_capture_progress(
                        session_id, progress_state, heartbeat=now >= next_heartbeat_at,
                    )
                    if now >= next_heartbeat_at:
                        next_heartbeat_at = now + 60.0
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
            # A deliberate QQ leave command transfers ownership of the analysis
            # confirmation to the administrator who stopped the recording.
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
                    "schema_version": "oopz.qq.report_delivery.v1",
                    "pdf_path": str(pdf_path or ""),
                    "started_at_text": start_text,
                    "ended_at_text": end_text,
                    "updated_at": _iso(),
                })
                queued = enqueue_session_messages(session_dir, self.state_root)
                final_status = "report_queued"
                details = {
                    "report_id": analysis["result"]["report_id"],
                    "outbox_messages": len(queued),
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
        try:
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
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
        try:
            lifecycle = json.loads(path.read_text(encoding="utf-8"))
            status = str(lifecycle.get("status") or "").strip()
        except (ValueError, OSError, TypeError):
            return
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
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return ""
        return str((value or {}).get("stop_reason") or "")

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

    def _process_analysis_decision(self, message: QQInboundMessage, command: str) -> dict[str, Any] | None:
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
                "schema_version": "oopz.qq.report_delivery.v1",
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
                    text=usage_notice, source="report_forward:analysis_usage",
                )
            for piece in pieces:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text=piece, source="report_forward:analysis",
                )
            attachment_labels: list[str] = []
            if pdf_path:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="", source="report_forward:analysis_pdf", file_path=pdf_path,
                )
                attachment_labels.append("PDF")
            internal_report = self._internal_report_path(session_dir)
            if internal_report is not None:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="", source="report_forward:analysis_internal_md", file_path=str(internal_report),
                )
                attachment_labels.append("完整.md")
            if not pdf_path:
                enqueue_send_request(
                    self.state_root, target_type="private", target_id=admin_id,
                    text="分析已完成，但 PDF 生成失败或暂不可用；摘要和完整 Markdown 仍可正常使用。",
                    source="report_forward:pdf_unavailable",
                )
            self._save_flow({
                "schema_version": FLOW_SCHEMA,
                "admin_id": admin_id,
                "stage": "awaiting_target_type",
                "reports": [{
                    "session_id": session_dir.name,
                    "summary_path": "",
                    "report_id": None,
                    "pieces": pieces,
                    "pdf_path": pdf_path,
                }],
                "selected_index": 1,
                "target_type": None,
            })
            enqueue_send_request(
                self.state_root, target_type="private", target_id=admin_id,
                text=(f"报告已发送（{len(pieces)} 段"
                      f"{(' + ' + ' + '.join(attachment_labels)) if attachment_labels else ''}）。"
                      "是否进一步输出到其他对象？回复：群聊 / 好友 / 跳过。"),
                source="report_forward:prompt",
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
            print(f"[分析进度] Session={session_dir.name} 的报告已排队发送到 QQ。", flush=True)
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
        # qq_messages.jsonl can belong to an older analysis route and would
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
        qq_path = session_dir / "handoff" / "qq_messages.jsonl"
        if qq_path.is_file() and not _is_reparse_point(qq_path):
            pieces: list[str] = []
            for line in qq_path.read_text(encoding="utf-8").splitlines():
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
    def _flow_pieces(self, selected: dict[str, Any]) -> list[str]:
        raw = selected.get("pieces")
        if isinstance(raw, list) and raw:
            return [str(piece) for piece in raw if str(piece).strip()]
        summary_path = Path(str(selected.get("summary_path") or "")) if selected else None
        if summary_path is not None and summary_path.is_file() and not _is_reparse_point(summary_path):
            return split_text(report_text(summary_path))
        raise ValueError("报告文本不存在")

    def _save_pending_flow(self, value: dict[str, Any]) -> None:
        value["updated_at"] = _iso()
        admin_id = str(value.get("admin_id") or "")
        _atomic_json(self._admin_flow_path(self._pending_flow_path, admin_id), value)
        legacy = self._load_json_object(self._pending_flow_path)
        if legacy is not None and str(legacy.get("admin_id") or "") == admin_id:
            self._pending_flow_path.unlink(missing_ok=True)

    def _load_pending_flow(self, admin_id: str | None = None) -> dict[str, Any] | None:
        candidates: list[Path] = []
        if admin_id:
            candidates.extend((
                self._admin_flow_path(self._pending_flow_path, admin_id), self._pending_flow_path,
            ))
        else:
            candidates.append(self._pending_flow_path)
            candidates.extend(sorted(self.state_root.glob("pending_flow.*.json")))
        for path in candidates:
            value = self._load_json_object(path)
            if value is None or value.get("schema_version") != PENDING_SCHEMA:
                continue
            if admin_id and str(value.get("admin_id") or "") != admin_id:
                continue
            return value
        return None

    def _clear_pending_flow(self, admin_id: str | None = None) -> None:
        paths = [self._admin_flow_path(self._pending_flow_path, admin_id)] if admin_id else []
        if not admin_id:
            paths.extend(self.state_root.glob("pending_flow.*.json"))
        legacy = self._load_json_object(self._pending_flow_path)
        if not admin_id or (legacy is not None and str(legacy.get("admin_id") or "") == admin_id):
            paths.append(self._pending_flow_path)
        for path in paths:
            if path.is_file() and not _is_reparse_point(path):
                path.unlink()

    def _pending_sessions(self, message: QQInboundMessage, command: str) -> dict[str, Any]:
        self._clear_flow(message.sender_id)
        pending = find_pending_sessions(self.output_root)
        if not pending:
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text="没有未分析的录制会话。",
            )
        lines = [f"{index}. Session={item['session_id']}" for index, item in enumerate(pending, start=1)]
        self._save_pending_flow({
            "schema_version": PENDING_SCHEMA,
            "admin_id": message.sender_id,
            "stage": "awaiting_pending_selection",
            "sessions": [{"session_id": item["session_id"]} for item in pending],
            "selected_index": None,
            "action": None,
        })
        return make_reply(
            message, command=command, status="completed", at=_iso(),
            text="未分析的录制会话：\n" + "\n".join(lines) + "\n\n回复编号选择；选择后回复：分析 / 删除。",
        )

    def _process_pending_flow(self, message: QQInboundMessage, command: str) -> dict[str, Any] | None:
        flow = self._load_pending_flow(message.sender_id)
        if flow is None:
            return None
        text = message.text.strip()
        stage = str(flow.get("stage") or "")
        if stage == "awaiting_pending_selection":
            if text.casefold() in {"取消", "跳过", "cancel"}:
                self._clear_pending_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="已取消。")
            if not text.isdigit():
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text="请回复会话编号，或回复 取消。",
                )
            index = int(text)
            sessions = flow.get("sessions") or []
            if not 1 <= index <= len(sessions):
                return make_reply(
                    message, command=command, status="rejected", at=_iso(),
                    text=f"编号必须在 1-{len(sessions)} 之间。",
                )
            flow["selected_index"] = index
            flow["stage"] = "awaiting_pending_action"
            self._save_pending_flow(flow)
            session_id = str(sessions[index - 1]["session_id"])
            return make_reply(
                message, command=command, status="completed", at=_iso(),
                text=f"已选择 Session={session_id}；回复：分析 / 删除 / 取消。",
            )
        if stage == "awaiting_pending_action":
            action = text.casefold()
            if action in {"取消", "cancel", "退出"}:
                self._clear_pending_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="已取消。")
            index = int(flow.get("selected_index") or 1)
            sessions = flow.get("sessions") or []
            selected = sessions[index - 1] if 1 <= index <= len(sessions) else None
            session_id = str(selected.get("session_id") or "") if selected else ""
            if not session_id:
                self._clear_pending_flow(message.sender_id)
                return make_reply(message, command=command, status="rejected", at=_iso(), text="会话信息失效，已取消。")
            session_dir = (self.output_root / session_id).resolve()
            if session_dir.parent != self.output_root.resolve() or not session_dir.is_dir():
                self._clear_pending_flow(message.sender_id)
                return make_reply(message, command=command, status="rejected", at=_iso(), text="会话目录不存在，已取消。")
            if action in {"分析", "analyze"}:
                if not self._start_analysis_and_deliver(session_dir, message.sender_id):
                    self._clear_pending_flow(message.sender_id)
                    return make_reply(
                        message, command=command, status="rejected", at=_iso(),
                        text=f"Session={session_id} 已在分析中，请用 /oopz 状态 查看进度。",
                    )
                self._clear_pending_flow(message.sender_id)
                decision = self._load_decision()
                if decision is not None and str(decision.get("session_id") or "") == session_id:
                    self._clear_decision()
                return make_reply(
                    message, command=command, status="accepted", at=_iso(),
                    text=f"已开始分析 Session={session_id}（{_configured_analysis_label()}）。完成后会发送报告。",
                )
            if action in {"删除", "delete"}:
                flow["stage"] = "awaiting_delete_confirm"
                flow["session_id"] = session_id
                self._save_pending_flow(flow)
                return make_reply(
                    message, command=command, status="completed", at=_iso(),
                    text=f"确认删除整个 Session={session_id}？回复：确认删除 / 取消。",
                )
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="请回复：分析 / 删除 / 取消。",
            )
        if stage == "awaiting_delete_confirm":
            decision = text.casefold()
            if decision in {"取消", "cancel", "不"}:
                self._clear_pending_flow(message.sender_id)
                return make_reply(message, command=command, status="completed", at=_iso(), text="已取消删除。")
            if decision in {"确认删除", "确认", "yes", "y", "是"}:
                session_id = str(flow.get("session_id") or "")
                try:
                    self._delete_session(session_id)
                except ValueError as error:
                    self._clear_pending_flow(message.sender_id)
                    return make_reply(message, command=command, status="rejected", at=_iso(), text=str(error))
                self._clear_pending_flow(message.sender_id)
                return make_reply(
                    message, command=command, status="completed", at=_iso(),
                    text=f"已删除 Session={session_id}。",
                )
            return make_reply(
                message, command=command, status="rejected", at=_iso(),
                text="请回复：确认删除 / 取消。",
            )
        return None

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
    async def wait_until_idle(self, timeout: float = 30.0) -> None:
        task = self._active_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def serve_directory(self, *, poll_seconds: float = 0.25) -> None:
        if not 0.05 <= poll_seconds <= 5:
            raise ValueError("poll_seconds must be 0.05 to 5")
        inbox = self.state_root / "inbox"
        archive = self.state_root / "inbox_processed"
        failures = self.state_root / "inbox_failed"
        inbox.mkdir(parents=True, exist_ok=True)
        archive.mkdir(parents=True, exist_ok=True)
        failures.mkdir(parents=True, exist_ok=True)
        next_cleanup = 0.0
        next_flow_expiry = 0.0
        while True:
            loop_time = asyncio.get_running_loop().time()
            if loop_time >= next_cleanup:
                cleanup_outbox(self.state_root)
                next_cleanup = loop_time + 3600.0
            if loop_time >= next_flow_expiry:
                self._expire_report_flows()
                next_flow_expiry = loop_time + 5.0
            for path in sorted(inbox.glob("*.json")):
                if not path.is_file() or _is_reparse_point(path):
                    continue
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    message = QQInboundMessage.from_dict(raw)
                    await self.handle(raw)
                    destination = archive / f"{message.message_id}.json"
                except Exception as error:
                    _atomic_json(failures / f"{path.stem}.error.json", {
                        "schema_version": "oopz.qq.inbox_error.v1",
                        "source_file": path.name,
                        "failed_at": _iso(),
                        "error_type": type(error).__name__,
                        "error": str(error)[:1000],
                    })
                    destination = failures / path.name
                if destination.exists():
                    path.unlink()
                else:
                    path.replace(destination)
            await asyncio.sleep(poll_seconds)
