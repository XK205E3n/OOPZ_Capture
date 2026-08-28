"""Shared JSON file and timestamp helpers.

These helpers previously existed as byte-identical private copies in up to five
modules at once.  They live here so a fix to atomicity or formatting lands once.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def iso_utc(value: datetime | None = None) -> str:
    """UTC ISO-8601 with millisecond precision; ``None`` means now."""
    moment = value or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def atomic_json(path: Path, value: Any) -> None:
    """Write JSON through a pid-suffixed sibling, then atomically replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_or_none(path: Path) -> dict[str, Any] | None:
    """Tolerant read: any missing file or malformed payload yields ``None``."""
    try:
        value = read_json(path)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None
