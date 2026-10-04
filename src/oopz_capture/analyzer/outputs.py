"""Turn an analysis into files: metadata and statistics for the card, the audit trail, the rendered PNG + MD."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from ..digest.stats import load_frequency_stats
from .pipeline import Analysis
from .transcript import Session


def build_metadata(session: Session, coverage: dict, flow: list[dict], labels: dict | None = None) -> dict:
    start = session.started_at
    end = session.stopped_at or start + timedelta(milliseconds=session.duration_ms)
    next_day = "次日 " if end.date() != start.date() else ""
    note = "依据录音的自动转写整理，转写可能有错别字。"
    missing = coverage["missing"]
    if missing:
        note += f"有 {len(missing)} 个时间段分析失败，未纳入：" + "、".join(m["time"] for m in missing) + "。"
    labels = labels or {}
    tags = {"odd_label": labels["odd"]} if labels.get("odd") else {}
    tags |= {"topic_labels": labels["topics"]} if labels.get("topics") else {}
    tags |= {"moment_labels": labels["moments"]} if labels.get("moments") else {}
    return tags | {"synthetic": False, "session_id": session.session_id, "duration_ms": session.duration_ms,
            "session": {"date_label": start.strftime("%Y.%m.%d"),
                        "time_label": f"{start:%H:%M} — {next_day}{end:%H:%M}"},
            "timeline": [{"title": item["time"].replace("-", "–"), "text": item["text"]} for item in flow],
            "source_note": note}


def compute_stats(session: Session, session_dir: Path, analysis: Analysis) -> dict:
    complete = not analysis.coverage["missing"]
    return load_frequency_stats(Path(session_dir), session.segments, session.duration_ms, coverage_complete=complete)


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save(session: Session, session_dir: Path, analysis: Analysis, out_dir: Path) -> None:
    """Everything the renderer needs plus the audit trail (what was asked, what was answered)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "content.json", analysis.content)
    _write(out_dir / "bundle.json", analysis.bundle)
    _write(out_dir / "meta.json", build_metadata(session, analysis.coverage, analysis.flow, analysis.labels))
    _write(out_dir / "stats.json", compute_stats(session, session_dir, analysis))
    _write(out_dir / "windows.json", analysis.units)
    _write(out_dir / "coverage.json", analysis.coverage)
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as stream:
        for call in analysis.calls:
            stream.write(json.dumps(call, ensure_ascii=False) + "\n")


def card_fit_check(session: Session, session_dir: Path):
    """A check for analyze_session: render the digest for real and send it back if it does not fit."""
    import tempfile

    from ..digest.render.pipeline import generate
    from ..digest.render.textlayout import RenderError

    stats = load_frequency_stats(Path(session_dir), session.segments, session.duration_ms)

    def check(content: dict, bundle: dict, coverage: dict, flow: list[dict]) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                generate(content, bundle, build_metadata(session, coverage, flow), stats, Path(tmp))
            except RenderError as error:
                if error.code != "height_exceeded":
                    raise
                height, limit = error.details.get("height"), error.details.get("limit")
                raise ValueError(f"card:too_tall the rendered card is {height}px high but at most {limit}px fit; "
                                 "shorten the texts (people comments and topics first) and keep the structure") from None

    return check


def save_failure(out_dir: Path, error) -> None:
    """What was asked and answered before the analysis gave up."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "windows.json", error.units)
    _write(out_dir / "failure.json", {"error": str(error)})
    with (out_dir / "calls.jsonl").open("w", encoding="utf-8") as stream:
        for call in error.calls:
            stream.write(json.dumps(call, ensure_ascii=False) + "\n")


def render(out_dir: Path, avatars: dict | None = None) -> dict:
    """digest.png + digest.md next to the analysis files (needs Pillow and the fonts)."""
    from ..digest.render.pipeline import generate

    out_dir = Path(out_dir)
    load = lambda name: json.loads((out_dir / name).read_text(encoding="utf-8"))  # noqa: E731
    return generate(load("content.json"), load("bundle.json"), load("meta.json"), load("stats.json"),
                    out_dir / "digest", avatars=avatars)
