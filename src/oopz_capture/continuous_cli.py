from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Sequence
from uuid import uuid4

from .continuous import (
    MAX_CHUNK_SECONDS,
    ContinuousRequest,
    repair_continuous_session,
    request_stop,
    run_continuous_capture,
)
from .vad import VADConfig
from .identifiers import validate_session_id
from .workflow import emit_event


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-continuous",
        description="Continuous OOPZ capture with five-minute maximum audio chunks",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser("start", help="join once and record until a stop command or local 04:00")
    start.add_argument("--area", required=True, dest="area_id")
    start.add_argument("--channel", required=True, dest="channel_id")
    start.add_argument("--request-id")
    start.add_argument("--session-id", help=argparse.SUPPRESS)
    start.add_argument("--chunk-seconds", type=int, default=MAX_CHUNK_SECONDS)
    start.add_argument("--cutoff-hour", type=int, default=4, dest="cutoff_local_hour")
    start.add_argument("--language", choices=["auto", "zh", "en", "yue", "ja", "ko"], default="auto")
    start.add_argument("--retain-audio", action="store_true", help="retain multitrack audio after transcription for ASR experiments")
    start.add_argument("--deadline-seconds", type=int, default=900, dest="processing_deadline_seconds")
    start.add_argument("--retention-hours", type=int, default=168)
    start.add_argument("--poll-interval", type=float, default=0.25, dest="poll_interval_seconds")
    start.add_argument("--membership-refresh-seconds", type=float, default=30.0)
    start.add_argument("--membership-timeout-seconds", type=float, default=10.0)
    start.add_argument(
        "--empty-channel-timeout-seconds", type=float, default=300.0,
        help="leave after this many seconds with no channel member other than the recorder (default: 300)",
    )
    start.add_argument("--connection-check-seconds", type=float, default=2.0)
    start.add_argument("--disconnect-grace-seconds", type=float, default=15.0)
    start.add_argument("--browser-operation-timeout-seconds", type=float, default=2.0)
    start.add_argument("--reconnect-window-seconds", type=float, default=300.0)
    start.add_argument("--reconnect-initial-delay-seconds", type=float, default=1.0)
    start.add_argument("--reconnect-max-delay-seconds", type=float, default=30.0)
    start.add_argument("--reconnect-attempt-timeout-seconds", type=float, default=30.0)
    start.add_argument("--rtc-uid")
    start.add_argument("--output-root", type=Path, default=Path("output"))
    start.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    start.add_argument("--show-browser", action="store_true")
    start.add_argument("--consent-confirmed", action="store_true")
    start.add_argument("--max-runtime-seconds", type=float, help=argparse.SUPPRESS)
    start.add_argument("--vad-threshold", type=float, default=0.5)
    start.add_argument("--min-speech-ms", type=int, default=250)
    start.add_argument("--min-silence-ms", type=int, default=300)
    start.add_argument("--speech-pad-ms", type=int, default=200)

    stop = commands.add_parser("stop", help="request a graceful leave and final report handoff")
    stop.add_argument("--session", required=True, dest="session_id")
    stop.add_argument("--output-root", type=Path, default=Path("output"))
    stop.add_argument("--reason", default="qq_leave_command")

    status = commands.add_parser("status", help="show one continuous Session lifecycle")
    status.add_argument("--session", required=True, dest="session_id")
    status.add_argument("--output-root", type=Path, default=Path("output"))

    repair = commands.add_parser("repair", help="retry retained failed chunks and rebuild the final transcript")
    repair.add_argument("--session", required=True, dest="session_id")
    repair.add_argument("--output-root", type=Path, default=Path("output"))
    repair.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    repair.add_argument("--vad-threshold", type=float, default=0.5)
    repair.add_argument("--min-speech-ms", type=int, default=250)
    repair.add_argument("--min-silence-ms", type=int, default=300)
    repair.add_argument("--speech-pad-ms", type=int, default=200)
    return parser


def _session_path(output_root: Path, session_id: str) -> Path:
    session_id = validate_session_id(session_id, "--session")
    root = output_root.resolve()
    path = (root / session_id).resolve()
    if path.parent != root:
        raise ValueError("unsafe Session path")
    return path


