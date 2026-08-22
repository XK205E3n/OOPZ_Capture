from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.continuous import ContinuousRequest, _write_final_handoff


def request(**changes) -> ContinuousRequest:
    values = {
        "request_id": str(uuid4()),
        "area_id": "area",
        "channel_id": "channel",
        "consent_confirmed": True,
    }
    values.update(changes)
    return ContinuousRequest(**values)


def test_short_summary_default_is_300_seconds() -> None:
    value = request()
    value.validate()
    assert value.short_summary_seconds == 300
    assert value.chunk_seconds == 300
    assert value.long_summary_seconds == 3600


def test_old_30_second_summary_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be 300"):
        request(short_summary_seconds=30).validate()


def test_final_analyzer_handoff_contains_300_second_window(tmp_path: Path) -> None:
    session = tmp_path / str(uuid4())
    session.mkdir()
    now = datetime.now(timezone.utc)
    path = _write_final_handoff(
        session,
        request(),
        stopped_at=now,
        delete_after=now + timedelta(hours=168),
        segment_count=0,
        chunk_results=[],
    )
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["delivery_mode"] == "final_only"
    assert value["summary_windows"] == {
        "short_summary_seconds": 300,
        "long_summary_seconds": 3600,
    }
