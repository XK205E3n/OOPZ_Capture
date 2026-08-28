from __future__ import annotations

from pathlib import Path
import json

import pytest

from oopz_capture.controller import ControllerConfig, ControllerService, _parse_duration_seconds
from oopz_capture.controller_protocol import SenderPolicy


def controller_config(tmp_path: Path) -> ControllerConfig:
    return ControllerConfig(
        output_root=tmp_path / "output",
        state_root=tmp_path / "feishu_state",
        authorization=SenderPolicy(frozenset({"member-1"}), frozenset({"group-1"})),
        consent_confirmed=True,
    )


def test_duration_is_optional_but_has_safe_bounds() -> None:
    assert _parse_duration_seconds("") is None
    assert _parse_duration_seconds("45m") == 2700
    with pytest.raises(ValueError, match="5 秒到 24 小时"):
        _parse_duration_seconds("2")


def test_controller_uses_feishu_state_root(tmp_path: Path) -> None:
    service = ControllerService(controller_config(tmp_path))
    assert service.state_root == (tmp_path / "feishu_state").resolve()
    assert service.output_root == (tmp_path / "output").resolve()


def test_controller_requires_explicit_recording_consent(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="recording consent"):
        ControllerConfig(
            output_root=tmp_path / "output",
            state_root=tmp_path / "state",
            authorization=SenderPolicy(frozenset({"member-1"})),
            consent_confirmed=False,
        ).validate()


def test_controller_recovers_active_worker_lifecycle(tmp_path: Path) -> None:
    session_id = "2026-08-22_20-35-59_BJT"
    session = tmp_path / "output" / session_id
    session.mkdir(parents=True)
    (session / "lifecycle.json").write_text(json.dumps({
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "request_id": "request-1",
        "status": "recording",
        "started_at": "2026-08-22T12:36:00+00:00",
    }), encoding="utf-8")
    (session / "request.json").write_text(json.dumps({
        "request_id": "request-1",
        "area_id": "area-1",
        "channel_id": "channel-1",
        "requested_by": {"source": "feishu"},
    }), encoding="utf-8")

    service = ControllerService(controller_config(tmp_path))

    assert service._state["active"]["session_id"] == session_id
    assert service._state["active"]["status"] == "recording"
    assert service._state["active"]["recovered_from_lifecycle"] is True


def test_controller_reconciles_stale_failure_with_completed_capture(tmp_path: Path) -> None:
    session_id = "2026-08-22_20-35-59_BJT"
    session = tmp_path / "output" / session_id
    session.mkdir(parents=True)
    state = tmp_path / "feishu_state"
    state.mkdir()
    (state / "controller.json").write_text(json.dumps({
        "schema_version": "oopz.controller.controller.state.v1",
        "active": None,
        "last_job": {
            "session_id": session_id,
            "status": "ready_for_analysis",
            "error_type": "NameError",
            "error": "old monitor error",
            "finished_at": "2026-08-22T12:36:01+00:00",
        },
    }), encoding="utf-8")
    (session / "lifecycle.json").write_text(json.dumps({
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "status": "ready_for_analysis",
        "stop_reason": "operator_stop_command",
        "stopped_at": "2026-08-22T15:42:09+00:00",
        "chunks_total": 38,
        "chunks_transcribed": 38,
        "chunks_failed": 0,
    }), encoding="utf-8")

    service = ControllerService(controller_config(tmp_path))

    assert service._state["last_job"]["status"] == "ready_for_analysis"
    assert service._state["last_job"]["chunks_transcribed"] == 38
    assert "error" not in service._state["last_job"]


def test_controller_startup_recovers_interrupted_analysis_for_status(tmp_path: Path) -> None:
    session_id = "2026-08-27_19-14-12_BJT"
    session = tmp_path / "output" / session_id
    state = tmp_path / "feishu_state"
    state.mkdir()
    (session / "handoff").mkdir(parents=True)
    variant = session / "analysis_variants" / "configured-api"
    variant.mkdir(parents=True)
    (session / "handoff" / "analyzer_request.json").write_text("{}", encoding="utf-8")
    (variant / ".run.lock").write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    (variant / "lifecycle.json").write_text(json.dumps({
        "status": "analyzing_short_windows", "updated_at": "2026-08-27T01:29:42+00:00",
    }), encoding="utf-8")
    (state / "controller.json").write_text(json.dumps({
        "schema_version": "oopz.controller.controller.state.v1",
        "active": None,
        "last_job": {"session_id": session_id, "status": "analyzing"},
    }), encoding="utf-8")

    service = ControllerService(controller_config(tmp_path))

    assert service._state["last_job"]["status"] == "analysis_interrupted_recoverable"
    assert not (variant / ".run.lock").exists()
