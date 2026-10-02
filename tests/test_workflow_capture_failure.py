from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from oopz_capture.workflow import REQUEST_SCHEMA, WorkflowRequest, run_workflow


def test_capture_failure_is_managed_and_expires(tmp_path: Path) -> None:
    workflow_request = WorkflowRequest.from_dict({
        "schema_version": REQUEST_SCHEMA,
        "command": "record_and_transcribe",
        "request_id": str(uuid4()),
        "area_id": "area-id",
        "channel_id": "channel-id",
        "duration_seconds": 90,
        "consent_confirmed": True,
        "retention_hours": 168,
    })

    async def capture_runner(config, **kwargs):
        del config
        session_dir = kwargs["output_root"] / kwargs["session_id"]
        (session_dir / "audio").mkdir(parents=True)
        (session_dir / "audio" / "308348510.wav").write_bytes(b"partial audio")
        raise RuntimeError("OOPZ disconnected")

    with pytest.raises(RuntimeError, match="OOPZ disconnected"):
        asyncio.run(run_workflow(
            object(), workflow_request, output_root=tmp_path,
            capture_runner=capture_runner,
        ))

    sessions = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert len(sessions) == 1
    lifecycle = json.loads((sessions[0] / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["managed_by"] == "oopz-worker-v1"
    assert lifecycle["status"] == "failed"
    assert lifecycle["delete_after"]
    assert lifecycle["audio_deleted"] is False
    assert (sessions[0] / "audio" / "308348510.wav").is_file()
