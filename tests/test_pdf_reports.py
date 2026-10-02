from __future__ import annotations

import json
from pathlib import Path

from oopz_capture import pdf_reports


def test_render_session_reports_writes_atomic_archive_manifest(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "2026-08-22_20-35-59_BJT"
    session.mkdir()
    (session / "session.json").write_text(json.dumps({
        "session_id": session.name,
        "started_at": "2026-08-22T12:35:59+00:00",
    }), encoding="utf-8")
    report = session / "summary.public.md"
    report.write_text("# 测试报告\n", encoding="utf-8")

    def fake_render(_source: Path, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"%PDF-test")
        return destination

    monkeypatch.setattr(pdf_reports, "render_markdown_pdf", fake_render)

    rendered = pdf_reports.render_session_reports(session, [(report, "public-report")])

    assert len(rendered) == 1
    manifest = json.loads((session / "report_archive.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "oopz.report.archive.v1"
    assert manifest["files"] == [str(rendered[0].relative_to(tmp_path)).replace("\\", "/")]
