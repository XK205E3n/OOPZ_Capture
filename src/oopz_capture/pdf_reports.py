"""Markdown report PDF rendering through the project-local md-to-pdf tool."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .jsonio import read_json as _read_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_PATH = PROJECT_ROOT / "tools" / "node" / "node.exe"
RENDERER = PROJECT_ROOT / "tools" / "md_to_pdf.mjs"
NODE_MODULES = PROJECT_ROOT / "node_modules"
REPORT_ARCHIVE_SCHEMA = "oopz.report.archive.v1"
PDF_SETUP_HINT = "run `pnpm install` in the project root to restore PDF reports"


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or "report"


def _duration_label(seconds: float) -> str:
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours and minutes:
        return f"{hours}h{minutes:02d}m"
    if hours:
        return f"{hours}h"
    if minutes and seconds:
        return f"{minutes}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def session_report_stamp(session_dir: Path) -> tuple[str, str]:
    """Return (Beijing date folder, date-start-duration filename prefix)."""
    session_dir = session_dir.resolve()
    session = _read_json(session_dir / "session.json") if (session_dir / "session.json").is_file() else {}
    lifecycle = _read_json(session_dir / "lifecycle.json") if (session_dir / "lifecycle.json").is_file() else {}
    started_text = str(lifecycle.get("capture_started_at") or session.get("capture_clock_started_at") or session.get("started_at") or "")
    stopped_text = str(lifecycle.get("stopped_at") or "")
    try:
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})_BJT", session_dir.name)
        if not match:
            raise ValueError(f"cannot determine recording start time: {session_dir}")
        date, clock = match.groups()
        return date, f"{date}_{clock}_BJT"
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    beijing = started.astimezone(timezone.utc).astimezone(timezone(timedelta(hours=8)))
    duration = 0.0
    if stopped_text:
        try:
            stopped = datetime.fromisoformat(stopped_text.replace("Z", "+00:00"))
            if stopped.tzinfo is None:
                stopped = stopped.replace(tzinfo=timezone.utc)
            duration = max(0.0, (stopped - started).total_seconds())
        except ValueError:
            pass
    return beijing.strftime("%Y-%m-%d"), f"{beijing.strftime('%Y-%m-%d_%H-%M-%S')}_BJT_{_duration_label(duration)}"


def render_markdown_pdf(markdown_path: Path, output_path: Path) -> Path:
    markdown_path = markdown_path.resolve()
    output_path = output_path.resolve()
    if not RENDERER.is_file():
        raise FileNotFoundError(f"md-to-pdf renderer is missing: {RENDERER}")
    if not NODE_PATH.is_file():
        raise FileNotFoundError(f"Project Node runtime is missing: {NODE_PATH}")
    if not NODE_MODULES.is_dir():
        raise FileNotFoundError(f"md-to-pdf dependencies are not installed; {PDF_SETUP_HINT}")
    if markdown_path.suffix.lower() != ".md":
        raise ValueError(f"expected Markdown input: {markdown_path}")
    result = subprocess.run(
        [str(NODE_PATH), str(RENDERER), str(markdown_path), str(output_path)],
        cwd=str(PROJECT_ROOT),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError(f"PDF renderer returned without creating {output_path}: {result.stdout}")
    return output_path


def render_session_reports(session_dir: Path, reports: Iterable[tuple[Path, str]]) -> list[Path]:
    """Render reports into output/Report/<Beijing date>/ with stable names."""
    session_dir = session_dir.resolve()
    date_folder, stamp = session_report_stamp(session_dir)
    output_dir = session_dir.parent / "Report" / date_folder
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: list[Path] = []
    for markdown_path, label in reports:
        output_path = output_dir / f"{stamp}_{_safe_name(label)}.pdf"
        rendered.append(render_markdown_pdf(markdown_path, output_path))
    manifest_path = session_dir / "report_archive.json"
    previous: dict = {}
    if manifest_path.is_file():
        try:
            value = _read_json(manifest_path)
            previous = value if value.get("schema_version") == REPORT_ARCHIVE_SCHEMA else {}
        except (OSError, ValueError, TypeError):
            previous = {}
    output_root = session_dir.parent.resolve()
    archived = {
        str(path.resolve().relative_to(output_root)).replace("\\", "/")
        for path in rendered
    }
    archived.update(str(item) for item in previous.get("files", []) if str(item).strip())
    payload = {
        "schema_version": REPORT_ARCHIVE_SCHEMA,
        "session_id": session_dir.name,
        "files": sorted(archived),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
    }
    temporary = manifest_path.with_name(manifest_path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return rendered
