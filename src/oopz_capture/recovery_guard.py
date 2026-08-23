"""Temporary file-backed supervision for a capture whose controller monitor died.

This utility intentionally does not open another Feishu long connection.  It
uses the already-running gateway's durable audit trail only to relay a later
``停止`` command into the continuous worker's normal ``control/stop.json``
protocol.  It also restores progress lines for the existing monitor window.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Sequence

from .continuous import request_stop
from .env_loader import load_project_env
from .send_request import enqueue_send_request


ACTIVE_STATUSES = frozenset({"connecting", "recording", "reconnecting", "stopping"})


def _append_progress(log_path: Path, text: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(f"[录制进度] {text}\n")


def _read_lifecycle(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _progress(session_dir: Path) -> tuple[str, int, int, int, int]:
    lifecycle = _read_lifecycle(session_dir / "lifecycle.json")
    status = str(lifecycle.get("status") or "unknown")
    transcribed = failed = recording = 0
    chunks_root = session_dir / "chunks"
    chunks = [item for item in chunks_root.iterdir() if item.is_dir()] if chunks_root.is_dir() else []
    for chunk in chunks:
        state = _read_lifecycle(chunk / "lifecycle.json")
        chunk_status = str(state.get("status") or "recording")
        if chunk_status == "transcribed":
            transcribed += 1
        elif chunk_status == "failed":
            failed += 1
        elif chunk_status in {"recording", "capturing"}:
            recording += 1
    return status, len(chunks), transcribed, recording, failed


def _new_commands(audit_path: Path, offset: int) -> tuple[int, set[str]]:
    if not audit_path.is_file():
        return offset, set()
    with audit_path.open("r", encoding="utf-8") as stream:
        stream.seek(offset)
        lines = stream.readlines()
        next_offset = stream.tell()
    commands: set[str] = set()
    for line in lines:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get("kind") == "accepted_command":
            command = str(value.get("command") or "")
            if command in {"/oopz 离开", "/oopz 状态"}:
                commands.add(command)
    return next_offset, commands


def _queue_status_correction(state_root: Path, chat_id: str, session_id: str, current: tuple[str, int, int, int, int]) -> None:
    status, total, transcribed, recording, failed = current
    status_text = {
        "connecting": "正在连接频道",
        "recording": "正在录音",
        "reconnecting": "语音连接中断，正在重连",
        "stopping": "正在安全结束并等待转写",
    }.get(status, status)
    enqueue_send_request(
        state_root,
        target_type="group",
        target_id=chat_id,
        source="recovery_guard:authoritative_status",
        text=(f"状态校正：Session ID={session_id}；状态={status_text}；"
              f"分片={transcribed}/{total} 已转写；录音中={recording}；失败={failed}。"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oopz-recovery-guard")
    parser.add_argument("--session", required=True)
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--state-root", type=Path, default=Path("feishu_state"))
    parser.add_argument("--runtime-log", type=Path, default=Path("logs/feishu_runtime.log"))
    args = parser.parse_args(argv)
    load_project_env()

    session_dir = args.output_root.resolve() / args.session
    lifecycle_path = session_dir / "lifecycle.json"
    if not lifecycle_path.is_file():
        raise ValueError(f"Session lifecycle not found: {args.session}")
    audit_path = args.state_root.resolve() / "feishu_audit.jsonl"
    offset = audit_path.stat().st_size if audit_path.is_file() else 0
    chat_id = os.environ.get("OOPZ_FEISHU_ADMIN_CHAT_ID", "").strip()
    last = None
    last_heartbeat = 0.0
    stop_forwarded = False
    _append_progress(args.runtime_log, f"恢复监控：Session ID={args.session}。")

    while True:
        offset, commands = _new_commands(audit_path, offset)
        if "/oopz 离开" in commands and not stop_forwarded:
            request_stop(args.output_root, args.session, reason="operator_stop_command")
            stop_forwarded = True
            _append_progress(args.runtime_log, f"已从飞书“停止”同步安全结束指令；Session ID={args.session}。")
        current = _progress(session_dir)
        if "/oopz 状态" in commands:
            if chat_id:
                _queue_status_correction(args.state_root, chat_id, args.session, current)
            else:
                _append_progress(args.runtime_log, "无法发送状态校正：未配置 OOPZ_FEISHU_ADMIN_CHAT_ID。")
        now = time.monotonic()
        if current != last or now - last_heartbeat >= 60.0:
            status, total, transcribed, recording, failed = current
            _append_progress(
                args.runtime_log,
                f"心跳：状态={status}；分片总数={total}；已转写={transcribed}；"
                f"录音中={recording}；失败={failed}。",
            )
            last, last_heartbeat = current, now
        if current[0] not in ACTIVE_STATUSES:
            return 0
        time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
