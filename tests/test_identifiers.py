from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from oopz_capture.identifiers import is_session_id, new_session_id, validate_session_id


def test_session_id_uses_recording_start_time_in_beijing(tmp_path) -> None:
    started_at = datetime(2026, 8, 13, 14, 15, 30, tzinfo=timezone.utc)
    assert new_session_id(tmp_path, started_at=started_at) == "2026-08-13_22-15-30_BJT"


def test_session_id_uses_incrementing_suffix_for_same_second(tmp_path) -> None:
    started_at = datetime(2026, 8, 13, 14, 15, 30, tzinfo=timezone.utc)
    (tmp_path / "2026-08-13_22-15-30_BJT").mkdir()
    (tmp_path / "2026-08-13_22-15-30_BJT-02").mkdir()
    assert new_session_id(tmp_path, started_at=started_at) == "2026-08-13_22-15-30_BJT-03"


def test_new_and_legacy_session_ids_are_valid_but_unsafe_names_are_rejected() -> None:
    assert is_session_id("2026-08-13_22-15-30_BJT")
    assert is_session_id("2026-08-13_22-15-30_BJT-02")
    assert is_session_id(str(uuid4()))
    for value in ("2026-02-30_22-15-30_BJT", "2026-08-13_22-15-30_BJT-01", "../output"):
        with pytest.raises(ValueError, match="Beijing-time ID"):
            validate_session_id(value)
