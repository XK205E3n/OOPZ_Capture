"""Hands a finished digest image to MaiBot and says what happened (QQ bridge, see docs/QQ_BRIDGE.md)."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable, Mapping

from .relay import BridgeConfig, BridgeConfigError, request_id_for, send_image

# What the endpoint's error codes mean for the person reading the Feishu notice.
REASONS = {
    "target_blocked": "这个群在 MaiBot 的黑名单里",
    "disabled": "MaiBot 的外部发图接口没有启用",
    "unauthorized": "发图口令不对",
    "session_unavailable": "MaiBot 找不到这个群的会话（机器人可能不在群里）",
    "path_not_allowed": "图片不在 MaiBot 允许读取的目录里",
    "request_id_conflict": "同一个发送编号对应了另一张图",
    "rate_limited": "发得太快被限速",
    "busy": "MaiBot 正忙",
    "timeout": "等待 MaiBot 回复超时",
}


def deliver_digest(session_dir: Path, image: Path, *, env: Mapping[str, str] = os.environ,
                   send: Callable = send_image) -> dict | None:
    """None when the bridge is off.  Otherwise the outcome, also written (without the token) to
    ``analysis/qq_relay.json`` next to the digest.  A misconfigured bridge is reported, not raised."""
    session_dir = Path(session_dir)
    try:
        config = BridgeConfig.from_env(env)
    except BridgeConfigError as error:
        return {"status": "misconfigured", "error": str(error)}
    if config is None:
        return None
    request_id = request_id_for(session_dir.name, image)
    result = send(config, Path(image), request_id) | {"target": f"{config.target_type}:{config.target_id}"}
    try:
        (session_dir / "analysis" / "qq_relay.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return result


def failure_notice(result: dict | None) -> str | None:
    """The Feishu text for a bridge outcome that needs a human, or None (off, or sent)."""
    if not result or result.get("status") == "sent":
        return None
    status, error = result.get("status"), str(result.get("error") or "")
    reason = REASONS.get(error) or (f"连不上 MaiBot（{error}）" if error.startswith("connection:") else error)
    if status == "unknown":
        return "QQ 群发图结果不确定：可能已经发出，也可能没发出。请到 QQ 群确认一下，程序不会自动重试。" + (
            f"（{reason}）" if reason else "")
    if status == "partial":
        return "QQ 群里图已经发出，但附带的文字没发出去。不会自动重试。"
    if status == "misconfigured":
        return f"QQ 群发图没有启用：配置有问题（{error}）。飞书这边不受影响。"
    return f"QQ 群发图失败：{reason or '未知原因'}。飞书这边的图已经发出。"
