from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def _add_vad_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vad-threshold", type=float, default=0.5)
    parser.add_argument("--min-speech-ms", type=int, default=250)
    parser.add_argument("--min-silence-ms", type=int, default=300)
    parser.add_argument("--speech-pad-ms", type=int, default=200)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oopz-transcribe", description="Offline VAD and ASR for an OOPZ capture session")
    commands = parser.add_subparsers(dest="command", required=True)
    vad = commands.add_parser("vad", help="run Silero VAD")
    vad.add_argument("session_dir", type=Path); _add_vad_options(vad)
    transcribe = commands.add_parser("transcribe", help="run SenseVoice on VAD segments")
    transcribe.add_argument("session_dir", type=Path)
    transcribe.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    transcribe.add_argument("--language", choices=["auto", "zh", "en", "yue", "ja", "ko"], default="auto")
    transcribe.add_argument("--output-stem", default="transcript")
    render = commands.add_parser("render", help="render transcript.md from transcript.jsonl")
    render.add_argument("session_dir", type=Path)
    process = commands.add_parser("process", help="run VAD, SenseVoice, JSONL and Markdown")
    process.add_argument("session_dir", type=Path); _add_vad_options(process)
    process.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    process.add_argument("--language", choices=["auto", "zh", "en", "yue", "ja", "ko"], default="auto")
    return parser


def _vad_config(args: argparse.Namespace):
    from .vad import VADConfig
    return VADConfig(threshold=args.vad_threshold, min_speech_ms=args.min_speech_ms, min_silence_ms=args.min_silence_ms, speech_pad_ms=args.speech_pad_ms)


def main(argv: Sequence[str] | None = None) -> int:
    from .env_loader import load_project_env
    load_project_env()
    args = _parser().parse_args(argv); session_dir = args.session_dir.resolve()
    try:
        if args.command == "vad":
            from .vad import SileroVADBackend, run_vad_session
            values = run_vad_session(session_dir, SileroVADBackend(), _vad_config(args))
            print(f"VAD segments: {len(values)}"); print(f"Output: {session_dir / 'vad' / 'segments.jsonl'}"); return 0
        if args.command == "transcribe":
            from .asr import SenseVoiceBackend
            from .transcript import render_transcript_markdown, transcribe_session
            from .transcript import AUTO_TRANSCRIPT_LANGUAGES
            is_auto = args.language == "auto"
            values = transcribe_session(
                session_dir, SenseVoiceBackend(device=args.device),
                language=None if is_auto else args.language, output_stem=args.output_stem,
                allowed_languages=AUTO_TRANSCRIPT_LANGUAGES if is_auto else None,
            )
            print(f"Transcript segments: {len(values)}"); print(f"Markdown: {render_transcript_markdown(session_dir, values, output_stem=args.output_stem)}"); return 0
        if args.command == "render":
            from .transcript import render_transcript_markdown
            print(f"Markdown: {render_transcript_markdown(session_dir)}"); return 0
        if args.command == "process":
            from .pipeline import run_speech_pipeline
            vad_count, transcript_count, markdown = run_speech_pipeline(session_dir, vad_config=_vad_config(args), device=args.device, language=None if args.language == "auto" else args.language)
            print(f"VAD segments: {vad_count}; transcript segments: {transcript_count}")
            print(f"JSONL: {session_dir / 'transcript.jsonl'}"); print(f"Markdown: {markdown}"); return 0
        raise AssertionError(args.command)
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr); return 1


if __name__ == "__main__": raise SystemExit(main())
