from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json

import pytest

from oopz_capture.controller import START_FLOW_SCHEMA, ControllerConfig, ControllerService, _parse_duration_seconds
from oopz_capture.controller_protocol import SenderPolicy
from oopz_capture.jsonio import atomic_json


def controller_config(tmp_path: Path) -> ControllerConfig:
    return ControllerConfig(
        output_root=tmp_path / "output",
        state_root=tmp_path / "feishu_state",
        authorization=SenderPolicy(frozenset({"member-1"}), frozenset({"group-1"})),
    )


def test_duration_is_optional_but_has_safe_bounds() -> None:
    assert _parse_duration_seconds("") is None
    assert _parse_duration_seconds("45m") == 2700
    with pytest.raises(ValueError, match="5 秒到 24 小时"):
        _parse_duration_seconds("2")


def test_start_flow_expires_after_ttl(tmp_path: Path) -> None:
    service = ControllerService(controller_config(tmp_path))
    stale = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat(timespec="milliseconds")
    atomic_json(service._start_flow_path, {
        "schema_version": START_FLOW_SCHEMA,
        "admin_id": "member-1",
        "stage": "awaiting_area_selection",
        "max_runtime_seconds": None,
        "areas": [],
        "updated_at": stale,
    })

    assert service._load_start_flow() is None
    assert not service._start_flow_path.exists()


def test_controller_uses_feishu_state_root(tmp_path: Path) -> None:
    service = ControllerService(controller_config(tmp_path))
    assert service.state_root == (tmp_path / "feishu_state").resolve()
    assert service.output_root == (tmp_path / "output").resolve()


def test_controller_retires_orphaned_worker_lifecycle_on_restart(tmp_path: Path) -> None:
    session_id = "2026-08-22_20-35-59_BJT"
    session = tmp_path / "output" / session_id
    session.mkdir(parents=True)
    lifecycle_payload = {
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "request_id": "request-1",
        "status": "recording",
        "started_at": "2026-08-22T12:36:00+00:00",
    }
    (session / "lifecycle.json").write_text(json.dumps(lifecycle_payload), encoding="utf-8")
    (session / "request.json").write_text(json.dumps({
        "request_id": "request-1",
        "area_id": "area-1",
        "channel_id": "channel-1",
        "requested_by": {"source": "feishu"},
    }), encoding="utf-8")

    service = ControllerService(controller_config(tmp_path))

    # No capture task exists in this process, so the active lifecycle describes
    # a dead run: it must be retired instead of adopted.
    assert service._state["active"] is None
    retired = json.loads((session / "lifecycle.json").read_text(encoding="utf-8"))
    assert retired["status"] == "interrupted"
    assert retired["previous_status"] == "recording"
    assert retired["interrupted_reason"] == "controller_restarted_without_capture_driver"


def test_controller_adopts_active_lifecycle_only_while_driven(tmp_path: Path) -> None:
    session_id = "2026-08-22_20-35-59_BJT"
    session = tmp_path / "output" / session_id
    session.mkdir(parents=True)
    lifecycle_payload = {
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "request_id": "request-1",
        "status": "recording",
        "started_at": "2026-08-22T12:36:00+00:00",
    }
    (session / "lifecycle.json").write_text(json.dumps(lifecycle_payload), encoding="utf-8")
    (session / "request.json").write_text(json.dumps({
        "request_id": "request-1",
        "area_id": "area-1",
        "channel_id": "channel-1",
        "requested_by": {"source": "feishu"},
    }), encoding="utf-8")

    async def scenario() -> None:
        service = ControllerService(controller_config(tmp_path))
        assert service._state["active"] is None
        # Simulate a live capture driver, then re-arm the active lifecycle.
        (session / "lifecycle.json").write_text(json.dumps(lifecycle_payload), encoding="utf-8")
        driver = asyncio.create_task(asyncio.sleep(3600))
        try:
            service._active_task = driver
            recovered = service._recover_active_session()
            assert recovered is not None
            assert recovered["session_id"] == session_id
            assert recovered["status"] == "recording"
            assert recovered["recovered_from_lifecycle"] is True
        finally:
            driver.cancel()
            try:
                await driver
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


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


def test_controller_startup_marks_a_dead_analysis_as_interrupted(tmp_path: Path) -> None:
    session_id = "2026-08-27_19-14-12_BJT"
    (tmp_path / "output" / session_id).mkdir(parents=True)
    (tmp_path / "output" / session_id / "lifecycle.json").write_text(json.dumps({"status": "ready_for_analysis"}), encoding="utf-8")
    state = tmp_path / "feishu_state"
    state.mkdir()
    (state / "controller.json").write_text(json.dumps({
        "schema_version": "oopz.controller.controller.state.v1",
        "active": None,
        "last_job": {"session_id": session_id, "status": "analyzing"},
    }), encoding="utf-8")

    service = ControllerService(controller_config(tmp_path))

    assert service._state["last_job"]["status"] == "analysis_interrupted"


def test_a_finished_recording_is_analysed_and_the_image_and_text_file_are_queued(tmp_path: Path) -> None:
    from oopz_capture.continuous import ContinuousRequest
    from oopz_capture.send_request import list_send_requests

    async def loader(*args):
        return object()

    async def capture(config, request, *, output_root, device, session_id):
        session = output_root / session_id
        session.mkdir(parents=True)
        (session / "lifecycle.json").write_text(json.dumps({"status": "ready_for_analysis", "stop_reason": "done"}), encoding="utf-8")
        return session

    analysed = []

    def analyse(session_dir):
        analysed.append(session_dir.name)
        png = session_dir / "analysis" / "digest" / "digest.png"
        png.parent.mkdir(parents=True)
        png.write_bytes(b"png")
        md = png.with_name("digest.md")
        md.write_text("text", encoding="utf-8")
        return {"png": str(png), "md": str(md), "usage_text": "分析用量\n模型：m"}

    service = ControllerService(controller_config(tmp_path), config_loader=loader, capture_runner=capture, analysis_runner=analyse)

    async def run():
        request = ContinuousRequest(request_id="r", area_id="a", channel_id="c", consent_confirmed=True, requested_by={})
        service._state["active"] = {"session_id": "s1", "status": "recording"}
        await service._run_session("s1", request)
        await service.shutdown()

    asyncio.run(run())
    assert analysed == ["s1"]
    queued = list_send_requests(service.state_root, statuses={"pending"})
    assert [(q["image_path"] or q["file_path"] or "").rsplit("digest", 1)[-1] for q in queued] == [".png", ".md", ""]
    assert [q["text"] for q in queued] == ["", "", "分析用量\n模型：m"]          # image, text file, then the usage note
    assert service._state["last_job"]["status"] == "analysis_completed"


def test_a_failed_analysis_is_reported_in_text_and_can_be_retried(tmp_path: Path) -> None:
    from oopz_capture.send_request import list_send_requests
    session = tmp_path / "output" / "s1"
    session.mkdir(parents=True)

    def broken(session_dir):
        raise RuntimeError("model unreachable")

    service = ControllerService(controller_config(tmp_path), analysis_runner=broken)
    service._state["last_job"] = {"session_id": "s1", "status": "analyzing"}
    asyncio.run(service._analyze_and_deliver(session))
    queued = list_send_requests(service.state_root, statuses={"pending"})
    assert len(queued) == 1 and "model unreachable" in queued[0]["text"] and "重新出图" in queued[0]["text"]
    assert service._state["last_job"]["status"] == "analysis_failed" and "s1" not in service.busy_sessions()
