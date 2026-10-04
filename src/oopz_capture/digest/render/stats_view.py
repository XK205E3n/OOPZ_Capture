"""Format trusted deterministic statistics for display. Never computes a ranking.

The ranking, ties and eligibility come from ``oopz_capture.digest_stats``
(``compute_frequency_stats``). This module only turns the already-selected rows
into display strings, using exact rational arithmetic so that display rounding
can never change who is listed (the rate shown is rounded half-up to 2 decimals;
the ordering was decided on exact integers upstream).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from typing import Any, Mapping

from .textlayout import RenderError, sanitize_text


def format_rate(count: int, observed_ms: int) -> str:
    """merged_speech_segment_count / (observed_in_channel_seconds / 60), 2 dp half-up."""
    if not isinstance(count, int) or not isinstance(observed_ms, int) or observed_ms <= 0 or count < 0:
        raise RenderError("stats:invalid_row")
    exact = Fraction(count * 60_000, observed_ms)
    d = Decimal(exact.numerator) / Decimal(exact.denominator)
    return str(d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _minutes(ms: int) -> str:
    return f"{Decimal(ms) / Decimal(60_000):f}".rstrip("0").rstrip(".") if ms % 60_000 else str(ms // 60_000)


def _card(label: str, block: Mapping, labels: Mapping) -> dict:
    people = block.get("people") or []
    if block.get("status") != "available" or not people:
        raise RenderError("stats:card_unavailable")
    first = people[0]
    names = "、".join(sanitize_text(p["nickname"]) or sanitize_text(p["oopz_uid"]) for p in people)
    rate = format_rate(first["merged_utterance_count"], first["observed_presence_ms"])
    if len(people) == 1:
        mins = f"{Decimal(first['observed_presence_ms']) / Decimal(60_000):.1f}"
        detail = f"{first['merged_utterance_count']}段发言 · {mins}分钟观测"
    else:
        detail = labels["stats_tied"]
    return {"label": label, "names": names, "value": f"{rate} {labels['stats_unit']}", "detail": detail,
            "tag": None}


def build_stats_view(stats: Mapping | None, *, metadata: Mapping, labels: Mapping) -> dict | None:
    """Return the view block, or None when there is no stats input at all.

    ``{"status": "unavailable", ...}`` is an honest abstention, never filled in.
    """
    if stats is None:
        return {"status": "unavailable", "text": labels["stats_unavailable"]}
    if stats.get("status") != "available":
        return {"status": "unavailable", "text": labels["stats_unavailable"]}
    simulated = stats.get("simulation") is True
    synthetic = bool(metadata.get("synthetic"))
    if simulated != synthetic:
        # never mix a simulated ranking into a real report, or a real one into a demo
        raise RenderError("stats:simulation_mismatch", "stats and metadata disagree on synthetic data")
    try:
        duration = int(stats["session_duration_ms"])
        highest = _card(labels["stats_highest"], stats["most_frequent"], labels)
        lowest = _card(labels["stats_lowest"], stats["least_frequent"], labels)
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderError("stats:malformed", type(exc).__name__) from exc
    half = f"{float(Fraction(duration, 120_000)):g}"
    view: dict[str, Any] = {
        "status": "available", "simulated": simulated, "notice": None, "scope_note": None,
        "rows": [highest, lowest],
        "method_note": f"只有观测接入严格超过{half}分钟才参评；频率＝合并发言段数÷观测在场分钟。恰好一半的参与者被排除。",
    }
    if simulated:
        notice = sanitize_text(stats.get("simulation_notice") or "") or "统计演示：进出时间/发言计数为模拟"
        view["notice"] = notice
        view["scope_note"] = f"完整{_minutes(duration)}分钟的虚构会话，不是真实用户排名"
        for row in view["rows"]:
            row["tag"] = labels["stats_simulated_tag"]
    return view
