from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_probe import AgoraBrowserProbe, PROBE_VERSION
from .identity import build_identity_mappings
from .identifiers import new_session_id, validate_session_id
from .output import write_json, write_jsonl
from .readable import identity_label, readable_nickname
from .recorder import CaptureRecorder
from .session import _resolve_participants


logger = logging.getLogger(__name__)


async def run_capture(
    config: Any,
    *,
    area: str,
    channel: str,
    duration_seconds: float,
    poll_interval: float,
    output_root: Path,
    rtc_uid: str | None = None,
    session_id: str | None = None,
) -> Path:
    from oopz_sdk import OopzBot

    session_id = validate_session_id(session_id) if session_id else new_session_id(output_root)
    session_dir = output_root / session_id
    started_at = datetime.now(timezone.utc)
    bot = OopzBot(config)
    probe = AgoraBrowserProbe(bot.voice.backend)
    recorder = CaptureRecorder(session_dir)
    joined = False
    capture_started = False
    participants_by_uid: dict[str, Any] = {}
    sign: Any = None
    snapshot = None
    manifest: list[dict[str, Any]] = []
    failure: BaseException | None = None

    try:
        await bot.voice.start()
        await probe.install()
        await probe.start_audio_capture()
        capture_started = True
        sign = await bot.voice.join(area=area, channel=channel, rtc_uid=rtc_uid)
        joined = True
        print("Capture bot connected and will remain until the requested duration ends.")

        loop = asyncio.get_running_loop()
        deadline = None if duration_seconds == 0 else loop.time() + duration_seconds
        next_membership_refresh = 0.0
        while deadline is None or loop.time() < deadline:
            for chunk in await probe.drain_audio():
                recorder.ingest(chunk)
            if loop.time() >= next_membership_refresh:
                for participant in await _resolve_participants(bot, area, channel):
                    participants_by_uid[participant.oopz_uid] = participant
                next_membership_refresh = loop.time() + 3.0
            sleep_for = poll_interval
            if deadline is not None:
                sleep_for = min(sleep_for, max(0.0, deadline - loop.time()))
            if sleep_for:
                await asyncio.sleep(sleep_for)
    except BaseException as error:
        failure = error
    finally:
        if capture_started:
            try:
                await probe.stop_audio_capture()
                while chunks := await probe.drain_audio():
                    for chunk in chunks:
                        recorder.ingest(chunk)
            except Exception:
                logger.warning("failed to stop/drain browser audio capture", exc_info=True)
        manifest = recorder.close()
        try:
            snapshot = await probe.snapshot()
        except Exception:
            logger.warning("failed to take final capture snapshot", exc_info=True)
        if joined:
            try:
                await bot.voice.leave()
            except Exception:
                logger.warning("failed to leave OOPZ voice channel cleanly", exc_info=True)
        try:
            await bot.stop()
        except Exception:
            logger.debug("bot.stop failed during cleanup", exc_info=True)

    if snapshot is None:
        from .models import ProbeSnapshot
        snapshot = ProbeSnapshot()
    participants = list(participants_by_uid.values())
    backend_status = {}
    try:
        backend_status = await bot.voice.backend.get_status()
    except Exception:
        pass
    mappings = build_identity_mappings(
        participants,
        snapshot,
        self_oopz_uid=str(getattr(config, "person_uid", "") or ""),
        self_agora_uid=backend_status.get("currentAgoraUid"),
    )
    finished_at = datetime.now(timezone.utc)
    session = {
        "session_id": session_id,
        "milestones_implemented": [4, 5, 6],
        "milestones_accepted": [],
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "area": area,
        "channel": channel,
        "agora_room_id": str(getattr(sign, "rtc_channel_name", "") or ""),
        "connection_state_at_stop": snapshot.connection_state,
        "probe_version": PROBE_VERSION,
        "track_count": len(manifest),
        "clean_finish": failure is None,
        "failure_type": type(failure).__name__ if failure else None,
    }
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "users.json", [item.to_dict() for item in mappings])
    write_json(session_dir / "audio_manifest.json", manifest)
    write_jsonl(session_dir / "debug" / "agora_events.jsonl", snapshot.events)
    write_jsonl(session_dir / "debug" / "uid_mapping.jsonl", [item.to_dict() for item in mappings])

    mappings_by_agora = {str(item.agora_uid): item for item in mappings if item.agora_uid is not None}
    lines = [
        "# OOPZ capture summary", "",
        f"Session ID: {session_id}",
        f"Started: {started_at.isoformat()}",
        f"Finished: {finished_at.isoformat()}",
        f"Captured tracks: {len(manifest)}", "", "## Identity-labelled tracks", "",
    ]
    if manifest:
        for track in manifest:
            uid = str(track["agora_uid"])
            mapping = mappings_by_agora.get(uid)
            label = identity_label(
                nickname=mapping.nickname if mapping else "",
                oopz_uid=mapping.oopz_uid if mapping else "",
                agora_uid=int(uid),
            )
            lines.append(
                f"- {label}; file=audio/{uid}.wav; captured={track['captured_seconds']:.2f}s; aligned_duration={track['duration_seconds']:.2f}s"
            )
    else:
        lines.append("- No remote audio track was captured.")
    lines.extend(["", "## Mapping observations", ""])
    for mapping in mappings:
        lines.append(
            f"- {identity_label(nickname=mapping.nickname, oopz_uid=mapping.oopz_uid, agora_uid=mapping.agora_uid)}; status={mapping.status}"
        )
    lines.extend(["", "## Acceptance status", "", "Implementation is present, but Milestones 4-6 require a real-room manual listening test before acceptance.", ""])
    (session_dir / "capture_summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"Capture completed. Session ID={session_id}")
    for mapping in mappings:
        print(identity_label(nickname=mapping.nickname, oopz_uid=mapping.oopz_uid, agora_uid=mapping.agora_uid))
    if failure is not None:
        raise failure
    return session_dir
