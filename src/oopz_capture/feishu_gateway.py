"""Feishu M1/M2 adapter using the official ``lark-oapi`` Channel SDK."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .feishu_protocol import FeishuInbound, display_intent, normalize_intent, synthetic_controller_id
from .feishu_publisher import FeishuPublisher, PublicationConfig, public_report_fingerprint, recording_title
from .jsonio import atomic_json as _atomic_json, iso_utc as _iso, read_json_or_none as _read_json_or_none
from .controller import ControllerConfig, ControllerService, _env_bool
from .controller_protocol import SenderPolicy
from .reports import find_pending_sessions, find_recent_reports
from .settings import canonical_setting_key, setting_description, setting_is_configured, setting_status
from .send_request import acknowledge_send_request, list_send_requests, reschedule_send_request, send_request_is_due


FEISHU_HELP_TEXT = "\n".join([
    "飞书群共用指令（仅本群成员 @OOPZ 后生效）",
    "",
    "【录音与分析】",
    "• 帮助：显示本说明。",
    "• 开始录音 [时长]：时长可省略，例如“开始录音”“开始录音 1小时”“开始录音 45分钟”；随后点击卡片选择 OOPZ 域和语音频道。未填写时长时会持续录音，但仍受北京时间强制结束时间和频道无人自动退出保护；也可随时发送“停止”。",
    "• 状态：查看当前录音或最近一次分析状态。",
    "• 停止：安全结束当前录音；完成转写后，点击卡片选择“开始分析”或“暂不分析”。",
    "• 待分析：列出尚未完成分析的录音；选择后再次开始分析。",
    "",
    "【报告与会话】",
    "• 最近报告：列出最近 7 份报告；选择后将对应 PDF 上传到本群。",
    "• 详细报告：列出最近 7 份报告；选择后将内部完整 .md 上传到本群。",
    "• 删除会话 [Session ID]：列出可删除会话，或指定 Session ID；必须再次点击确认，才会删除本地会话、公开文档及其公开索引记录。",
    "",
    "【运行设置】",
    "• 设置状态：显示可由本群调整的当前运行参数（敏感值会打码）。",
    "• 设置 变量=值：例如“设置 OOPZ_LANGUAGE=zh”“设置 分片时长=300”。",
    "",
    "──────────",
    "说明：频道、分析、报告、发布和撤回卡片均对本群所有成员开放；同一事件不会重复执行。公开报告经“批准发布”后进入公开日历，日历记录内的“打开公开报告”链接用于阅读。为避免在群消息中泄露凭据，密码、手机号和 API Key 只能在本机配置，不接受群内设置。",
])


# Keep the operational settings that make sense for a Feishu-only group.
# Secret values are intentionally local-only.
FEISHU_SETTING_KEYS = frozenset({
    "OOPZ_CUTOFF_LOCAL_HOUR", "OOPZ_EMPTY_CHANNEL_TIMEOUT_SECONDS", "OOPZ_CHUNK_SECONDS",
    "OOPZ_TRANSCRIPTION_REPAIR_ATTEMPTS", "OOPZ_LANGUAGE", "OOPZ_RETAIN_AUDIO",
    "OOPZ_RETENTION_HOURS", "OOPZ_DEVICE", "OOPZ_ANALYSIS_MAX_PARALLELISM",
    "OOPZ_PROCESSING_DEADLINE_SECONDS", "OOPZ_POLL_INTERVAL_SECONDS",
    "OOPZ_MEMBERSHIP_REFRESH_SECONDS", "OOPZ_MEMBERSHIP_TIMEOUT_SECONDS",
    "OOPZ_CONNECTION_CHECK_SECONDS", "OOPZ_DISCONNECT_GRACE_SECONDS",
    "OOPZ_BROWSER_OPERATION_TIMEOUT_SECONDS", "OOPZ_RECONNECT_WINDOW_SECONDS",
    "OOPZ_RECONNECT_INITIAL_DELAY_SECONDS", "OOPZ_RECONNECT_MAX_DELAY_SECONDS",
    "OOPZ_RECONNECT_ATTEMPT_TIMEOUT_SECONDS",
    "ANALYZER_PROVIDER", "ANALYZER_MODEL", "ANALYZER_TIMEOUT_SECONDS",
    "ANALYZER_MAX_RETRIES", "ANALYZER_MIN_INTERVAL_SECONDS", "ANALYZER_MAX_TOKENS",
    "ANALYZER_THINKING_MAX_TOKENS", "ANALYZER_THINKING_MODE", "ANALYZER_JSON_MODE",
})

# These settings affect credentials, trust boundaries, storage paths, gateway
# identity, or low-level connectivity.  The status command may name them so an
# operator knows where to look, but it must never read or echo their values.
LOCAL_ONLY_SETTING_INFO: dict[str, tuple[str, str]] = {
    "ANALYZER_API_KEY": (
        "分析 API 凭据",
        "未设置，分析不可用",
    ),
    "ANALYZER_BASE_URL": (
        "分析 API 地址",
        "未设置，分析不可用",
    ),
    "OOPZ_FEISHU_ADMIN_CHAT_ID": (
        "唯一接受控制指令的飞书群 ID",
        "未设置，启动后等待首次群邀请并自动绑定",
    ),
    "OOPZ_FEISHU_APP_ID": ("飞书机器人应用 ID", "未设置，网关无法启动"),
    "OOPZ_FEISHU_APP_SECRET": ("飞书机器人应用密钥", "未设置，网关无法启动"),
    "OOPZ_FEISHU_BASE_APP_TOKEN": (
        "公开索引多维表格 App Token",
        "未设置，公开发布和删除不可用",
    ),
    "OOPZ_FEISHU_BASE_TABLE_ID": (
        "公开索引数据表 ID",
        "未设置，公开发布和删除不可用",
    ),
    "OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN": (
        "公开报告文档目录 Token",
        "未设置，公开发布和删除不可用",
    ),
    "OOPZ_FEISHU_PUBLIC_INDEX_URL": (
        "发给读者的公开索引 HTTPS 地址",
        "未设置，公开发布不可用",
    ),
    "OOPZ_FEISHU_STATE_ROOT": (
        "飞书事件去重、发布决策和控制状态目录",
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


def adapt_controller_reply_for_feishu(text: str) -> str:
    """Translate internal controller prompts into the Feishu group vocabulary."""
    adapted = text.strip()
    if not adapted:
        return "已处理。"

    exact = {
        "不支持的指令；发送 /oopz 帮助 查看可用指令。": "该指令尚未接入飞书。请在本群 @OOPZ 后发送“帮助”查看可用指令。",
        "已跳过分析。可用 /oopz待分析 查看未分析会话。": "已跳过分析。需要再次处理时，请在本群 @OOPZ 后发送“待分析”。",
        "请回复：是 / 否。": "请点击分析卡片中的“开始分析”或“暂不分析”。",
    }
    if adapted in exact:
        return exact[adapted]

    # This is the normal start-recording acknowledgement.  Replace the whole
    # trailing legacy instruction rather than merely removing its command.
    adapted = re.sub(
        r"发送\s*/oopz\s*(?:离开|leave|stop)\s*可提前结束录音。",
        "如需提前结束，请在本群 @OOPZ 后发送“停止”。",
        adapted,
        flags=re.IGNORECASE,
    )
    replacements = (
        (r"(?:请)?回复\s*[：:]\s*是\s*/\s*否[。.]?", "请点击下方按钮选择“开始分析”或“暂不分析”。"),
        (r"格式：\s*/oopz\s*设置\s*变量名=值；可用变量见\s*/oopz\s*设置状态。", "格式：发送“设置 变量名=值”；可用变量请发送“设置状态”查看。"),
        (r"如需结束当前录音，请发送\s*/oopz\s*(?:离开|leave|stop)。", "如需结束当前录音，请在本群 @OOPZ 后发送“停止”。"),
        (r"请用\s*/oopz\s*状态\s*查看进度。", "请在本群 @OOPZ 后发送“状态”查看进度。"),
        (r"发送\s*/oopz\s*状态\s*可查看详情。", "请在本群 @OOPZ 后发送“状态”查看详情。"),
        (r"请重新发送\s*/oopz\s*开始。", "请在本群 @OOPZ 后发送“开始录音”。"),
        (r"发送\s*/oopz\s*帮助\s*查看可用指令。", "请在本群 @OOPZ 后发送“帮助”查看可用指令。"),
        (r"可稍后使用\s*/oopz\s*待分析\s*重试。", "请在本群 @OOPZ 后发送“待分析”重试。"),
        (r"/oopz\s*待分析", "“待分析”"),
        (r"/oopz\s*离开", "“停止”"),
        (r"/oopz\s*状态", "“状态”"),
        (r"/oopz\s*开始", "“开始录音”"),
        (r"/oopz\s*帮助", "“帮助”"),
    )
    for pattern, replacement in replacements:
        adapted = re.sub(pattern, replacement, adapted, flags=re.IGNORECASE)

    # A configured Feishu group has no privileged controller role.  These
    # phrases occur in controller progress messages and should state the
    # actual group-wide access model.
    adapted = adapted.replace("另一位管理员", "另一位群成员")
    adapted = adapted.replace("等待管理员确认", "等待本群成员确认")
    adapted = adapted.replace("已保存到 .env", "已保存为本机运行配置")
    return adapted


class FeishuChannel(Protocol):
    async def send(self, to: str, message: Any, opts: Any = None) -> Any: ...


@dataclass(frozen=True)
class FeishuGatewayConfig:
    app_id: str
    app_secret: str
    admin_chat_id: str
    state_root: Path
    controller_config: ControllerConfig
    publication: PublicationConfig | None = None

    @classmethod
    def from_env(cls) -> "FeishuGatewayConfig":
        from .deepseek_client import DeepSeekConfig

        app_id = os.environ.get("OOPZ_FEISHU_APP_ID", "").strip()
        app_secret = os.environ.get("OOPZ_FEISHU_APP_SECRET", "").strip()
        chat_id = os.environ.get("OOPZ_FEISHU_ADMIN_CHAT_ID", "").strip()
        if not app_id or not app_secret or not chat_id:
            raise ValueError("OOPZ_FEISHU_APP_ID, OOPZ_FEISHU_APP_SECRET and OOPZ_FEISHU_ADMIN_CHAT_ID are required")
        # Production startup must fail early when the analysis API contract is
        # incomplete. No provider, endpoint, model, or tuning value is implied.
        DeepSeekConfig.from_env()
        state_root = Path(os.environ.get("OOPZ_FEISHU_STATE_ROOT", "feishu_state"))
        # Keep recording settings in their existing OOPZ_* variables.
        controller = ControllerConfig(
            output_root=Path(os.environ.get("OOPZ_OUTPUT_ROOT", "output")), state_root=state_root,
            authorization=SenderPolicy(frozenset({_CONTROLLER_PLACEHOLDER_ID})),
            consent_confirmed=True,
            chunk_seconds=int(os.environ.get("OOPZ_CHUNK_SECONDS", "300")),
            cutoff_local_hour=int(os.environ.get("OOPZ_CUTOFF_LOCAL_HOUR", "4")),
            language=os.environ.get("OOPZ_LANGUAGE", "auto").strip(),
            retain_audio=_env_bool("OOPZ_RETAIN_AUDIO"),
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
            device=os.environ.get("OOPZ_DEVICE", "cpu").strip(),
        )
        controller.validate()
        values = {
            "folder_token": os.environ.get("OOPZ_FEISHU_PUBLIC_FOLDER_TOKEN", "").strip(),
            "base_app_token": os.environ.get("OOPZ_FEISHU_BASE_APP_TOKEN", "").strip(),
            "base_table_id": os.environ.get("OOPZ_FEISHU_BASE_TABLE_ID", "").strip(),
            "public_index_url": os.environ.get("OOPZ_FEISHU_PUBLIC_INDEX_URL", "").strip(),
        }
        publication = PublicationConfig(**values) if any(values.values()) else None
        if publication is not None:
            publication.validate()
        return cls(app_id, app_secret, chat_id, state_root, controller, publication)


class FeishuGateway:
    """Routes configured-group Feishu messages into the existing controller core.

    The controller uses the Feishu state directory. Outbound messages are
    always sent to the configured Feishu group.
    """

    def __init__(self, config: FeishuGatewayConfig, channel: FeishuChannel, *, controller: ControllerService | None = None, publisher: FeishuPublisher | None = None):
        self.config = config
        self.channel = channel
        self.state_root = config.state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.controller = controller or ControllerService(config.controller_config)
        self.publisher = publisher
        self._lock = asyncio.Lock()

    async def _approver_display_name(self, open_id: str) -> str | None:
        """Resolve a name only for the configured group; an ID fallback is safe."""
        if self.publisher is None:
            return None
        resolver = getattr(self.publisher.client, "get_chat_member_name", None)
        if not callable(resolver):
            return None
        try:
            return await resolver(chat_id=self.config.admin_chat_id, open_id=open_id)
        except Exception as error:
            self._audit("approver_name_lookup_failed", open_id=open_id, error_type=type(error).__name__)
            return None

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
        try:
            title, _ = recording_title(session_id)
            return title
        except ValueError:
            return f"Session={session_id}"

    def _publication_for_session(self, session_id: str) -> dict[str, Any] | None:
        """Load the durable publication record used by delete cards."""
        path = self.state_root / "publication_decisions" / f"{session_id}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"publication_record_invalid": True}
        return value if isinstance(value, dict) else {"publication_record_invalid": True}

    def _delete_session_label(self, session_id: str) -> str:
        publication = self._publication_for_session(session_id)
        if publication and publication.get("publication_record_invalid"):
            status = "发布记录异常"
        elif publication and publication.get("publication_created") and not publication.get("remote_deleted_at"):
            status = "已发布"
        else:
            status = "仅本地"
        return f"{self._session_label(session_id)}｜{session_id}｜{status}"

    @staticmethod
    def _remote_delete_failure_text(error: Exception, publication: dict[str, Any] | None = None) -> str:
        """Turn actionable Feishu permission failures into a useful reply."""
        detail = str(error)
        if "99991672" in detail and "space:document:delete" in detail:
            return (
                "飞书应用缺少删除云文档权限 `space:document:delete`（错误码 99991672）。"
                "本地会话和公开索引记录均未删除；请在飞书开放平台为应用开通该权限并发布新版本，然后重试。"
            )
        if "99991672" in detail and "base:record:delete" in detail:
            prefix = "公开文档已删除，但" if publication and publication.get("remote_document_deleted_at") else ""
            return (
                f"{prefix}飞书应用缺少删除 Base 记录权限 `base:record:delete`（错误码 99991672）。"
                "公开索引记录和本地会话均未删除；请开通该权限并发布新版本，然后重试。"
            )
        return "删除公开文档或公开索引失败；本地会话未删除，请稍后重试。"

    async def _delete_remote_publication(
        self, publication: dict[str, Any], decision_path: Path,
    ) -> None:
        """Delete remote resources stepwise and persist each completed step."""
        if self.publisher is None:
            raise RuntimeError("publication target is not configured")
        if not publication.get("remote_document_deleted_at"):
            await self.publisher.delete_document(publication)
            publication["remote_document_deleted_at"] = _iso()
            _atomic_json(decision_path, publication)
        if not publication.get("remote_index_deleted_at"):
            await self.publisher.delete_index_record(publication)
            publication["remote_index_deleted_at"] = _iso()
            _atomic_json(decision_path, publication)
        publication["remote_deleted_at"] = _iso()
        _atomic_json(decision_path, publication)

    def _report_selection_card(self, *, kind: str) -> dict[str, Any] | None:
        reports = find_recent_reports(self.config.controller_config.output_root, 7)
        if not reports:
            return None
        is_pdf = kind == "pdf"
        actions = []
        for item in reports:
            session_id = str(item["session_id"])
            actions.append({
                "tag": "button",
                "text": {"tag": "plain_text", "content": self._session_label(session_id)[:80]},
                "type": "primary" if is_pdf else "default",
                "value": {"action_id": f"report:{kind}:{session_id}"},
            })
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "选择公开 PDF 报告" if is_pdf else "选择内部 Markdown 报告"}},
            "elements": [
                {"tag": "markdown", "content": "选择一份报告后，将文件直接上传到本群。"},
                {"tag": "action", "actions": actions},
            ],
        }

    def _pending_selection_card(self) -> dict[str, Any] | None:
        pending = find_pending_sessions(self.config.controller_config.output_root)
        if not pending:
            return None
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "选择待分析录音"}},
            "elements": [
                {"tag": "markdown", "content": "选择后会重新开始分析；若机器人曾异常退出，会从已保存的窗口检查点继续，避免重复 API 请求。分析完成后报告将发送到本群。"},
                {"tag": "action", "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": (
                            self._session_label(str(item["session_id"]))
                            + ("（中断可恢复）" if item.get("interrupted") else "")
                        )[:80]},
                        "type": "primary",
                        "value": {"action_id": f"pending:analyze:{item['session_id']}"},
                    }
                    for item in pending[:7]
                ]},
            ],
        }

    def _delete_selection_card(self) -> dict[str, Any] | None:
        merged: dict[str, float] = {}
        for item in find_recent_reports(self.config.controller_config.output_root, 7):
            merged[str(item["session_id"])] = float(item.get("modified_ts") or 0)
        for item in find_pending_sessions(self.config.controller_config.output_root)[:7]:
            session_id = str(item["session_id"])
            merged[session_id] = max(merged.get(session_id, 0), float(item.get("modified_ts") or 0))
        if not merged:
            return None
        session_ids = sorted(merged, key=merged.__getitem__, reverse=True)[:7]
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "选择要删除的会话"}},
            "elements": [
                {"tag": "markdown", "content": "下一步还会要求确认。已发布会话会同时删除本地文件、公开文档和公开索引记录。"},
                {"tag": "action", "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": self._delete_session_label(session_id)[:80]},
                        "type": "danger",
                        "value": {"action_id": f"delete:request:{session_id}"},
                    }
                    for session_id in session_ids
                ]},
            ],
        }

    def _delete_confirmation_card(self, session_id: str) -> dict[str, Any]:
        publication = self._publication_for_session(session_id)
        is_published = bool(
            publication
            and publication.get("publication_created")
            and not publication.get("remote_deleted_at")
        )
        scope = (
            "本地 Session、飞书公开文档和公开索引记录"
            if is_published
            else "本地 Session；该会话没有待删除的公开文档或公开索引记录"
        )
        return {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "确认删除会话"}},
            "elements": [
                {"tag": "markdown", "content": f"将永久删除 **{self._session_label(session_id)}**。\n\nSession ID：`{session_id}`\n\n删除范围：{scope}。"},
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
        if command == "/oopz 最近报告":
            card = self._report_selection_card(kind="pdf")
            return {"card": card} if card else {"text": "没有找到可发送的 PDF 报告。"}
        if command == "/oopz 详细报告":
            card = self._report_selection_card(kind="md")
            return {"card": card} if card else {"text": "没有找到内部完整 Markdown 报告。"}
        if command == "/oopz 待分析":
            card = self._pending_selection_card()
            return {"card": card} if card else {"text": "没有未分析的录制会话。"}
        if command == "/oopz 删除会话":
            card = self._delete_selection_card()
            return {"card": card} if card else {"text": "没有可删除的会话。"}
        if command.startswith("/oopz 删除会话 "):
            session_id = command.removeprefix("/oopz 删除会话 ").strip()
            try:
                if not self._session_dir(session_id).is_dir():
                    return {"text": "会话目录不存在，未显示删除确认。"}
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
                outbound = {"text": FEISHU_HELP_TEXT}
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
        if action_id in {"analysis_yes", "analysis_no"}:
            await self.handle_message(FeishuInbound(event_id, self.config.admin_chat_id, open_id, "是" if action_id == "analysis_yes" else "否"))
            return
        if action_id.startswith("selection:"):
            number = action_id.removeprefix("selection:")
            if number.isdigit():
                await self.handle_message(FeishuInbound(event_id, self.config.admin_chat_id, open_id, number))
            elif number == "cancel":
                await self.handle_message(FeishuInbound(event_id, self.config.admin_chat_id, open_id, "取消"))
            return
        if action_id.startswith(("report:", "pending:", "delete:")):
            await self._handle_extended_card_action(action_id=action_id, open_id=open_id, event_id=event_id)
            return
        if not action_id.startswith("publication:"):
            return
        parts = action_id.split(":", 2)
        if len(parts) != 3:
            self._audit("invalid_publication_action", action_id=action_id, open_id=open_id)
            return
        _, decision, payload = parts
        await self._handle_publication_card_action(
            decision=decision, payload=payload, open_id=open_id, event_id=event_id,
        )

    async def _handle_publication_card_action(
        self, *, decision: str, payload: str, open_id: str, event_id: str,
    ) -> None:
        session_id, separator, expected_fingerprint = payload.rpartition("|")
        if not separator:
            session_id, expected_fingerprint = payload, ""
        if decision not in {"approve", "reject", "withdraw"} or not _SESSION_ID.fullmatch(session_id):
            return
        path = self.state_root / "publication_decisions" / f"{session_id}.json"
        event_path = self._event_path(event_id)
        send_text: str
        async with self._lock:
            if event_path.exists():
                self._audit("duplicate_card_action", action_id=f"publication:{decision}", event_id=event_id)
                return
            existing = _read_json_or_none(path)
            if isinstance(existing, dict) and existing.get("decision") == "withdraw" and not existing.get("publication_created"):
                # A pre-publication withdraw never blocked anything worth keeping;
                # retire the legacy record so a later approval can proceed.
                existing = None
            if isinstance(existing, dict):
                if decision == "withdraw" and existing.get("publication_created") and not existing.get("revoked_at"):
                    if self.publisher is None:
                        await self._send_text("未配置 M3 发布目标，无法撤回已发布报告。")
                        return
                    await self.publisher.revoke(existing)
                    existing.update({"revoked_at": _iso(), "revoked_by_open_id": open_id})
                    _atomic_json(path, existing)
                    self._audit("publication_withdrawn", session_id=session_id, withdrawn_by_open_id=open_id)
                    send_text = "已撤回公开文档，并从公开日历中隐藏该报告。"
                else:
                    send_text = f"Session={session_id} 的发布审查已记录，未重复执行。"
            elif decision == "withdraw":
                # Nothing was published, so there is nothing to withdraw; record
                # nothing, otherwise the record would block a later approval.
                self._audit("publication_withdraw_before_publish_ignored", session_id=session_id, open_id=open_id)
                send_text = "该报告尚未发布公开文档，无需撤回；如要放弃发布，请点击“不发布”。"
            else:
                record = {"schema_version": "oopz.feishu.publication_decision.v1", "session_id": session_id, "decision": decision, "approved_by_open_id": open_id, "decided_at": _iso(), "publication_created": False}
                if decision == "approve":
                    if self.publisher is None:
                        _atomic_json(path, record)
                        self._audit("publication_decision", **record)
                        send_text = "未配置 M3 发布目标，已拒绝执行公开发布。请配置公开文档文件夹、Base 和固定索引链接。"
                    else:
                        approved_by_name = await self._approver_display_name(open_id)
                        result = await self.publisher.publish(
                            session_id=session_id, approved_by_open_id=open_id,
                            approved_by_name=approved_by_name, expected_fingerprint=expected_fingerprint or None,
                        )
                        if approved_by_name:
                            record["approved_by_name"] = approved_by_name
                        record.update({"publication_created": True, **result})
                        _atomic_json(path, record)
                        self._audit("publication_decision", **record)
                        send_text = f"已发布到固定公开索引：{record['public_index_url']}"
                else:
                    _atomic_json(path, record)
                    self._audit("publication_decision", **record)
                    send_text = "已记录：不发布该候选报告。"
            _atomic_json(event_path, {
                "feishu_card_event_id": event_id,
                "action_id": f"publication:{decision}",
                "sender_open_id": open_id,
                "session_id": session_id,
                "received_at": _iso(),
            })
        try:
            await self._send_text(send_text)
        except Exception:
            event_path.unlink(missing_ok=True)
            raise

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
            elif outbound.get("file_path"):
                file_path = Path(str(outbound["file_path"]))
                await self.channel.send(self.config.admin_chat_id, {"file": {"source": str(file_path), "file_name": file_path.name}})
                await self._send_text(str(outbound.get("text") or "文件已上传到本群。"))
            else:
                await self._send_text(str(outbound.get("text") or "已处理。"))
        except Exception:
            # See handle_message: allow redelivery of a click whose reply was lost.
            path.unlink(missing_ok=True)
            raise

    async def _extended_card_outbound(self, *, family: str, action: str, session_id: str, session_dir: Path, open_id: str) -> dict[str, Any]:
        if family == "report":
            if not session_dir.is_dir():
                return {"text": "会话目录不存在，无法发送报告。"}
            if action == "pdf":
                try:
                    _, pdf_path, _ = self.controller._delivery_for_session(session_id)
                except ValueError as error:
                    return {"text": str(error)}
                if not pdf_path:
                    return {"text": "这份报告没有可用的 PDF 文件。"}
                return {"file_path": pdf_path, "text": "公开 PDF 报告已上传到本群。"}
            if action == "md":
                full_path = self.controller._internal_report_path(session_dir)
                if full_path is None:
                    return {"text": "这份会话没有内部完整 .md 报告。"}
                return {"file_path": str(full_path), "text": "内部完整 Markdown 报告已上传到本群。"}
            return {"text": "未知的报告操作。"}

        if family == "pending" and action == "analyze":
            if not session_dir.is_dir():
                return {"text": "会话目录不存在，无法重新分析。"}
            surrogate = self._allow_group_member_in_controller(open_id)
            if not self.controller._start_analysis_and_deliver(session_dir, surrogate):
                return {"text": f"Session={session_id} 已在分析中，请发送“状态”查看进度。"}
            decision = self.controller._load_decision()
            if decision is not None and str(decision.get("session_id") or "") == session_id:
                self.controller._clear_decision()
            return {"text": f"已开始分析 {self._session_label(session_id)}。完成后报告会发送到本群。"}

        if family == "delete":
            if action == "request":
                if not session_dir.is_dir():
                    return {"text": "会话目录不存在，未执行删除。"}
                return {"card": self._delete_confirmation_card(session_id)}
            if action == "cancel":
                return {"text": "已取消删除。"}
            if action != "confirm":
                return {"text": "未知的删除操作。"}
            if not session_dir.is_dir():
                return {"text": "会话目录不存在，未执行删除。"}
            decision_path = self.state_root / "publication_decisions" / f"{session_id}.json"
            publication = self._publication_for_session(session_id)
            if publication and publication.get("publication_record_invalid"):
                return {"text": "公开报告记录无法读取，为避免留下公开文件，未删除本地会话。"}
            remote_deleted = False
            if publication and publication.get("publication_created") and not publication.get("remote_deleted_at"):
                if self.publisher is None:
                    return {"text": "该会话已有公开报告，但当前未配置发布目标；为避免留下公开文件，未删除本地会话。"}
                try:
                    await self._delete_remote_publication(publication, decision_path)
                except Exception as error:
                    self._audit("session_delete_remote_failed", session_id=session_id, error=f"{type(error).__name__}: {error}")
                    return {"text": self._remote_delete_failure_text(error, publication)}
                publication["remote_deleted_by_open_id"] = open_id
                _atomic_json(decision_path, publication)
                remote_deleted = True
            try:
                self.controller._delete_session(session_id)
            except ValueError as error:
                return {"text": f"公开内容已删除，但本地会话删除失败：{error}"}
            if publication is not None:
                publication["deleted_at"] = _iso()
                publication["deleted_by_open_id"] = open_id
                _atomic_json(decision_path, publication)
            self._audit("session_deleted", session_id=session_id, deleted_by_open_id=open_id)
            if remote_deleted:
                return {"text": f"已删除 {self._session_label(session_id)}（Session ID={session_id}）的本地会话、公开文档和公开索引记录。"}
            return {"text": f"已删除 {self._session_label(session_id)}（Session ID={session_id}）的本地会话。该会话没有公开文档或公开索引记录。"}

        return {"text": "未知的卡片操作。"}

    async def drain_outbox(self) -> int:
        sent = 0
        for item in list_send_requests(self.state_root, statuses={"pending"}):
            if not send_request_is_due(item):
                continue
            try:
                source = str(item.get("source") or "")
                if source == "analysis_decision":
                    await self._send_card(self._analysis_card(str(item.get("text") or "")))
                elif source == "publication_review:prompt":
                    await self._send_card(self._publication_card())
                elif item.get("file_path"):
                    path = Path(str(item["file_path"]))
                    self._console("发件", f"文件={path.name}")
                    await self.channel.send(self.config.admin_chat_id, {"file": {"source": str(path), "file_name": path.name}})
                else:
                    await self._send_text(adapt_controller_reply_for_feishu(str(item.get("text") or "")))
                acknowledge_send_request(self.state_root, str(item["send_request_id"]), status="sent")
                sent += 1
            except Exception as error:
                reschedule_send_request(self.state_root, str(item["send_request_id"]), error=f"Feishu: {type(error).__name__}: {error}")
                self._audit("outbound_retry", send_request_id=str(item["send_request_id"]), error=f"{type(error).__name__}: {error}")
        return sent

    async def reconcile_publications(self) -> int:
        """Remove remote reports whose local Session is already absent."""
        if self.publisher is None:
            return 0
        changed = 0
        root = self.state_root / "publication_decisions"
        for path in root.glob("*.json") if root.is_dir() else ():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                session_id = str(value.get("session_id") or "")
                if not value.get("publication_created") or value.get("remote_deleted_at") or (self.config.controller_config.output_root / session_id).is_dir():
                    continue
                await self.publisher.delete(value)
                value["remote_deleted_at"] = _iso()
                value["deleted_by_retention_at"] = value["remote_deleted_at"]
                _atomic_json(path, value)
                self._audit("retention_remote_deleted", session_id=session_id, document_id=value.get("document_id"))
                changed += 1
            except Exception as error:
                self._audit("retention_withdrawal_failed", record=str(path), error=f"{type(error).__name__}: {error}")
        return changed

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
        """Delete an expired local Session only after its remote report is deleted."""
        removed = 0
        async with self._lock:
            for session_id in self._expired_session_ids(self.config.controller_config.output_root):
                decision_path = self.state_root / "publication_decisions" / f"{session_id}.json"
                publication: dict[str, Any] | None = None
                if decision_path.is_file():
                    try:
                        candidate = json.loads(decision_path.read_text(encoding="utf-8"))
                        publication = candidate if isinstance(candidate, dict) else None
                    except (OSError, ValueError, TypeError):
                        self._audit("retention_skipped_invalid_publication", session_id=session_id)
                        continue
                if publication and publication.get("publication_created") and not publication.get("remote_deleted_at"):
                    if self.publisher is None:
                        self._audit("retention_skipped_no_publisher", session_id=session_id)
                        continue
                    try:
                        await self._delete_remote_publication(publication, decision_path)
                    except Exception as error:
                        self._audit("retention_remote_delete_failed", session_id=session_id, error=f"{type(error).__name__}: {error}")
                        continue
                    publication["deleted_by_retention_at"] = publication["remote_deleted_at"]
                    _atomic_json(decision_path, publication)
                try:
                    self.controller._delete_session(session_id)
                except Exception as error:
                    # A locked file (viewer/antivirus) must not kill the gateway
                    # loop; the session stays and is retried next minute.
                    self._audit("retention_local_delete_failed", session_id=session_id, error=f"{type(error).__name__}: {error}")
                    continue
                if publication is not None:
                    publication["deleted_at"] = _iso()
                    _atomic_json(decision_path, publication)
                self._audit("retention_session_deleted", session_id=session_id)
                removed += 1
            removed += self._purge_stale_state_files()
        return removed

    def _backfill_approver_open_id(self) -> str:
        root = self.state_root / "publication_decisions"
        candidates: list[tuple[str, str]] = []
        for path in root.glob("*.json") if root.is_dir() else ():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                open_id = str(value.get("approved_by_open_id") or "").strip()
                decided_at = str(value.get("decided_at") or "")
                if open_id:
                    candidates.append((decided_at, open_id))
            except (OSError, ValueError, TypeError):
                continue
        if not candidates:
            raise ValueError("没有可用于历史补传的飞书审批人 ID；请先在本群批准一份报告。")
        return max(candidates)[1]

    async def backfill_publications(self) -> int:
        """Publish every current, non-future local public report exactly once."""
        if self.publisher is None:
            raise ValueError("未配置公开文档文件夹、Base 或固定索引链接")
        approver_open_id = self._backfill_approver_open_id()
        created = 0
        # Session timestamps are named in Beijing time, not the host locale.
        now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
        async with self._lock:
            for item in find_recent_reports(self.config.controller_config.output_root, 1000):
                session_id = str(item["session_id"])
                try:
                    _, recorded_at = recording_title(session_id)
                except ValueError:
                    continue
                if recorded_at > now:
                    continue
                path = self.state_root / "publication_decisions" / f"{session_id}.json"
                existing: dict[str, Any] | None = None
                if path.is_file():
                    try:
                        candidate = json.loads(path.read_text(encoding="utf-8"))
                        existing = candidate if isinstance(candidate, dict) else None
                    except (OSError, ValueError, TypeError):
                        existing = None
                if existing and existing.get("publication_created") and not existing.get("remote_deleted_at"):
                    approved_by_name = await self._approver_display_name(str(existing.get("approved_by_open_id") or ""))
                    if approved_by_name:
                        existing["approved_by_name"] = approved_by_name
                        _atomic_json(path, existing)
                    await self.publisher.repair_index_record(existing, approved_by_name=approved_by_name)
                    continue
                approved_by_name = await self._approver_display_name(approver_open_id)
                result = await self.publisher.publish(
                    session_id=session_id, approved_by_open_id=approver_open_id,
                    approved_by_name=approved_by_name,
                )
                record = {
                    "schema_version": "oopz.feishu.publication_decision.v1",
                    "session_id": session_id,
                    "decision": "backfill",
                    "approved_by_open_id": approver_open_id,
                    "approved_by_name": approved_by_name,
                    "decided_at": _iso(),
                    "publication_created": True,
                    **result,
                }
                _atomic_json(path, record)
                self._audit("publication_backfilled", session_id=session_id, document_id=result.get("document_id"))
                created += 1
        return created

    async def repair_publication_index(self) -> int:
        """Update active legacy Base entries to the tenant-hosted report URL."""
        if self.publisher is None:
            return 0
        changed = 0
        root = self.state_root / "publication_decisions"
        for path in root.glob("*.json") if root.is_dir() else ():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not value.get("publication_created"):
                    continue
                if value.get("revoked_at") or value.get("deleted_at"):
                    continue
                if not _SESSION_ID.fullmatch(str(value.get("session_id") or "")):
                    continue
                await self.publisher.repair_index_record(value)
                value["document_url"] = self.publisher.document_url(str(value["document_id"]))
                value["index_repaired_at"] = _iso()
                _atomic_json(path, value)
                self._audit("publication_index_repaired", session_id=value["session_id"], document_id=value.get("document_id"))
                changed += 1
            except Exception as error:
                self._audit("publication_index_repair_failed", record=str(path), error=f"{type(error).__name__}: {error}")
        return changed

    async def _send_text(self, text: str) -> None:
        self._console("发信", text[:500])
        result = await self.channel.send(self.config.admin_chat_id, {"text": text})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu send failed"))

    async def _send_reply(self, text: str) -> None:
        text = adapt_controller_reply_for_feishu(text)
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
            if line.strip().startswith("回复编号"):
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

    async def _send_card(self, card: dict[str, Any]) -> None:
        header = (((card.get("header") or {}).get("title") or {}).get("content") or "操作卡片")
        self._console("发卡", str(header))
        result = await self.channel.send(self.config.admin_chat_id, {"card": card})
        if getattr(result, "success", True) is False:
            raise RuntimeError(getattr(result, "error", "Feishu card send failed"))

    @staticmethod
    def _analysis_card(text: str) -> dict[str, Any]:
        body = adapt_controller_reply_for_feishu(text)
        return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": "录音已结束：是否开始分析"}}, "elements": [{"tag": "markdown", "content": body}, {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "开始分析"}, "type": "primary", "value": {"action_id": "analysis_yes"}}, {"tag": "button", "text": {"tag": "plain_text", "content": "暂不分析"}, "value": {"action_id": "analysis_no"}}]}]}

    def _publication_card(self) -> dict[str, Any]:
        state = getattr(self.controller, "_state", {})
        session_id = str((state.get("last_job") or {}).get("session_id") or "unknown")
        try:
            fingerprint = public_report_fingerprint(output_root=self.config.controller_config.output_root, session_id=session_id)
        except (OSError, ValueError):
            fingerprint = ""
        action_suffix = f"{session_id}|{fingerprint}" if fingerprint else session_id
        return {"config": {"wide_screen_mode": True}, "header": {"title": {"tag": "plain_text", "content": "候选公开报告审查"}}, "elements": [{"tag": "markdown", "content": "内部报告和候选公开 PDF 已发送到本群。批准将仅发布此时审查的公开版本。"}, {"tag": "action", "actions": [{"tag": "button", "text": {"tag": "plain_text", "content": "批准发布"}, "type": "primary", "value": {"action_id": f"publication:approve:{action_suffix}"}}, {"tag": "button", "text": {"tag": "plain_text", "content": "不发布"}, "value": {"action_id": f"publication:reject:{action_suffix}"}}, {"tag": "button", "text": {"tag": "plain_text", "content": "撤回"}, "type": "danger", "value": {"action_id": f"publication:withdraw:{action_suffix}"}}]}]}
