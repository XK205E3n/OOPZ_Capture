"""Split all runs into consecutive windows; every run lands in exactly one window."""
from __future__ import annotations

from dataclasses import dataclass

from .transcript import Run

MAX_CHARS = 12_000        # text per model call (medium density is roughly 25-30k characters per hour)
MAX_SPAN_MS = 45 * 60_000      # also bounds the timeline: a long, quiet session still gets several steps
QUIET_MS = 90_000         # a pause this long is a preferred place to cut
SOFT_FRACTION = 0.6       # start looking for a pause once the window is this full
MIN_CHARS = 300           # a window with less text than this is merged into its neighbour


@dataclass(frozen=True)
class Window:
    index: int              # 1-based
    runs: list[Run]

    @property
    def start_ms(self) -> int:
        return self.runs[0].start_ms

    @property
    def end_ms(self) -> int:
        return max(run.end_ms for run in self.runs)

    @property
    def chars(self) -> int:
        return sum(len(run.text) for run in self.runs)


def split_windows(runs: list[Run], *, max_chars: int = MAX_CHARS, max_span_ms: int = MAX_SPAN_MS,
                  quiet_ms: int = QUIET_MS) -> list[Window]:
    windows: list[list[Run]] = []
    current: list[Run] = []
    chars = 0
    for run in runs:
        if current:
            span = run.start_ms - current[0].start_ms
            gap = run.start_ms - current[-1].end_ms
            full = max(chars / max_chars, span / max_span_ms)
            if (chars + len(run.text) > max_chars or span > max_span_ms
                    or (full >= SOFT_FRACTION and gap >= quiet_ms)):
                windows.append(current)
                current, chars = [], 0
        current.append(run)
        chars += len(run.text)
    if current:
        windows.append(current)
    merged: list[list[Run]] = []
    for part in windows:
        if merged and sum(len(r.text) for r in part) < MIN_CHARS:
            merged[-1] += part
        elif merged and sum(len(r.text) for r in merged[-1]) < MIN_CHARS:
            merged[-1] += part
        else:
            merged.append(part)
    return [Window(index, part) for index, part in enumerate(merged, 1)]
