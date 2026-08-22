from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .qq_controller import (
    QQControllerConfig,
    QQControllerService,
    _atomic_json,
    acquire_instance_lock,
    release_instance_lock,
)
from .qq_outbox import acknowledge_delivery, list_outbox
from .qq_protocol import QQInboundMessage, parse_command


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-qq-controller",
        description="Authorized QQ command core and final-report Outbox",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate one normalized QQ inbound message")
    validate.add_argument("message", type=Path)
    submit = commands.add_parser("submit", help="submit one normalized message to a running controller")
    submit.add_argument("message", type=Path)
    submit.add_argument("--state-root", type=Path, default=Path("controller_state"))
    serve = commands.add_parser("serve", help="run the directory-based adapter core")
    serve.add_argument("--poll-seconds", type=float, default=0.25)
    state = commands.add_parser("state", help="show controller state")
    state.add_argument("--state-root", type=Path, default=Path("controller_state"))
    outbox = commands.add_parser("outbox", help="list final-report messages awaiting a QQ adapter")
    outbox.add_argument("--state-root", type=Path, default=Path("controller_state"))
    outbox.add_argument("--all", action="store_true")
    ack = commands.add_parser("ack", help="record a QQ adapter delivery result")
    ack.add_argument("--state-root", type=Path, default=Path("controller_state"))
    ack.add_argument("--message-id", required=True)
    ack.add_argument("--status", choices=["sent", "failed"], required=True)
    ack.add_argument("--error")
    return parser


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("message must be a JSON object")
    return value


def _submit(path: Path, state_root: Path) -> Path:
    raw = _read(path)
    message = QQInboundMessage.from_dict(raw)
    parse_command(message.text)
    destination = state_root.resolve() / "inbox" / f"{message.message_id}.json"
    if destination.exists():
        raise ValueError(f"message is already submitted: {message.message_id}")
    _atomic_json(destination, raw)
    return destination


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            message = QQInboundMessage.from_dict(_read(args.message))
            print(json.dumps({
                "status": "valid", "Message ID": message.message_id,
                "command": parse_command(message.text), "sender_id": message.sender_id,
                "chat_type": message.chat_type, "chat_id": message.chat_id,
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "submit":
            path = _submit(args.message, args.state_root)
            print(f"指令已提交：{path}")
            return 0
        if args.command == "serve":
            config = QQControllerConfig.from_env()
            instance_lock = acquire_instance_lock(config.state_root)
            try:
                print(f"QQ 控制核心已启动；状态目录={config.state_root.resolve()}")
                print("当前使用目录适配器，不会直接登录 QQ。")
                asyncio.run(QQControllerService(config).serve_directory(poll_seconds=args.poll_seconds))
            finally:
                release_instance_lock(instance_lock)
            return 0
        if args.command == "state":
            path = args.state_root.resolve() / "controller.json"
            print(path.read_text(encoding="utf-8") if path.is_file() else "{}")
            return 0
        if args.command == "outbox":
            statuses = None if args.all else {"pending", "failed", "blocked"}
            print(json.dumps(list_outbox(args.state_root.resolve(), statuses=statuses), ensure_ascii=False, indent=2))
            return 0
        if args.command == "ack":
            value = acknowledge_delivery(
                args.state_root.resolve(), args.message_id, status=args.status, error=args.error,
            )
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        raise AssertionError(args.command)
    except KeyboardInterrupt:
        print("控制器已停止。", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
