from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from .onebot_gateway import (
    ControllerDirectoryBridge,
    DiagnosticEchoBridge,
    OneBotGateway,
    OneBotGatewayConfig,
    diagnose_connection,
    notify_administrators,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-onebot",
        description="Private-admin OneBot 11 gateway for NapCatQQ",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-config", help="validate environment configuration without connecting")
    commands.add_parser("diagnose", help="check NapCat login and configured report group without OOPZ")
    notice = commands.add_parser("notify-admin", help="send an acknowledged private lifecycle notice to administrators")
    notice_group = notice.add_mutually_exclusive_group(required=True)
    notice_group.add_argument("--text", help="notice text")
    notice_group.add_argument("--lifecycle", choices=("shutdown", "restarting", "restarted"), help="standard lifecycle notice")
    serve = commands.add_parser("serve", help="connect NapCat to the directory-based QQ controller")
    serve.add_argument(
        "--diagnostic-echo",
        action="store_true",
        help="reply to the admin locally without invoking OOPZ, transcription, analysis, or DeepSeek",
    )
    state = commands.add_parser("state", help="show the gateway health state")
    state.add_argument("--state-root", type=Path, default=Path("controller_state"))
    return parser


def _read_state(root: Path) -> dict:
    path = root.resolve() / "onebot_gateway.json"
    if not path.is_file():
        return {"status": "not_started", "state_file": str(path)}
    return json.loads(path.read_text(encoding="utf-8"))


async def _run(args: argparse.Namespace) -> dict | None:
    if args.command == "state":
        return _read_state(args.state_root)
    config = OneBotGatewayConfig.from_env()
    if args.command == "validate-config":
        return config.public_summary()
    if args.command == "diagnose":
        return await diagnose_connection(config)
    if args.command == "notify-admin":
        lifecycle_text = {
            "shutdown": "OOPZ QQ 机器人已关闭。",
            "restarting": "OOPZ QQ 机器人正在重启。",
            "restarted": "OOPZ QQ 机器人重启完毕。",
        }
        return await notify_administrators(config, lifecycle_text.get(args.lifecycle, args.text))
    if args.command == "serve":
        if args.diagnostic_echo:
            bridge = DiagnosticEchoBridge()
            mode = "diagnostic_echo"
        else:
            bridge = ControllerDirectoryBridge(
                config.state_root,
                timeout_seconds=config.controller_reply_timeout_seconds,
            )
            mode = "controller"
        gateway = OneBotGateway(config, bridge, mode=mode)
        print(
            "OneBot 网关已启动。"
            + ("当前为隔离测试模式，不会调用 OOPZ 或 DeepSeek。" if args.diagnostic_echo else "正在等待管理员私聊指令。"),
            flush=True,
        )
        await gateway.serve_forever()
        return None
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    parser = _parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        result = asyncio.run(_run(args))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        logging.getLogger(__name__).exception("OneBot command failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
