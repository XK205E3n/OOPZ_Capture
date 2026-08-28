from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import IdentityMapping, ProbeSnapshot


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def write_jsonl(path: Path, values: Iterable[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for value in values:
            stream.write(json.dumps(value, ensure_ascii=False, default=_json_default))
            stream.write("\n")


def write_probe_output(
    session_dir: Path,
    session: dict[str, Any],
    mappings: list[IdentityMapping],
    snapshot: ProbeSnapshot,
) -> None:
    write_json(session_dir / "session.json", session)
    write_json(session_dir / "users.json", [item.to_dict() for item in mappings])
    write_jsonl(session_dir / "debug" / "agora_events.jsonl", snapshot.events)
    write_jsonl(
        session_dir / "debug" / "uid_mapping.jsonl",
        [item.to_dict() for item in mappings],
    )

