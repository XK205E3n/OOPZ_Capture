"""Finding finished recordings and their digest images for the Feishu selection cards."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .jsonio import read_json_or_none
from .workflow import _is_reparse_point

READY = {"ready_for_analysis", "ready_for_analysis_with_errors"}


def analysis_dir(session_dir: Path) -> Path:
    return Path(session_dir) / "analysis"


def digest_png(session_dir: Path) -> Path:
    return analysis_dir(session_dir) / "digest" / "digest.png"


def digest_md(session_dir: Path) -> Path:
    return analysis_dir(session_dir) / "digest" / "digest.md"


def _session_dirs(output_root: Path):
    root = Path(output_root).resolve()
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and not _is_reparse_point(path):
                yield path


def _modified(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_recent_digests(output_root: Path, limit: int = 7) -> list[dict[str, Any]]:
    """Newest sessions that already have a digest image."""
    found = [{"session_id": d.name, "modified_ts": _modified(digest_png(d))} for d in _session_dirs(output_root)
             if digest_png(d).is_file()]
    return sorted(found, key=lambda item: item["modified_ts"], reverse=True)[:limit]


def find_pending_sessions(output_root: Path, busy: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """Recorded and transcribed sessions that have no digest image yet (``busy`` are being analysed right now)."""
    found = []
    for d in _session_dirs(output_root):
        lifecycle = read_json_or_none(d / "lifecycle.json") or {}
        if lifecycle.get("status") in READY and not digest_png(d).is_file() and d.name not in busy:
            found.append({"session_id": d.name, "modified_ts": _modified(d)})
    return sorted(found, key=lambda item: item["modified_ts"], reverse=True)
