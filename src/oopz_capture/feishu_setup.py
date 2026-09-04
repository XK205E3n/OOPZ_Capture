"""One-click Feishu bot provisioning through the official app-registration flow.

The flow follows the same device-registration contract used by the official
``@larksuiteoapi/node-sdk`` ``registerApp()`` (and by the reference project
PlutoKeating/dsh-lark-bot), i.e. RFC 8628 style:

1. ``POST https://accounts.feishu.cn/oauth/v1/app/registration`` with
   ``action=begin`` returns a ``verification_uri_complete`` QR link, a
   ``device_code``, a poll ``interval`` and an ``expires_in``.
2. The user scans the QR link with the Feishu mobile app and confirms the
   create-or-update page.  Scopes, long-connection events and card callbacks
   travel in the gzip+base64url ``addons`` query parameter, so the confirm
   page pre-requests everything OOPZ needs in one confirmation.
3. ``action=poll`` with the ``device_code`` returns ``authorization_pending``,
   ``slow_down``, a terminal error, or the app's ``client_id``/``client_secret``.

Only public HTTP endpoints are used; the operator explicitly approves every
change by scanning.  App secrets are written to the local gitignored ``.env``
and are never printed or logged.
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .settings import upsert_env


FEISHU_ACCOUNT_DOMAIN = "accounts.feishu.cn"
LARK_ACCOUNT_DOMAIN = "accounts.larksuite.com"
REGISTRATION_ENDPOINT = "/oauth/v1/app/registration"
# The registration landing page creates this app archetype; OOPZ only needs a
# tenant-scoped bot app, which this flow provides.
APP_ARCHETYPE = "PersonalAgent"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
SLOW_DOWN_EXTRA_SECONDS = 5.0
DEFAULT_EXPIRES_IN_SECONDS = 600.0

OOPZ_BOT_NAME = "OOPZ 管理机器人"
OOPZ_BOT_DESCRIPTION = "OOPZ 语音频道录音、转写、分析与报告发布机器人"

# README_FEISHU_BOT_SETUP.md 第 4 节：11 项应用身份（tenant）权限；不要扩大。
REQUIRED_TENANT_SCOPES: tuple[str, ...] = (
    "im:message.group_at_msg:readonly",
    "im:message:send_as_bot",
    "im:resource",
    "im:chat.members:read",
    "docx:document:create",
    "docx:document:write_only",
    "docs:permission.setting:write_only",
    "space:document:delete",
    "base:record:create",
    "base:record:update",
    "base:record:delete",
)
# README 第 3.1 节：长连接事件（群内指令 + 首次入群自动绑定控制群）。
REQUIRED_TENANT_EVENTS: tuple[str, ...] = (
    "im.message.receive_v1",
    "p2.im.chat.member.bot.added_v1",
)
# README 第 3.2 节：卡片回传交互（不是旧版 card.action.trigger_v1）。
REQUIRED_CALLBACKS: tuple[str, ...] = ("card.action.trigger",)

RegistrationTransport = Callable[[str, dict[str, str]], dict[str, Any]]


class RegistrationError(RuntimeError):
    """A terminal failure of the app-registration flow."""

    def __init__(self, code: str, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


@dataclass(frozen=True)
class RegistrationResult:
    app_id: str
    app_secret: str
    operator_open_id: str | None
    tenant_brand: str


def _addons_payload() -> dict[str, Any]:
    return {
        "scopes": {"tenant": list(REQUIRED_TENANT_SCOPES), "user": []},
        "events": {"items": {"tenant": list(REQUIRED_TENANT_EVENTS), "user": []}},
        "callbacks": {"items": list(REQUIRED_CALLBACKS)},
    }


def encode_addons() -> str:
    """Encode addons exactly as the platform requires: JSON -> gzip -> URL-safe base64."""
    raw = json.dumps(_addons_payload(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.b64encode(gzip.compress(raw)).decode("ascii")
    return encoded.replace("+", "-").replace("/", "_").rstrip("=")


def build_registration_url(
    begin_result: dict[str, Any],
    *,
    app_id: str | None = None,
    create_only: bool = False,
    source: str = "oopz-capture",
) -> str:
    """Append OOPZ's preset and addons to the QR landing link."""
    base = str(begin_result.get("verification_uri_complete") or "").strip()
    if not base:
        raise RegistrationError("invalid_begin", "registration begin returned no verification_uri_complete")
    params: dict[str, str] = {
        "from": "sdk",
        "source": source,
        "tp": "sdk",
        "addons": encode_addons(),
        "name": OOPZ_BOT_NAME,
        "desc": OOPZ_BOT_DESCRIPTION,
    }
    if create_only:
        params["createOnly"] = "true"
    if app_id:
        params["clientID"] = app_id
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urllib.parse.urlencode(params)}"


