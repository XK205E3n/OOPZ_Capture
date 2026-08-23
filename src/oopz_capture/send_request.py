"""Durable outbound requests produced by the controller for the Feishu gateway."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .workflow import _is_reparse_point


SEND_REQUEST_SCHEMA = "oopz.controller.send_request.v1"
DEFAULT_MAX_ATTEMPTS = 8
_RETRY_DELAYS_SECONDS = (5, 15, 30, 60, 120, 300, 600)
_enqueue_lock = threading.Lock()
_last_enqueue_order_ns = 0


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _next_enqueue_order_ns() -> int:
    """Return a process-local strictly increasing order for adjacent sends."""
    global _last_enqueue_order_ns
    with _enqueue_lock:
        _last_enqueue_order_ns = max(time.time_ns(), _last_enqueue_order_ns + 1)
        return _last_enqueue_order_ns


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _request_path(state_root: Path, request_id: str) -> Path:
    try:
        UUID(request_id)
    except ValueError as error:
        raise ValueError("send_request_id must be a UUID") from error
    root = (state_root / "send_requests").resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = (root / f"{request_id}.json").resolve()
    if path.parent != root:
        raise ValueError("unsafe send request path")
    return path


def enqueue_send_request(
    state_root: Path,
    *,
    target_type: str,
    target_id: str,
    text: str,
    source: str,
    file_path: str | None = None,
    notify_admin_id: str | None = None,
) -> dict[str, Any]:
    """Queue one outbound message (already split by the caller)."""
    if target_type not in {"private", "group"}:
        raise ValueError("target_type must be private or group")
    target_id = str(target_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", target_id):
        raise ValueError("target_id must contain 1 to 256 safe identifier characters")
    file_path_value = str(file_path or "").strip() or None
    if file_path_value is not None and len(file_path_value) > 2048:
        raise ValueError("file_path is too long")
    text = str(text or "").strip()
    if (not text and not file_path_value) or len(text) > 8000:
        raise ValueError("text must contain 1 to 8000 characters or a file_path")
    notify_admin_value = str(notify_admin_id or "").strip() or None
    if notify_admin_value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", notify_admin_value):
        raise ValueError("notify_admin_id must contain 1 to 256 safe identifier characters")
    request_id = str(uuid4())
    record: dict[str, Any] = {
        "schema_version": SEND_REQUEST_SCHEMA,
        "send_request_id": request_id,
        "target_type": target_type,
        "target_id": target_id,
        "text": text,
        "file_path": file_path_value,
        "notify_admin_id": notify_admin_value,
        "source": str(source or "")[:100],
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
        "next_attempt_at": _iso(),
        "enqueue_order_ns": _next_enqueue_order_ns(),
        "created_at": _iso(),
        "updated_at": _iso(),
        "sent_at": None,
        "last_error": None,
    }
    _atomic_json(_request_path(state_root, request_id), record)
    return record


def list_send_requests(state_root: Path, *, statuses: set[str] | None = None) -> list[dict[str, Any]]:
    root = state_root / "send_requests"
    if not root.exists():
        return []
    if not root.is_dir() or _is_reparse_point(root):
        raise ValueError("unsafe send requests directory")
    values: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        if not path.is_file() or _is_reparse_point(path):
            raise ValueError(f"unsafe send request entry: {path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        if statuses is None or value.get("status") in statuses:
            values.append(value)
    # File names are UUIDs and timestamp resolution can be too coarse for a
    # text+PDF pair. Always preserve controller enqueue order so recipients see
    # the explanatory summary before its attachment; due-time checks happen in
    # the gateway immediately before delivery.
    values.sort(key=lambda item: (
        int(item.get("enqueue_order_ns", 0) or 0),
        str(item.get("created_at") or ""),
        str(item.get("send_request_id") or ""),
    ))
    return values


def acknowledge_send_request(
    state_root: Path,
    request_id: str,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    if status not in {"sent", "failed"}:
        raise ValueError("send request status must be sent or failed")
    path = _request_path(state_root, request_id)
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"send request not found: {request_id}")
    value = json.loads(path.read_text(encoding="utf-8"))
    value["status"] = status
    value["updated_at"] = _iso()
    value["sent_at"] = _iso() if status == "sent" else None
    value["last_error"] = (error or "delivery failed")[:1000] if status == "failed" else None
    _atomic_json(path, value)
    return value


def send_request_is_due(value: dict[str, Any], *, now: datetime | None = None) -> bool:
    if value.get("status") != "pending":
        return False
    raw = str(value.get("next_attempt_at") or value.get("created_at") or "")
    try:
        due = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= (now or datetime.now(timezone.utc))


def reschedule_send_request(
    state_root: Path,
    request_id: str,
    *,
    error: str,
    retry_indefinitely: bool = False,
) -> dict[str, Any]:
    """Persist a bounded retry instead of losing a notification on a transient delivery error."""
    path = _request_path(state_root, request_id)
    if not path.is_file() or _is_reparse_point(path):
        raise ValueError(f"send request not found: {request_id}")
    value = json.loads(path.read_text(encoding="utf-8"))
    attempt_count = int(value.get("attempt_count", 0) or 0) + 1
    max_attempts = int(value.get("max_attempts", DEFAULT_MAX_ATTEMPTS) or DEFAULT_MAX_ATTEMPTS)
    now = datetime.now(timezone.utc)
    value["attempt_count"] = attempt_count
    value["max_attempts"] = max_attempts
    value["updated_at"] = now.isoformat(timespec="milliseconds")
    value["last_error"] = str(error or "delivery failed")[:1000]
    if attempt_count >= max_attempts and not retry_indefinitely:
        value["status"] = "failed"
        value["next_attempt_at"] = None
    else:
        delay = _RETRY_DELAYS_SECONDS[min(attempt_count - 1, len(_RETRY_DELAYS_SECONDS) - 1)]
        value["status"] = "pending"
        value["next_attempt_at"] = (now + timedelta(seconds=delay)).isoformat(timespec="milliseconds")
    _atomic_json(path, value)
    return value


def expedite_pending_send_requests(state_root: Path) -> int:
    """Make retained transient notifications eligible immediately after recovery."""
    changed = 0
    for value in list_send_requests(state_root, statuses={"pending"}):
        request_id = str(value.get("send_request_id") or "")
        if not request_id:
            continue
        path = _request_path(state_root, request_id)
        value["next_attempt_at"] = _iso()
        value["updated_at"] = _iso()
        _atomic_json(path, value)
        changed += 1
    return changed
