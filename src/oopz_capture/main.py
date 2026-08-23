from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Sequence


def _add_voice_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--area", required=True, help="OOPZ area ID")
    parser.add_argument("--channel", required=True, help="OOPZ voice channel ID")
    parser.add_argument("--rtc-uid", help="override the current account's numeric RTC UID")
    parser.add_argument("--output-root", type=Path, default=Path("output"), help="local output directory")
    parser.add_argument("--show-browser", action="store_true", help="show Chromium for diagnostics")
    parser.add_argument("--consent-confirmed", action="store_true", help="confirm that every participant knows recording is active")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oopz-capture", description="Local OOPZ/Agora isolated-track voice capture POC")
    parser.add_argument("--verbose", action="store_true", help="enable diagnostic logs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="list joined areas and channels")
    discover.add_argument("--json", action="store_true", help="print JSON")

    probe = subparsers.add_parser("probe", help="join briefly and verify identity mappings")
    _add_voice_target(probe)
    probe.add_argument("--wait-seconds", type=float, default=25.0, help="maximum mapping wait time")
    probe.add_argument("--allow-inferred", action="store_true", help="return success for unverified mappings")

    capture = subparsers.add_parser("capture", help="remain in-channel and record one WAV per remote Agora UID")
    _add_voice_target(capture)
    capture.add_argument("--duration", type=float, default=90.0, help="seconds to stay; 0 means until Ctrl+C")
    capture.add_argument("--poll-interval", type=float, default=0.25, help="seconds between PCM drains")

    analyze = subparsers.add_parser("analyze", help="detect simultaneous energy in captured UID tracks")
    analyze.add_argument("session_dir", type=Path, help="output/<session-id> directory")
    analyze.add_argument("--threshold-dbfs", type=float, default=-45.0)
    analyze.add_argument("--window-ms", type=int, default=100)
    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    logging.getLogger("oopz_sdk.transport").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def _config(*, show_browser: bool = False) -> Any:
    from oopz_sdk import OopzConfig
    # This deployment supports only phone/password login. Force the SDK route
    # so inherited environment state cannot select a direct-credentials login.
    os.environ["OOPZ_LOGIN_METHOD"] = "password"
    return await OopzConfig.from_env_async(voice_backend="browser", voice_browser_headless=not show_browser)


async def _discover(args: argparse.Namespace) -> int:
    import json
    from oopz_sdk import OopzRESTClient
    from .readable import readable_nickname

    config = await _config()
    output: list[dict[str, Any]] = []
    async with OopzRESTClient(config) as client:
        areas = await client.areas.get_joined_areas()
        for area in areas:
            groups = await client.areas.get_area_channels(area.area_id)
            area_item = {"area_id": area.area_id, "name": area.name, "groups": []}
            for group in groups:
                group_item = {
                    "group_id": group.group_id,
                    "name": group.name,
                    "channels": [{"channel_id": channel.channel_id, "name": channel.name, "type": str(channel.channel_type)} for channel in group.channels],
                }
                area_item["groups"].append(group_item)
            output.append(area_item)
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        for area in output:
            print(f"[AREA] {readable_nickname(area['name'])} | Area ID={area['area_id']}")
            for group in area["groups"]:
                print(f"  [GROUP] {readable_nickname(group['name'])} | Group ID={group['group_id']}")
                for channel in group["channels"]:
                    print(f"    [CHANNEL] {readable_nickname(channel['name'])} | Channel ID={channel['channel_id']} | type={channel['type']}")
    return 0


def _consent_ok(args: argparse.Namespace) -> bool:
    if args.consent_confirmed:
        return True
    print("Refusing to join or record: pass --consent-confirmed only after every participant has been informed.", file=sys.stderr)
    return False


async def _probe(args: argparse.Namespace) -> int:
    if not _consent_ok(args):
        return 3
    if args.wait_seconds < 0:
        print("--wait-seconds must be non-negative", file=sys.stderr)
        return 3
    from .session import run_probe
    config = await _config(show_browser=args.show_browser)
    result = await run_probe(config, area=args.area.strip(), channel=args.channel.strip(), wait_seconds=args.wait_seconds, output_root=args.output_root.resolve(), rtc_uid=args.rtc_uid)
    print(f"Diagnostic output: {result.session_dir}")
    if result.all_verified or args.allow_inferred:
        return 0
    print("Milestone 3 is not accepted: one or more mappings lack live Agora evidence.", file=sys.stderr)
    return 2


async def _capture(args: argparse.Namespace) -> int:
    if not _consent_ok(args):
        return 3
    if args.duration < 0 or not 0.05 <= args.poll_interval <= 5:
        print("--duration must be non-negative and --poll-interval must be 0.05 to 5 seconds", file=sys.stderr)
        return 3
    from .capture_session import run_capture
    config = await _config(show_browser=args.show_browser)
    session_dir = await run_capture(config, area=args.area.strip(), channel=args.channel.strip(), duration_seconds=args.duration, poll_interval=args.poll_interval, output_root=args.output_root.resolve(), rtc_uid=args.rtc_uid)
    print(f"Capture output: {session_dir}")
    print(f"Next: oopz-capture analyze \"{session_dir}\"")
    return 0


async def _analyze(args: argparse.Namespace) -> int:
    from .analysis import analyze_session
    result = analyze_session(args.session_dir.resolve(), threshold_dbfs=args.threshold_dbfs, window_ms=args.window_ms)
    print(f"Analyzed tracks: {len(result['tracks'])}; overlap intervals: {len(result['overlaps'])}")
    print(f"Human-readable report: {args.session_dir.resolve() / 'analysis' / 'overlap.md'}")
    return 0


async def _run(args: argparse.Namespace) -> int:
    if args.command == "discover": return await _discover(args)
    if args.command == "probe": return await _probe(args)
    if args.command == "capture": return await _capture(args)
    if args.command == "analyze": return await _analyze(args)
    raise AssertionError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("Stopped by user.", file=sys.stderr)
        return 130
    except Exception as error:
        logging.getLogger(__name__).exception("command failed")
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
