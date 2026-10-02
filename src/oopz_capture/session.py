from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .browser_probe import AgoraBrowserProbe, PROBE_VERSION
from .identity import build_identity_mappings
from .identifiers import new_session_id
from .models import IdentityMapping, OopzParticipant, ProbeSnapshot
from .output import write_probe_output
from .readable import identity_label


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeRunResult:
    session_dir: Path
    mappings: list[IdentityMapping]
    snapshot: ProbeSnapshot

    @property
    def all_verified(self) -> bool:
        return bool(self.mappings) and all(item.verified for item in self.mappings)


def _member_uid(member: Any) -> str:
    return str(getattr(member, "uid", "") or "").strip()


async def _resolve_participants(bot: Any, area: str, channel: str) -> list[OopzParticipant]:
    result = await bot.channels.get_voice_channel_members(area=area)
    members = list((result.channel_members or {}).get(channel, []))
    if not members:
        return []
    uids = [uid for member in members if (uid := _member_uid(member))]
    people_by_uid: dict[str, Any] = {}
    if uids:
        try:
            people = await bot.person.get_person_infos_batch(uids)
            people_by_uid.update({str(getattr(person, "uid", "") or ""): person for person in people if getattr(person, "uid", "")})
        except Exception:
            logger.warning("batch person lookup failed; retrying individually", exc_info=True)
            for uid in uids:
                try:
                    people_by_uid[uid] = await bot.person.get_person_info(uid)
                except Exception:
                    logger.warning("person lookup failed OOPZ UID=%s", uid)
    participants = []
    for member in members:
        uid = _member_uid(member)
        if not uid:
            continue
        person = people_by_uid.get(uid)
        participants.append(OopzParticipant(
            oopz_uid=uid,
            nickname=str(getattr(person, "name", "") or ""),
            pid=str(getattr(person, "pid", "") or ""),
            is_bot=bool(getattr(member, "is_bot", False)),
        ))
    return participants


async def _wait_for_participants(bot: Any, *, area: str, channel: str, wait_seconds: float) -> list[OopzParticipant]:
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
    participants = await _resolve_participants(bot, area, channel)
    while not participants and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(min(1.0, max(0.0, deadline - asyncio.get_running_loop().time())))
        participants = await _resolve_participants(bot, area, channel)
    return participants


async def _wait_for_snapshot(probe: AgoraBrowserProbe, *, expected_oopz_uids: set[str], wait_seconds: float) -> ProbeSnapshot:
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
    latest = await probe.snapshot()
    while asyncio.get_running_loop().time() < deadline:
        observed = {str(item.get("uid") or "") for item in latest.voice_states if isinstance(item, dict)}
        if expected_oopz_uids and expected_oopz_uids.issubset(observed):
            break
        await asyncio.sleep(min(1.0, max(0.0, deadline - asyncio.get_running_loop().time())))
        latest = await probe.snapshot()
    return latest


async def run_probe(config: Any, *, area: str, channel: str, wait_seconds: float, output_root: Path, rtc_uid: str | None = None) -> ProbeRunResult:
    from oopz_sdk import OopzBot
    session_id = new_session_id(output_root)
    started_at = datetime.now(timezone.utc)
    session_dir = output_root / session_id
    bot = OopzBot(config)
    joined = False
    probe = AgoraBrowserProbe(bot.voice.backend)
    try:
        await bot.voice.start()
        await probe.install()
        sign = await bot.voice.join(area=area, channel=channel, rtc_uid=rtc_uid)
        joined = True
        print("Connected to OOPZ voice channel for mapping verification.")
        participants = await _wait_for_participants(bot, area=area, channel=channel, wait_seconds=min(5.0, wait_seconds))
        print(f"Participants: {len(participants)}")
        backend_status = await bot.voice.backend.get_status()
        self_agora_uid = backend_status.get("currentAgoraUid")
        self_oopz_uid = str(getattr(config, "person_uid", "") or "")
        snapshot = await _wait_for_snapshot(
            probe,
            expected_oopz_uids={item.oopz_uid for item in participants if item.oopz_uid != self_oopz_uid},
            wait_seconds=wait_seconds,
        )
        mappings = build_identity_mappings(participants, snapshot, self_oopz_uid=self_oopz_uid, self_agora_uid=self_agora_uid)
        for mapping in mappings:
            print(f"{identity_label(nickname=mapping.nickname, oopz_uid=mapping.oopz_uid, agora_uid=mapping.agora_uid)} | status={mapping.status}")
        finished_at = datetime.now(timezone.utc)
        session = {
            "session_id": session_id,
            "milestones": [1, 2, 3],
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "area": area,
            "channel": channel,
            "agora_room_id": str(getattr(sign, "rtc_channel_name", "") or ""),
            "connected": True,
            "connection_state": snapshot.connection_state,
            "participant_count": len(participants),
            "all_mappings_verified": bool(mappings) and all(item.verified for item in mappings),
            "probe_version": PROBE_VERSION,
        }
        write_probe_output(session_dir, session, mappings, snapshot)
        return ProbeRunResult(session_dir, mappings, snapshot)
    finally:
        if joined:
            try:
                await bot.voice.leave()
            except Exception:
                logger.warning("failed to leave OOPZ voice channel cleanly", exc_info=True)
        try:
            await bot.stop()
        except Exception:
            logger.debug("bot.stop failed during cleanup", exc_info=True)
