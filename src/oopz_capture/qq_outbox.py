from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from .workflow import _is_reparse_point
from .identifiers import validate_session_id


OUTBOX_SCHEMA = "oopz.qq.outbox.v1"
_RETRY_DELAYS_SECONDS = (5, 15, 30, 60, 120, 300, 600)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _message_path(state_root: Path, message_id: str) -> Path:
    try:
        UUID(message_id)
    except ValueError as error:
        raise ValueError("message_id must be a UUID") from error
    root = (state_root / "outbox").resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{message_id}.json").resolve()
    if path.parent != root:
        raise ValueError("unsafe outbox path")
    return path


def enqueue_session_messages(session_dir: Path, state_root: Path) -> list[dict[str, Any]]:
    cleanup_outbox(state_root)
    path = session_dir / "handoff" / "qq_messages.jsonl"
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"QQ message handoff is missing or unsafe: {path}")
    now = datetime.now(timezone.utc)
    maximum_expiry = now + timedelta(hours=168)
    analyzer_request = session_dir / "handoff" / "analyzer_request.json"
    expiry = maximum_expiry
    if analyzer_request.is_file() and not _is_reparse_point(analyzer_request):
        request = json.loads(analyzer_request.read_text(encoding="utf-8"))
        declared = ((request.get("retention") or {}).get("delete_after")) if isinstance(request, dict) else None
        if declared:
            expiry = min(_parse_time(str(declared)), maximum_expiry)
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        message = json.loads(line)
        if not isinstance(message, dict) or message.get("schema_version") != "oopz.qq.message.v1":
            raise ValueError(f"invalid QQ message at line {line_number}")
        message_id = str(message.get("message_id") or "")
        for field in ("message_id", "report_id"):
            try:
                UUID(str(message.get(field) or ""))
            except ValueError as error:
                raise ValueError(f"QQ message {field} must be a UUID at line {line_number}") from error
        validate_session_id(message.get("session_id"), f"QQ message session_id at line {line_number}")
        output = _message_path(state_root, message_id)
        status = "blocked" if message.get("delivery_status") == "target_required" else "pending"
        record = {
            "schema_version": OUTBOX_SCHEMA,
            "message_id": message_id,
            "session_id": str(message.get("session_id") or ""),
            "report_id": str(message.get("report_id") or ""),
            "status": status,
            "attempt_count": 0,
            "next_attempt_at": _iso(),
            "queued_at": _iso(),
            "expires_at": expiry.isoformat(timespec="milliseconds"),
            "updated_at": _iso(),
            "last_error": None,
            "message": message,
        }
        if output.is_file():
            existing = json.loads(output.read_text(encoding="utf-8"))
            if existing.get("message") != message:
                raise ValueError(f"outbox message_id collision: {message_id}")
            records.append(existing)
            continue
        _atomic_json(output, record)
        records.append(record)
    return records


def cleanup_outbox(state_root: Path, *, now: datetime | None = None) -> list[str]:
    root = state_root / "outbox"
    if not root.exists():
        return []
    if not root.is_dir() or _is_reparse_point(root):
        raise ValueError("unsafe outbox directory")
    threshold = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expired: list[Path] = []
    for path in sorted(root.glob("*.json")):
        if not path.is_file() or _is_reparse_point(path):
            raise ValueError(f"unsafe outbox entry: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        expires_at = _parse_time(str(value.get("expires_at") or ""))
        if expires_at <= threshold:
            expired.append(path)
    for path in expired:
        path.unlink()
    return [path.stem for path in expired]


def list_outbox(state_root: Path, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    root = state_root / "outbox"
    if not root.exists():
        return []
    if not root.is_dir() or _is_reparse_point(root):
        raise ValueError("unsafe outbox directory")
    values: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        if not path.is_file() or _is_reparse_point(path):
            raise ValueError(f"unsafe outbox entry: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if statuses is None or value.get("status") in statuses:
            values.append(value)
    return values


def acknowledge_delivery(
    state_root: Path,
    message_id: str,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in {"sent", "failed"}:
        raise ValueError("delivery status must be sent or failed")
    path = _message_path(state_root, message_id)
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"outbox message not found: {message_id}")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["status"] = status
    value["attempt_count"] = int(value.get("attempt_count", 0)) + 1
    value["updated_at"] = _iso()
    value["delivered_at"] = _iso() if status == "sent" else None
    value["last_error"] = (error or "delivery failed")[:1000] if status == "failed" else None
    _atomic_json(path, value)
    return value


def delivery_is_due(value: dict[str, Any], *, now: datetime | None = None) -> bool:
    if value.get("status") != "pending":
        return False
    raw = str(value.get("next_attempt_at") or value.get("queued_at") or "")
    try:
        due = _parse_time(raw)
    except ValueError:
        return True
    return due <= (now or datetime.now(timezone.utc))


def reschedule_delivery(state_root: Path, message_id: str, *, error: str) -> dict[str, Any]:
    """Keep report delivery pending through a transient NapCat/QQ outage."""
    path = _message_path(state_root, message_id)
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"outbox message not found: {message_id}")
    value = json.loads(path.read_text(encoding="utf-8"))
    attempts = int(value.get("attempt_count", 0) or 0) + 1
    delay = _RETRY_DELAYS_SECONDS[min(attempts - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
    value.update({
        "status": "pending",
        "attempt_count": attempts,
        "updated_at": _iso(),
        "next_attempt_at": (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="milliseconds"),
        "last_error": str(error or "delivery failed")[:1000],
    })
    _atomic_json(path, value)
    return value


def expedite_pending_deliveries(state_root: Path) -> int:
    changed = 0
    for value in list_outbox(state_root, statuses={"pending"}):
        message_id = str(value.get("message_id") or "")
        if not message_id:
            continue
        path = _message_path(state_root, message_id)
        value["next_attempt_at"] = _iso()
        value["updated_at"] = _iso()
        _atomic_json(path, value)
        changed += 1
    return changed
