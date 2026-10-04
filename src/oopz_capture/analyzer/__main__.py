"""python -m oopz_capture.analyzer analyze SESSION_DIR --out DIR   (needs the Qoder CLI environment)
python -m oopz_capture.analyzer render DIR                    (needs Pillow and the fonts)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backend import QoderCli
from .outputs import render, save, save_failure
from .pipeline import AnalysisError, analyze_session
from .transcript import load_session
from .windows import split_windows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m oopz_capture.analyzer")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("analyze", help="analyse a whole session")
    run.add_argument("session_dir", type=Path)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--parallelism", type=int, default=3)
    run.add_argument("--plan", action="store_true", help="only print the windows, call no model")
    draw = sub.add_parser("render", help="render digest.png and digest.md from an analysis directory")
    draw.add_argument("out", type=Path)
    args = parser.parse_args(argv)

    if args.command == "render":
        manifest = render(args.out)
        print(json.dumps({k: manifest[k] for k in ("width", "height", "png_bytes", "warnings")}, ensure_ascii=False))
        return 0
    session = load_session(args.session_dir)
    windows = split_windows(session.runs)
    print(f"{len(session.runs)} runs, {sum(len(r.text) for r in session.runs)} characters, "
          f"{len(session.roster)} speakers, {len(windows)} windows "
          f"({[w.chars for w in windows]} characters each)", flush=True)
    if args.plan:
        return 0
    try:
        analysis = analyze_session(session, QoderCli.from_env(), parallelism=args.parallelism, windows=windows)
    except AnalysisError as error:
        save_failure(args.out, error)
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    save(session, args.session_dir, analysis, args.out)
    seconds = sum(call["seconds"] for call in analysis.calls)
    print(f"done: {len(analysis.calls)} model calls ({seconds:.0f}s of calls), coverage {analysis.coverage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