def _request(args: argparse.Namespace) -> ContinuousRequest:
    request_id = args.request_id or str(uuid4())
    request = ContinuousRequest(
        request_id=request_id,
        area_id=args.area_id,
        channel_id=args.channel_id,
        consent_confirmed=args.consent_confirmed,
        chunk_seconds=args.chunk_seconds,
        cutoff_local_hour=args.cutoff_local_hour,
        language=args.language,
        processing_deadline_seconds=args.processing_deadline_seconds,
        retention_hours=args.retention_hours,
        poll_interval_seconds=args.poll_interval_seconds,
        membership_refresh_seconds=args.membership_refresh_seconds,
        membership_timeout_seconds=args.membership_timeout_seconds,
        empty_channel_timeout_seconds=args.empty_channel_timeout_seconds,
        retain_audio=args.retain_audio,
        connection_check_seconds=args.connection_check_seconds,
        disconnect_grace_seconds=args.disconnect_grace_seconds,
        browser_operation_timeout_seconds=args.browser_operation_timeout_seconds,
        reconnect_window_seconds=args.reconnect_window_seconds,
        reconnect_initial_delay_seconds=args.reconnect_initial_delay_seconds,
        reconnect_max_delay_seconds=args.reconnect_max_delay_seconds,
        reconnect_attempt_timeout_seconds=args.reconnect_attempt_timeout_seconds,
        rtc_uid=args.rtc_uid,
        requested_by={"source": "local_cli"},
        max_runtime_seconds=args.max_runtime_seconds,
    )
    request.validate()
    return request


async def _start(args: argparse.Namespace) -> int:
    from .main import _config

    request = _request(args)
    emit_event("request.accepted", request.request_id, command=request.command)
    config = await _config(show_browser=args.show_browser)
    session_dir = await run_continuous_capture(
        config,
        request,
        output_root=args.output_root,
        device=args.device,
        vad_config=VADConfig(
            threshold=args.vad_threshold,
            min_speech_ms=args.min_speech_ms,
            min_silence_ms=args.min_silence_ms,
            speech_pad_ms=args.speech_pad_ms,
        ),
        session_id=args.session_id,
    )
    print(f"连续录音结束；Session ID={session_dir.name}")
    print(f"人类可读总转写：{session_dir / 'transcript.md'}")
    print(f"最终分析器输入：{session_dir / 'handoff' / 'analyzer_request.json'}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        if args.command == "start":
            return asyncio.run(_start(args))
        if args.command == "stop":
            path = request_stop(args.output_root, args.session_id, reason=args.reason)
            print(f"已提交离开指令；Session ID={args.session_id} | 控制文件={path}")
            return 0
        if args.command == "status":
            lifecycle_path = _session_path(args.output_root, args.session_id) / "lifecycle.json"
            value = json.loads(lifecycle_path.read_text(encoding="utf-8"))
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0
        if args.command == "repair":
            session_dir = asyncio.run(repair_continuous_session(
                args.output_root, args.session_id, device=args.device,
                vad_config=VADConfig(
                    threshold=args.vad_threshold,
                    min_speech_ms=args.min_speech_ms,
                    min_silence_ms=args.min_silence_ms,
                    speech_pad_ms=args.speech_pad_ms,
                ),
            ))
            lifecycle = json.loads((session_dir / "lifecycle.json").read_text(encoding="utf-8"))
            print(json.dumps({
                "status": lifecycle["status"],
                "Session ID": session_dir.name,
                "chunks_transcribed": lifecycle["chunks_transcribed"],
                "chunks_failed": lifecycle["chunks_failed"],
                "transcript_segments": lifecycle["transcript_segments"],
                "human_transcript": str(session_dir / "transcript.md"),
                "analyzer_request": str(session_dir / "handoff" / "analyzer_request.json"),
                "audio_deleted": lifecycle["audio_deleted"],
            }, ensure_ascii=False, indent=2))
            return 0
        raise AssertionError(args.command)
    except KeyboardInterrupt:
        print("收到本地中断，正在执行安全退出。", file=sys.stderr)
        return 130
    except Exception as error:
        logging.getLogger(__name__).exception("continuous command failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
