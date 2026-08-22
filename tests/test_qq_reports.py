from __future__ import annotations

import os
from pathlib import Path

from oopz_capture.qq_reports import find_recent_reports


def test_find_recent_reports_chooses_newest_variant_within_session(tmp_path: Path) -> None:
    session = tmp_path / "2026-08-21_20-00-00_BJT"
    old = session / "analysis" / "summary.md"
    new = session / "analysis_variants" / "configured-api" / "summary.md"
    old.parent.mkdir(parents=True)
    new.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))

    reports = find_recent_reports(tmp_path)

    assert len(reports) == 1
    assert reports[0]["full_summary_path"] == new
