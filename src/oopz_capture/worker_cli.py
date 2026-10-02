from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Sequence
from uuid import UUID, uuid4

from .vad import VADConfig
from .workflow import REQUEST_SCHEMA, WorkflowRequest, emit_event, run_workflow


def _common_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output-root", type=Path, default=Path("output"))
    parser.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    parser.add_argument("--show-browser", action="store_true")
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--min-speech-ms", type=int, default=250)
    parser.add_argument("--min-silence-ms", type=int, default=300)
    parser.add_argument("--speech-pad-ms", type=int, default=200)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oopz-worker",
        description="Managed OOPZ capture-to-transcript worker",
    )
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="record, transcribe, validate, hand off, and purge audio")
    run.add_argument("--area", required=True, dest="area_id")
    run.add_argument("--channel", required=True, dest="channel_id")
    run.add_argument("--duration", type=float, required=True, dest="duration_seconds")
    run.add_argument("--request-id", default=None)
    run.add_argument("--language", choices=["auto", "zh", "en", "yue", "ja", "ko"], default="auto")
    run.add_argument("--retain-audio", action="store_true", help="retain multitrack audio after transcription for ASR experiments")
    run.add_argument("--deadline-seconds", type=int, default=900, dest="processing_deadline_seconds")
    run.add_argument("--retention-hours", type=int, default=360)
    run.add_argument("--poll-interval", type=float, default=0.25, dest="poll_interval_seconds")
    run.add_argument("--rtc-uid")
    run.add_argument("--consent-confirmed", action="store_true")
    _common_run_options(run)

    request = commands.add_parser("run-request", help="consume a controller JSON request")
    request.add_argument("request_file", type=Path)
    _common_run_options(request)

    validate = commands.add_parser("validate-request", help="validate and normalize a controller request")
    validate.add_argument("request_file", type=Path)

    return parser


def _load_request(path: Path) -> WorkflowRequest:
    value = json.loads(path.resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("request file must contain one JSON object")
    return WorkflowRequest.from_dict(value)


def _request_from_args(args: argparse.Namespace) -> WorkflowRequest:
    request_id = args.request_id or str(uuid4())
    try:
        UUID(request_id)
    except ValueError as error:
        raise ValueError("--request-id must be a UUID") from error
    return WorkflowRequest.from_dict({
        "schema_version": REQUEST_SCHEMA,
        "command": "record_and_transcribe",
        "request_id": request_id,
        "area_id": args.area_id,
        "channel_id": args.channel_id,
        "duration_seconds": args.duration_seconds,
        "consent_confirmed": args.consent_confirmed,
        "language": args.language,
        "processing_deadline_seconds": args.processing_deadline_seconds,
        "retention_hours": args.retention_hours,
        "poll_interval_seconds": args.poll_interval_seconds,
        "retain_audio": args.retain_audio,
        "rtc_uid": args.rtc_uid,
        "requested_by": {"source": "local_cli"},
    })


def _vad_config(args: argparse.Namespace) -> VADConfig:
    return VADConfig(
        threshold=args.vad_threshold,
        min_speech_ms=args.min_speech_ms,
        min_silence_ms=args.min_silence_ms,
        speech_pad_ms=args.speech_pad_ms,
    )


async def _run_job(args: argparse.Namespace, request: WorkflowRequest) -> int:
    from .main import _config

    emit_event("request.accepted", request.request_id, command=request.command)
    config = await _config(show_browser=args.show_browser)
    session_dir = await run_workflow(
        config,
        request,
        output_root=args.output_root,
        show_browser=args.show_browser,
        device=args.device,
        vad_config=_vad_config(args),
    )
    print(f"任务完成；Session ID={session_dir.name}")
    print(f"人类可读转写：{session_dir / 'transcript.md'}")
    print(f"分析器输入：{session_dir / 'handoff' / 'analyzer_request.json'}")
    return 0


async def _async_main(args: argparse.Namespace) -> int:
    if args.command == "run":
        return await _run_job(args, _request_from_args(args))
    if args.command == "run-request":
        return await _run_job(args, _load_request(args.request_file))
    raise AssertionError(args.command)


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    try:
        if args.command == "validate-request":
            request = _load_request(args.request_file)
            print(json.dumps(request.to_dict(), ensure_ascii=False, indent=2))
            return 0
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("任务被用户停止；未确认转写成功，因此录音不会自动删除。", file=sys.stderr)
        return 130
    except Exception as error:
        logging.getLogger(__name__).exception("worker command failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
