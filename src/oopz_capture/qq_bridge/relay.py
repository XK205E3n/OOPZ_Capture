"""Client for MaiBot's local image-relay endpoint (plugin ling_relay): POST /v1/send.

This is the QQ bridge, a module outside the recorder, the analyzer and the Feishu gateway: it only ever
reads a finished digest image and hands it to MaiBot.  Nothing here knows about OOPZ or Feishu.

Retry rules follow the endpoint's contract: ``sent`` is final; ``failed`` (nothing went out) may be
retried with the same request id; ``unknown`` and ``partial`` must not be retried; 429 is retried after
``retry_after`` seconds; every other refusal means the request itself or the setup is wrong.
"""
from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

ATTEMPTS = 4                 # sends of one request id, whatever the reason for trying again
RETRY_WAIT = (5.0, 15.0, 30.0)
MAX_RETRY_AFTER = 120.0
TIMEOUT = 30.0
DEFAULT_URL = "http://127.0.0.1:18764"


class BridgeConfigError(ValueError):
    """The bridge is switched on but its settings are unusable."""


@dataclass(frozen=True)
class BridgeConfig:
    url: str
    token: str
    target_type: str          # "group" or "private"
    target_id: str

    def __repr__(self) -> str:        # the token must never reach a log through a stray repr
        return f"BridgeConfig(url={self.url!r}, target={self.target_type}:{self.target_id}, token=***)"

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "BridgeConfig | None":
        """None when the bridge is off (no group set).  A group without a token, or a URL that is not a
        loopback address, is an error: the token must only ever travel to this machine."""
        group = str(env.get("OOPZ_QQ_GROUP_ID") or "").strip()
        if not group:
            return None
        token = str(env.get("OOPZ_QQ_RELAY_TOKEN") or "").strip()
        if not token:
            raise BridgeConfigError("OOPZ_QQ_RELAY_TOKEN is not set")
        if not re.fullmatch(r"\d{4,15}", group):
            raise BridgeConfigError("OOPZ_QQ_GROUP_ID must be a QQ group number")
        url = str(env.get("OOPZ_QQ_RELAY_URL") or DEFAULT_URL).strip().rstrip("/")
        parts = urlsplit(url)
        host = parts.hostname or ""
        try:
            local = ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if parts.scheme != "http" or not local or parts.path not in ("", "/"):
            raise BridgeConfigError("OOPZ_QQ_RELAY_URL must be http://127.0.0.1:<port> (the token only goes to this machine)")
        return cls(url=url, token=token, target_type="group", target_id=group)


def request_id_for(session_id: str, image: Path) -> str:
    """Stable for one image (a retry is recognised as the same request); a re-rendered image is a new one."""
    digest = hashlib.sha256(Path(image).read_bytes()).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", session_id)
    return f"oopz-{safe}-{digest}"[:80]


Post = Callable[[str, dict, dict, float], "tuple[int, dict]"]


def _post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST",
                                     headers={**headers, "Content-Type": "application/json", "Content-Length": str(len(data))})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        try:
            payload = json.loads(error.read().decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        return error.code, payload if isinstance(payload, dict) else {}


def send_image(config: BridgeConfig, image: Path, request_id: str, *, post: Post = _post,
               sleep: Callable[[float], None] = time.sleep) -> dict:
    """Send one image; returns {"status", "attempts", "request_id", "http", "error", "duplicate"}.
    status is sent / failed / unknown / partial / rejected.  Never raises for transport or HTTP problems."""
    body = {"request_id": request_id, "target": {"type": config.target_type, "id": config.target_id},
            "image_path": str(Path(image).resolve())}
    headers = {"Authorization": f"Bearer {config.token}"}
    outcome: dict = {"status": "failed", "attempts": 0, "request_id": request_id, "http": None, "error": "", "duplicate": False}
    for attempt in range(1, ATTEMPTS + 1):
        outcome["attempts"] = attempt
        wait = RETRY_WAIT[min(attempt - 1, len(RETRY_WAIT) - 1)]
        try:
            code, payload = post(config.url + "/v1/send", headers, body, TIMEOUT)
        except (ConnectionRefusedError, ConnectionResetError) as error:        # nothing reached the endpoint
            outcome |= {"status": "failed", "http": None, "error": f"connection:{type(error).__name__}"}
        except urllib.error.URLError as error:
            reason = error.reason
            if isinstance(reason, (ConnectionRefusedError, ConnectionResetError)) or (
                    isinstance(reason, OSError) and not isinstance(reason, (socket.timeout, TimeoutError))):
                outcome |= {"status": "failed", "http": None, "error": f"connection:{type(reason).__name__}"}
            else:      # a timeout: the request may have been processed
                return outcome | {"status": "unknown", "http": None, "error": "timeout"}
        except (TimeoutError, socket.timeout):
            return outcome | {"status": "unknown", "http": None, "error": "timeout"}
        except OSError as error:
            outcome |= {"status": "failed", "http": None, "error": f"connection:{type(error).__name__}"}
        else:
            error_code = str(payload.get("error") or "")
            outcome |= {"http": code, "error": error_code, "duplicate": bool(payload.get("duplicate"))}
            if code == 200:
                status = str(payload.get("status") or "unknown")
                outcome["status"] = status if status in {"sent", "failed", "unknown", "partial"} else "unknown"
                if outcome["status"] in {"sent", "unknown", "partial"}:
                    outcome["error"] = str(payload.get("error") or "")
                    return outcome
                outcome["error"] = str(payload.get("error") or "send failed")
            elif code == 429:
                outcome["status"] = "failed"
                try:
                    wait = min(MAX_RETRY_AFTER, max(1.0, float(payload.get("retry_after"))))
                except (TypeError, ValueError):
                    wait = 30.0
            else:
                return outcome | {"status": "rejected"}
        if attempt < ATTEMPTS:
            sleep(wait)
    return outcome