def urllib_form_transport(domain: str, fields: dict[str, str]) -> dict[str, Any]:
    """POST one form-encoded registration request; RFC 8628 uses HTTP 400 bodies."""
    request = urllib.request.Request(
        f"https://{domain}{REGISTRATION_ENDPOINT}",
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except ValueError as parse_error:
            raise RegistrationError("http_error", f"HTTP {error.code}: {body[:200]}") from parse_error


def begin_registration(transport: RegistrationTransport) -> dict[str, Any]:
    return transport(FEISHU_ACCOUNT_DOMAIN, {
        "action": "begin",
        "archetype": APP_ARCHETYPE,
        "auth_method": "client_secret",
        "request_user_info": "open_id",
    })


def poll_registration(
    transport: RegistrationTransport, domain: str, device_code: str,
) -> dict[str, Any]:
    return transport(domain, {"action": "poll", "device_code": device_code})


def register_app(
    *,
    transport: RegistrationTransport = urllib_form_transport,
    show_qr: Callable[[str], None],
    print_line: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float | None = None,
    app_id: str | None = None,
    create_only: bool = False,
) -> RegistrationResult:
    """Run begin -> QR -> poll until the operator confirms, or raise RegistrationError."""
    begin = begin_registration(transport)
    device_code = str(begin.get("device_code") or "").strip()
    if not device_code:
        raise RegistrationError("invalid_begin", "registration begin returned no device_code")
    raw_interval = begin.get("interval")
    interval = max(0.0, float(raw_interval) if raw_interval is not None else DEFAULT_POLL_INTERVAL_SECONDS)
    raw_expires = begin.get("expires_in")
    expires_in = float(raw_expires) if raw_expires is not None else DEFAULT_EXPIRES_IN_SECONDS
    timeout = float(timeout_seconds) if timeout_seconds is not None else expires_in
    url = build_registration_url(begin, app_id=app_id, create_only=create_only)
    print_line("请使用飞书 App 扫描下方二维码，并在手机上确认创建/更新机器人应用：")
    show_qr(url)
    print_line(f"无法扫码时，请在浏览器打开同一链接并登录确认：{url}")
    print_line(f"二维码有效期约 {max(1, round(expires_in / 60))} 分钟；确认后本命令会自动继续。")

    domain = FEISHU_ACCOUNT_DOMAIN
    domain_switched = False
    slow_down_extra = 0.0
    deadline = clock() + timeout
    while True:
        response = poll_registration(transport, domain, device_code)
        user = response.get("user_info") if isinstance(response.get("user_info"), dict) else {}
        # International (Lark) tenants confirm on a different account domain; the
        # same device_code keeps working there, so switch and keep polling.
        if str(user.get("tenant_brand") or "") == "lark" and not domain_switched:
            domain = LARK_ACCOUNT_DOMAIN
            domain_switched = True
            continue
        if response.get("client_id") and response.get("client_secret"):
            return RegistrationResult(
                app_id=str(response["client_id"]),
                app_secret=str(response["client_secret"]),
                operator_open_id=str(user.get("open_id") or "") or None,
                tenant_brand=str(user.get("tenant_brand") or "feishu"),
            )
        error = str(response.get("error") or "")
        if error == "slow_down":
            slow_down_extra += SLOW_DOWN_EXTRA_SECONDS
        elif error and error != "authorization_pending":
            raise RegistrationError(error, str(response.get("error_description") or "注册流程已终止"))
        # An empty (or unrecognised) poll body keeps polling, bounded by the deadline.
        if clock() >= deadline:
            raise RegistrationError("expired_token", "等待扫码确认超时；请重新运行本命令")
        sleep(interval + slow_down_extra)


def render_terminal_qr(url: str) -> None:
    try:
        import qrcode
    except ModuleNotFoundError:
        print("（未安装 qrcode 包，跳过终端二维码；请直接打开下方链接。）")
        return
    code = qrcode.QRCode(border=1)
    code.add_data(url)
    code.print_ascii(invert=True)


def _existing_env_value(key: str) -> str:
    return str(os.environ.get(key) or "").strip()


def manual_fallback_text() -> str:
    scopes = "\n".join(f"  - {scope}" for scope in REQUIRED_TENANT_SCOPES)
    events = "、".join(REQUIRED_TENANT_EVENTS)
    return (
        "也可以按手册手动配置：参见 README_FEISHU_BOT_SETUP.md。\n"
        f"需要的应用身份权限共 {len(REQUIRED_TENANT_SCOPES)} 项：\n{scopes}\n"
        f"长连接事件：{events}；卡片回调：{REQUIRED_CALLBACKS[0]}。"
    )


def run_setup(
    *,
    app_id: str | None = None,
    create_only: bool = False,
    force: bool = False,
    url_only: bool = False,
    show_qr: Callable[[str], None] | None = None,
    env_path: Path | None = None,
    print_line: Callable[[str], None] = print,
    transport: RegistrationTransport = urllib_form_transport,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout_seconds: float | None = None,
) -> int:
    """CLI entry: one QR confirmation configures credentials, scopes, events and callbacks."""
    target_app_id = (app_id or _existing_env_value("OOPZ_FEISHU_APP_ID")).strip() or None
    if target_app_id:
        print_line(f"将更新已有应用 {target_app_id}（扫码确认页会显示将要新增的权限与事件）。")
    if show_qr is None:
        show_qr = (lambda url: None) if url_only else render_terminal_qr
    try:
        result = register_app(
            transport=transport,
            show_qr=show_qr,
            print_line=print_line,
            sleep=sleep,
            clock=clock,
            timeout_seconds=timeout_seconds,
            app_id=target_app_id,
            create_only=create_only,
        )
    except RegistrationError as error:
        print_line(f"一键配置未完成：{error}")
        print_line(manual_fallback_text())
        return 1
    except OSError as error:
        print_line(f"无法连接飞书注册服务：{type(error).__name__}: {error}")
        print_line("请检查出站网络后重试。")
        print_line(manual_fallback_text())
        return 1

    stored_app_id = _existing_env_value("OOPZ_FEISHU_APP_ID")
    if stored_app_id and stored_app_id != result.app_id and not force:
        print_line(
            f"检测到 .env 已绑定其他应用（{stored_app_id}），本次得到的是 {result.app_id}；"
            "未写入任何配置。确认要切换时，请使用 --force 重新运行。"
        )
        return 1
    upsert_env("OOPZ_FEISHU_APP_ID", result.app_id, env_path=env_path)
    upsert_env("OOPZ_FEISHU_APP_SECRET", result.app_secret, env_path=env_path)
    print_line("一键配置完成：")
    print_line(f"  App ID：{result.app_id}")
    print_line(f"  App Secret：已写入本机 .env（长度 {len(result.app_secret)}，不在终端显示）")
    print_line(f"  应用身份权限：{len(REQUIRED_TENANT_SCOPES)} 项（扫码确认时已一并申请）")
    print_line(f"  长连接事件：{'、'.join(REQUIRED_TENANT_EVENTS)}")
    print_line(f"  卡片回调：{REQUIRED_CALLBACKS[0]}")
    print_line("")
    print_line("后续步骤：")
    print_line("1. 若开放平台提示有待发布版本，请到「版本管理与发布」创建并发布一个版本，机器人才能被搜索和邀请。")
    print_line("2. 运行 启动OOPZ全流程.bat（或 oopz-feishu serve），把机器人邀请进目标群；首个群会自动绑定为控制群。")
    print_line("3. 如需公开发布报告，仍需在 .env 配置四个 OOPZ_FEISHU_PUBLIC_* 变量，并把应用加为对应文件夹和 Base 的协作者（见 README_FEISHU_BOT_SETUP.md 第 6、7 节）。")
    return 0
