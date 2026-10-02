from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .models import IdentityMapping, OopzParticipant, ProbeSnapshot


def _numeric_uid(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text.isdecimal():
        return None
    number = int(text)
    return number if 0 <= number <= 0xFFFFFFFF else None


def build_identity_mappings(
    participants: Iterable[OopzParticipant],
    snapshot: ProbeSnapshot,
    *,
    self_oopz_uid: str = "",
    self_agora_uid: Any = None,
) -> list[IdentityMapping]:
    """Merge OOPZ membership/person data with observations from Agora."""

    remote_uids = {
        uid
        for item in snapshot.remote_users
        if (uid := _numeric_uid(item.get("uid") if isinstance(item, dict) else None))
        is not None
    }

    states_by_oopz: dict[str, int] = {}
    for item in snapshot.voice_states:
        if not isinstance(item, dict):
            continue
        oopz_uid = str(item.get("uid") or "").strip()
        agora_uid = _numeric_uid(item.get("cid"))
        if oopz_uid and agora_uid is not None:
            states_by_oopz[oopz_uid] = agora_uid

    normalized_self_uid = str(self_oopz_uid or "").strip()
    normalized_self_agora = _numeric_uid(self_agora_uid)
    mappings: list[IdentityMapping] = []
    for participant in participants:
        evidence: list[str] = []
        pid_uid = _numeric_uid(participant.pid)
        observed_uid = states_by_oopz.get(participant.oopz_uid)

        if observed_uid is not None:
            agora_uid = observed_uid
            evidence.append("OOPZ data_stream contained matching uid/cid")
            if observed_uid in remote_uids or participant.oopz_uid == normalized_self_uid:
                evidence.append("CID is present in the Agora room")
                status = "verified_data_stream"
            else:
                status = "inferred_person_pid"
                evidence.append("CID was not present in the current Agora user snapshot")
            if pid_uid is not None and pid_uid != observed_uid:
                evidence.append(f"WARNING: Person PID {pid_uid} differs from observed CID")
        elif (
            participant.oopz_uid == normalized_self_uid
            and normalized_self_agora is not None
        ):
            agora_uid = normalized_self_agora
            evidence.append("Local SDK browser transport reported this joined Agora UID")
            if pid_uid is not None and pid_uid != normalized_self_agora:
                status = "inferred_person_pid"
                evidence.append(
                    f"WARNING: Person PID {pid_uid} differs from local joined CID"
                )
            else:
                status = "verified_local_join"
                if pid_uid is not None:
                    evidence.append("Person PID matches the local joined Agora UID")
        elif pid_uid is not None:
            agora_uid = pid_uid
            evidence.append("Oopzbot-SDK uses Person PID as rtc_uid")
            if pid_uid in remote_uids or (
                participant.oopz_uid == normalized_self_uid
                and pid_uid == normalized_self_agora
            ):
                status = "verified_remote_user_pid"
                evidence.append("Person PID is present as an Agora UID")
            else:
                status = "inferred_person_pid"
                evidence.append("Person PID has not yet appeared in the Agora user snapshot")
        else:
            agora_uid = None
            status = "unresolved"
            evidence.append("No numeric Person PID or data_stream mapping was observed")

        mappings.append(
            IdentityMapping(
                oopz_uid=participant.oopz_uid,
                nickname=participant.nickname,
                agora_uid=agora_uid,
                oopz_pid=participant.pid,
                is_bot=participant.is_bot,
                status=status,
                evidence=evidence,
            )
        )

    return sorted(
        mappings,
        key=lambda item: (
            item.agora_uid is None,
            item.agora_uid if item.agora_uid is not None else 0,
            item.oopz_uid,
        ),
    )
