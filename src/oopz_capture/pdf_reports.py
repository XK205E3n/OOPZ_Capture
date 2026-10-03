"""Markdown report PDF rendering through the project-local md-to-pdf tool."""

from __future__ import annotations

import os
import signal
import shutil
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .jsonio import atomic_json as _atomic_json, read_json as _read_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
NODE_PATH = PROJECT_ROOT / "tools" / "node" / ("node.exe" if os.name == "nt" else "bin/node")
MIN_NODE_VERSION = (22, 12, 0)
RENDERER = PROJECT_ROOT / "tools" / "md_to_pdf.mjs"
NODE_MODULES = PROJECT_ROOT / "node_modules"
REPORT_ARCHIVE_SCHEMA = "oopz.report.archive.v1"
PDF_SETUP_HINT = "run `pnpm install` in the project root to restore PDF reports"
PDF_BACKENDS = {'chromium', 'weasyprint'}


def pdf_backend() -> str:
    """Engine changes are explicit; missing/failed engines never silently fall back."""
    backend = os.environ.get('OOPZ_PDF_BACKEND', 'chromium').strip().lower()
    if backend not in PDF_BACKENDS:
        raise ValueError('OOPZ_PDF_BACKEND must be chromium or weasyprint')
    if backend == 'weasyprint' and sys.platform != 'linux':
        raise RuntimeError('The WeasyPrint backend is supported on Linux; select chromium on Windows')
    return backend


def find_node() -> Path:
    """Choose one runtime, then use its directory first in subprocess PATH."""
    override = os.environ.get("OOPZ_NODE_PATH")
    candidates = [Path(override).expanduser()] if override else [
        NODE_PATH, PROJECT_ROOT / "tools" / "node" / "node",
        Path(shutil.which("node") or "__missing_node__"),
    ]
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if candidate.is_file():
            return candidate.absolute()
    raise FileNotFoundError("Node runtime missing; install Node >=22.12.0 or set OOPZ_NODE_PATH")


def node_environment(node: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
    return env


def validate_node(node: Path, env: dict[str, str]) -> None:
    try:
        result = subprocess.run([str(node), "--version"], env=env, check=True,
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        raise RuntimeError(f"Cannot validate Node runtime: {detail}") from error
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)\s*", result.stdout)
    if not match or tuple(map(int, match.groups())) < MIN_NODE_VERSION:
        raise RuntimeError("PDF dependencies require Node >=22.12.0; selected runtime is too old or invalid")


def _run_renderer(command: list[str], env: dict[str, str], timeout: float = 180) -> subprocess.CompletedProcess:
    # A separate POSIX process group owns only this renderer and its browser.
    # Killing just Node on timeout leaves Chromium children behind.
    process = subprocess.Popen(command, cwd=str(PROJECT_ROOT), env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               start_new_session=os.name != "nt")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.SubprocessError):
                # Still reap Node below when taskkill itself is unavailable.
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.kill()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Never hang cleanup if a Windows descendant retained a pipe.
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()
        raise
    if process.returncode:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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
    backend = pdf_backend()
    if not RENDERER.is_file():
        raise FileNotFoundError(f"md-to-pdf renderer is missing: {RENDERER}")
    node = find_node()
    env = node_environment(node)
    validate_node(node, env)
    if not NODE_MODULES.is_dir():
        raise FileNotFoundError(f"md-to-pdf dependencies are not installed; {PDF_SETUP_HINT}")
    if markdown_path.suffix.lower() != ".md":
        raise ValueError(f"expected Markdown input: {markdown_path}")
    # A stale renderer output must never pass as fresh; delete it first so the
    # size check below can only succeed on a file produced by this run.
    output_path.unlink(missing_ok=True)
    command = ([str(node), str(RENDERER), str(markdown_path), str(output_path)]
               if backend == 'chromium' else
               [sys.executable, '-m', 'oopz_capture.weasy_pdf', str(markdown_path),
                str(output_path), '--node', str(node)])
    try:
        result = _run_renderer(
            command, env,
        )
    except subprocess.TimeoutExpired as error:
        output_path.unlink(missing_ok=True)
        detail = error.stderr or b""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        raise RuntimeError(f"PDF renderer timed out after 180s: {str(detail)[-800:]}") from error
    except subprocess.CalledProcessError as error:
        output_path.unlink(missing_ok=True)
        detail = str(error.stderr or error.stdout or "no renderer diagnostics").strip()
        raise RuntimeError(f"PDF renderer exited {error.returncode}: {detail[-1200:]}") from error
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise
    if not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
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
    _atomic_json(manifest_path, payload)
    return rendered
