"""One session -> analysis files and the finished digest image (what the controller runs after a recording)."""
from __future__ import annotations

import time
from pathlib import Path

from .analyzer.backend import QoderCli
from .analyzer.outputs import card_fit_check, render, save, save_failure
from .analyzer.pipeline import AnalysisError, analyze_session
from .analyzer.transcript import load_session
from .analyzer.windows import split_windows
from .sessions import analysis_dir


def _duration(seconds: float) -> str:
    seconds = round(seconds)
    return f"{seconds // 60} 分 {seconds % 60} 秒" if seconds >= 60 else f"{seconds} 秒"


def usage_text(model: str, calls: list[dict], wall_seconds: float) -> str:
    """The short usage note sent to the group after the card: model, requests, time.  (The CLI reports no
    tokens for the free model, so those are not shown; everything it did report stays in calls.jsonl.)"""
    requests = sum(int((call.get("usage") or {}).get("cli_runs") or 1) for call in calls)
    retried = sum(1 for call in calls if call.get("error"))
    model_seconds = sum(float(call.get("seconds") or 0) for call in calls)
    return (f"分析用量\n模型：{model}\n"
            f"请求：{requests} 次" + (f"（其中 {retried} 次因校验未通过而重试）" if retried else "") + "\n"
            f"总耗时：{_duration(wall_seconds)}（模型调用合计 {_duration(model_seconds)}）")


def run_digest(session_dir: Path) -> dict:
    """Analyse the transcript (Qoder CLI), render the card; returns the file paths and the usage note.
    Raises AnalysisError."""
    started = time.monotonic()
    session_dir = Path(session_dir)
    out = analysis_dir(session_dir)
    session = load_session(session_dir)
    windows = split_windows(session.runs)
    print(f"[分析进度] {session_dir.name}：{len(session.runs)} 段发言，{len(windows)} 个窗口", flush=True)
    try:
        backend = QoderCli.from_env()
        analysis = analyze_session(session, backend, windows=windows, fit=card_fit_check(session, session_dir))
    except AnalysisError as error:
        save_failure(out, error)
        raise
    save(session, session_dir, analysis, out)
    manifest = render(out)
    return {"png": str(out / "digest" / "digest.png"), "md": str(out / "digest" / "digest.md"),
            "warnings": manifest.get("warnings", []),
            "usage_text": usage_text(backend.model, analysis.calls, time.monotonic() - started)}
