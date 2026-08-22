from __future__ import annotations

from oopz_capture.continuous import estimate_browser_clock_origin, rebase_browser_chunk


def test_browser_clock_origin_removes_pre_join_probe_time() -> None:
    chunk = {
        "sampleRate": 1000,
        "frameCount": 100,
        "sessionOffsetMs": 302100,
    }
    origin = estimate_browser_clock_origin(chunk, session_elapsed_ms=300000)
    assert origin == 2000
    rebased = rebase_browser_chunk(chunk, base_offset_ms=origin + 300000)
    assert rebased["sessionOffsetMs"] == 100
