from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import oopz_capture.continuous as continuous
from oopz_capture.continuous import (
    ContinuousRequest,
    count_other_members,
    reconnect_delay,
    refresh_participants_safely,
    request_stop,
    run_continuous_capture,
)
from oopz_capture.models import OopzParticipant, ProbeSnapshot
from oopz_capture.output import write_json


def request(**changes) -> ContinuousRequest:
    values = {
        "request_id": str(uuid4()),
        "area_id": "area",
        "channel_id": "channel",
        "consent_confirmed": True,
    }
    values.update(changes)
    value = ContinuousRequest(**values)
    value.validate()
    return value


def test_resilience_defaults_and_backoff_are_bounded() -> None:
    value = request()
    assert value.membership_refresh_seconds == 30
    assert value.reconnect_window_seconds == 300
    assert value.disconnect_grace_seconds == 15
    assert value.empty_channel_timeout_seconds == 300
    assert [reconnect_delay(i, 1, 30) for i in range(1, 8)] == [1, 2, 4, 8, 16, 30, 30]
    with pytest.raises(ValueError, match="reconnect_window_seconds"):
        request(reconnect_window_seconds=29)


def test_count_other_members_excludes_recorder_and_bot() -> None:
    assert count_other_members([
        OopzParticipant(oopz_uid="self", nickname="录音机器人"),
        OopzParticipant(oopz_uid="bot", nickname="SDK bot", is_bot=True),
        OopzParticipant(oopz_uid="friend", nickname="朋友"),
    ], self_oopz_uid="self") == 1


def test_membership_api_failure_is_nonfatal_and_preserves_last_mapping(monkeypatch) -> None:
    async def scenario() -> None:
        prior = OopzParticipant(oopz_uid="prior", nickname="旧映射")
        mappings = {prior.oopz_uid: prior}

        async def fail(*_args, **_kwargs):
            raise RuntimeError("HTTP 522")

        monkeypatch.setattr(continuous, "_resolve_participants", fail)
        ok, error = await refresh_participants_safely(object(), request(), mappings)
        assert ok is False
        assert error == "RuntimeError: HTTP 522"
        assert mappings == {"prior": prior}

        async def recover(*_args, **_kwargs):
            return [OopzParticipant(oopz_uid="new", nickname="新映射")]

        monkeypatch.setattr(continuous, "_resolve_participants", recover)
        ok, error = await refresh_participants_safely(object(), request(), mappings)
        assert ok is True
        assert error is None
        assert set(mappings) == {"prior", "new"}

    asyncio.run(scenario())


def test_stop_command_is_accepted_while_reconnecting(tmp_path: Path) -> None:
    session_id = str(uuid4())
    session = tmp_path / session_id
    session.mkdir()
    write_json(session / "lifecycle.json", {
        "managed_by": "oopz-worker-v1",
        "mode": "continuous",
        "status": "reconnecting",
    })
    path = request_stop(tmp_path, session_id)
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == session_id


class FakeBackend:
    def __init__(self) -> None:
        self.join_count = 0

    async def get_status(self) -> dict:
        return {"currentAgoraUid": 12345}


class FakeVoice:
    def __init__(self) -> None:
        self.backend = FakeBackend()

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def join(self, **_kwargs):
        self.backend.join_count += 1
        return SimpleNamespace(rtc_channel_name="fake-room")

    async def leave(self) -> None:
        return None


class FakeBot:
    instances: list["FakeBot"] = []

    def __init__(self, _config) -> None:
        self.voice = FakeVoice()
        self.channels = object()
        self.person = object()
        self.__class__.instances.append(self)

    async def stop(self) -> None:
        return None


class FakeProbe:
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.failed_once = False

    async def install(self) -> None:
        return None

    async def start_audio_capture(self) -> None:
        return None

    async def stop_audio_capture(self) -> None:
        return None

    async def drain_audio(self, _max_chunks: int = 128) -> list[dict]:
        if self.backend.join_count == 1 and not self.failed_once:
            self.failed_once = True
            raise RuntimeError("simulated browser disconnect")
        return []

    async def snapshot(self) -> ProbeSnapshot:
        return ProbeSnapshot(connection_state="CONNECTED")


