from __future__ import annotations

import logging
from typing import Any

from .models import OopzParticipant


logger = logging.getLogger(__name__)


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
            avatar_url=str(getattr(person, "avatar", "") or ""),
        ))
    return participants
