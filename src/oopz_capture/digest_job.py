"""One session -> analysis files and the finished digest image (what the controller runs after a recording)."""
from __future__ import annotations

import sys
from pathlib import Path

from .analyzer.backend import QoderCli
from .analyzer.outputs import card_fit_check, render, save, save_failure
from .analyzer.pipeline import AnalysisError, analyze_session
from .analyzer.transcript import load_session
from .analyzer.windows import split_windows
from .sessions import analysis_dir


def run_digest(session_dir: Path) -> dict:
    """Analyse the transcript (Qoder CLI), render the card; returns {"png": path}.  Raises AnalysisError."""
    session_dir = Path(session_dir)
    out = analysis_dir(session_dir)
    session = load_session(session_dir)
    windows = split_windows(session.runs)
    print(f"[分析] {session_dir.name}：{len(session.runs)} 段发言，{len(windows)} 个窗口", file=sys.stderr, flush=True)
    try:
        analysis = analyze_session(session, QoderCli.from_env(), windows=windows, fit=card_fit_check(session, session_dir))
    except AnalysisError as error:
        save_failure(out, error)
        raise
    save(session, session_dir, analysis, out)
    manifest = render(out)
    return {"png": str(out / "digest" / "digest.png"), "warnings": manifest.get("warnings", [])}
