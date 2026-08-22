"""Resume one failed QQ-triggered analysis and queue its normal QQ delivery.

This is intentionally a narrow recovery utility: it does not start a second
controller or acquire the controller instance lock.  It reuses the same
analysis checkpoint and OneBot outbox as the running controller.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from oopz_capture.env_loader import load_project_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resume failed OOPZ analysis and QQ delivery")
    parser.add_argument("--session", required=True, help="session directory name")
    parser.add_argument("--admin", required=True, help="administrator QQ number")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    if not args.admin.isascii() or not args.admin.isdigit():
        raise ValueError("--admin must be a numeric QQ number")
    load_project_env()
    from oopz_capture.qq_controller import QQControllerConfig, QQControllerService

    service = QQControllerService(QQControllerConfig.from_env())
    session_dir = (service.output_root / args.session).resolve()
    if session_dir.parent != service.output_root.resolve() or not session_dir.is_dir():
        raise ValueError("--session must name an existing direct child of OOPZ_OUTPUT_ROOT")
    await service._analyze_and_deliver(session_dir, args.admin)
    print(f"Analysis recovery completed or queued delivery for session: {session_dir.name}")


if __name__ == "__main__":
    asyncio.run(main())
