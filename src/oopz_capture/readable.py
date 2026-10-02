from __future__ import annotations


_MOJIBAKE_MARKERS = frozenset(
    "\u951f\u65a4\u62f7\u93c4\u71b7\u6473\u7ee9\u69fc\u4e2d\u6b91\u95c3\u8dfa"
)
_UNKNOWN_NICKNAME = "nickname-unavailable"


def readable_nickname(value: str) -> str:
    """Return a safe label; never guess a damaged human-readable nickname."""

    raw = str(value or "").strip()
    if not raw or "\ufffd" in raw:
        return _UNKNOWN_NICKNAME
    if any(marker in raw for marker in _MOJIBAKE_MARKERS):
        return _UNKNOWN_NICKNAME
    return raw


def identity_label(*, nickname: str, oopz_uid: str, agora_uid: int | None) -> str:
    agora = str(agora_uid) if agora_uid is not None else "UNRESOLVED"
    return (
        f"nickname={readable_nickname(nickname)} | "
        f"OOPZ UID={oopz_uid or 'UNRESOLVED'} | Agora UID={agora}"
    )
