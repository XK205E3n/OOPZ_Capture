from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


VERIFIED_SOURCES = {
    "verified_data_stream",
    "verified_remote_user_pid",
    "verified_local_join",
}


@dataclass(slots=True)
class OopzParticipant:
    oopz_uid: str
    nickname: str = ""
    pid: str = ""
    is_bot: bool = False


@dataclass(slots=True)
class IdentityMapping:
    oopz_uid: str
    nickname: str
    agora_uid: int | None
    oopz_pid: str
    is_bot: bool
    status: str
    evidence: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status in VERIFIED_SOURCES

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["verified"] = self.verified
        return value


@dataclass(slots=True)
class ProbeSnapshot:
    remote_users: list[dict[str, Any]] = field(default_factory=list)
    voice_states: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    connection_state: str = "unknown"

    @classmethod
    def from_browser(cls, value: Any) -> "ProbeSnapshot":
        if not isinstance(value, dict):
            return cls()
        return cls(
            remote_users=list(value.get("remoteUsers") or []),
            voice_states=list(value.get("voiceStates") or []),
            events=list(value.get("events") or []),
            connection_state=str(value.get("connectionState") or "unknown"),
        )