def test_disconnect_rejoins_into_same_session_directory(tmp_path: Path, monkeypatch) -> None:
    async def no_members(*_args, **_kwargs):
        raise RuntimeError("HTTP 522")

    FakeBot.instances.clear()
    monkeypatch.setattr("oopz_sdk.OopzBot", FakeBot)
    monkeypatch.setattr(continuous, "AgoraBrowserProbe", FakeProbe)
    monkeypatch.setattr(continuous, "_resolve_participants", no_members)
    session_id = str(uuid4())
    config = SimpleNamespace(person_uid="self")
    value = request(
        chunk_seconds=30,
        poll_interval_seconds=0.05,
        connection_check_seconds=0.5,
        disconnect_grace_seconds=3,
        browser_operation_timeout_seconds=0.5,
        reconnect_window_seconds=30,
        reconnect_initial_delay_seconds=0.25,
        reconnect_max_delay_seconds=1,
        reconnect_attempt_timeout_seconds=5,
        max_runtime_seconds=0.8,
    )
    session = asyncio.run(run_continuous_capture(
        config, value, output_root=tmp_path, session_id=session_id,
    ))

    assert session == tmp_path / session_id
    assert len([item for item in tmp_path.iterdir() if item.is_dir()]) == 1
    lifecycle = json.loads((session / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "ready_for_analysis"
    assert lifecycle["stop_reason"] == "diagnostic_max_runtime"
    assert lifecycle["failure"] is None
    assert lifecycle["reconnect_count"] == 1
    assert lifecycle["successful_connections"] == 2
    assert lifecycle["membership_refresh_failures"] >= 1
    assert lifecycle["chunks_total"] == 2
    chunks = sorted((session / "chunks").iterdir())
    assert len(chunks) == 2
    assert {
        json.loads((chunk / "chunk.json").read_text(encoding="utf-8"))["connection_episode"]
        for chunk in chunks
    } == {1, 2}
    events = [json.loads(line) for line in (session / "debug" / "connectivity_events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(item["event"] == "connection_lost" for item in events)
    assert sum(item["event"] == "connected" for item in events) == 2


def test_empty_channel_timeout_finishes_session(tmp_path: Path, monkeypatch) -> None:
    async def no_members(*_args, **_kwargs):
        return []

    class StableProbe(FakeProbe):
        async def drain_audio(self, _max_chunks: int = 128) -> list[dict]:
            return []

    FakeBot.instances.clear()
    monkeypatch.setattr("oopz_sdk.OopzBot", FakeBot)
    monkeypatch.setattr(continuous, "AgoraBrowserProbe", StableProbe)
    monkeypatch.setattr(continuous, "_resolve_participants", no_members)
    session_id = str(uuid4())
    config = SimpleNamespace(person_uid="self")
    value = request(
        chunk_seconds=30,
        poll_interval_seconds=0.05,
        connection_check_seconds=0.5,
        disconnect_grace_seconds=3,
        browser_operation_timeout_seconds=0.5,
        reconnect_window_seconds=30,
        reconnect_initial_delay_seconds=0.25,
        reconnect_max_delay_seconds=1,
        reconnect_attempt_timeout_seconds=5,
        empty_channel_timeout_seconds=5,
        max_runtime_seconds=20,
    )
    session = asyncio.run(run_continuous_capture(
        config, value, output_root=tmp_path, session_id=session_id,
    ))

    lifecycle = json.loads((session / "lifecycle.json").read_text(encoding="utf-8"))
    assert lifecycle["status"] == "ready_for_analysis"
    assert lifecycle["stop_reason"] == "empty_channel_timeout"
    assert lifecycle["failure"] is None
    assert lifecycle["empty_channel_timeout_seconds"] == 5
    events = [
        json.loads(line)
        for line in (session / "debug" / "connectivity_events.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert any(item["event"] == "empty_channel_started" for item in events)
    assert any(item["event"] == "empty_channel_timeout" for item in events)
