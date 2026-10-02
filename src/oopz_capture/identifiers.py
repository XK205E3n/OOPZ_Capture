from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


BEIJING_TIMEZONE = timezone(timedelta(hours=8))
READABLE_SESSION_ID_PATTERN = r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_BJT(?:-\d{2,3})?$"
_READABLE_SESSION_ID = re.compile(READABLE_SESSION_ID_PATTERN)
_READABLE_TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S_BJT"


def is_session_id(value: Any) -> bool:
    text = str(value or "")
    try:
        UUID(text)
        return True
    except ValueError:
        pass
    if not _READABLE_SESSION_ID.fullmatch(text):
        return False
    timestamp = text[:23]
    try:
        datetime.strptime(timestamp, _READABLE_TIMESTAMP_FORMAT)
    except ValueError:
        return False
    suffix = text[23:]
    return not suffix or int(suffix[1:]) >= 2


def validate_session_id(value: Any, field: str = "session_id") -> str:
    text = str(value or "")
    if not is_session_id(text):
        raise ValueError(
            f"{field} must be a legacy UUID or Beijing-time ID such as "
            "2026-08-13_22-15-30_BJT"
        )
    return text


def new_session_id(output_root: Path, *, started_at: datetime | None = None) -> str:
    moment = started_at or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        raise ValueError("started_at must include a timezone")
    base = moment.astimezone(BEIJING_TIMEZONE).strftime(_READABLE_TIMESTAMP_FORMAT)
    root = output_root.resolve()
    for sequence in range(1, 1000):
        candidate = base if sequence == 1 else f"{base}-{sequence:02d}"
        if not (root / candidate).exists():
            return candidate
    raise FileExistsError(f"too many Sessions started during {base}")
