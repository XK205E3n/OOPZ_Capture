"""QQ-only recovery supervisor.

This process deliberately has no knowledge of OOPZ capture, transcription, or
analysis process identifiers.  Its only mutation targets are the OneBot gateway
and the isolated NapCat installation, so a QQ recovery can never stop an active
recording job.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


LOGGER = logging.getLogger("oopz_capture.qq_watchdog")
SCHEMA = "oopz.qq.watchdog.v1"
TRANSIENT_MARKERS = (
    "retcode=1200", "网络连接异常", "serviceandmethod", "sendmsg",
    "timeout", "connection", "websocket", "winerror 1225",
)


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _port_open(host: str = "127.0.0.1", port: int = 3001) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _transient_pending_failure(state_root: Path) -> tuple[int, str]:
    root = state_root / "send_requests"
    if not root.is_dir():
        return 0, ""
    maximum = 0
    detail = ""
    for path in root.glob("*.json"):
        value = _read_json(path)
        if value.get("status") != "pending":
            continue
        error = str(value.get("last_error") or "")
        if not any(marker in error.casefold() for marker in TRANSIENT_MARKERS):
            continue
        attempt = int(value.get("attempt_count", 0) or 0)
        if attempt >= maximum:
            maximum = attempt
            detail = error[:500]
    return maximum, detail


@dataclass
class RecoveryState:
    schema_version: str = SCHEMA
    stage: str = "healthy"
    issue_started_at: float = 0.0
    stage_started_at: float = 0.0
    observed_attempt: int = 0
    last_action: str = ""
    last_action_at: str = ""
    healthy_polls: int = 0
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> "RecoveryState":
        value = _read_json(path)
        if value.get("schema_version") != SCHEMA:
            return cls()
        fields = cls.__dataclass_fields__
        return cls(**{key: value[key] for key in fields if key in value})

    def save(self, path: Path) -> None:
        self.updated_at = _iso()
        _atomic_json(path, asdict(self))


@dataclass(frozen=True)
class WatchdogConfig:
    project_root: Path
    state_root: Path
    poll_seconds: float = 10.0
    port_grace_seconds: float = 60.0
    gateway_grace_seconds: float = 45.0
    escalation_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        project_root = Path(__file__).resolve().parents[2]
        return cls(
            project_root=project_root,
            state_root=Path(os.environ.get("OOPZ_QQ_STATE_ROOT", "controller_state")).resolve(),
            poll_seconds=float(os.environ.get("OOPZ_QQ_WATCHDOG_POLL_SECONDS", "10")),
            port_grace_seconds=float(os.environ.get("OOPZ_QQ_WATCHDOG_PORT_GRACE_SECONDS", "60")),
            gateway_grace_seconds=float(os.environ.get("OOPZ_QQ_WATCHDOG_GATEWAY_GRACE_SECONDS", "45")),
            escalation_seconds=float(os.environ.get("OOPZ_QQ_WATCHDOG_ESCALATION_SECONDS", "120")),
        )

    def validate(self) -> None:
        if not 2 <= self.poll_seconds <= 60:
            raise ValueError("OOPZ_QQ_WATCHDOG_POLL_SECONDS must be 2 to 60")
        for name, value in (
            ("port grace", self.port_grace_seconds),
            ("gateway grace", self.gateway_grace_seconds),
            ("escalation", self.escalation_seconds),
        ):
            if not 10 <= value <= 3600:
                raise ValueError(f"QQ watchdog {name} must be 10 to 3600 seconds")
        expected = (self.project_root / "controller_state").resolve()
        if self.state_root != expected:
            raise ValueError("QQ watchdog state root must be the project controller_state directory")


def choose_action(
    *, port_ready: bool, gateway_connected: bool, qq_send_available: bool | None = True,
    send_attempt: int,
    state: RecoveryState, now: float, config: WatchdogConfig,
) -> str | None:
    """Choose the least disruptive recovery action for the current state."""
    healthy = port_ready and gateway_connected and qq_send_available is not False and send_attempt == 0
    if healthy:
        state.healthy_polls += 1
        if state.healthy_polls >= 2:
            state.stage = "healthy"
            state.issue_started_at = 0.0
            state.stage_started_at = 0.0
            state.observed_attempt = 0
        return None
    state.healthy_polls = 0
    if state.issue_started_at <= 0:
        state.issue_started_at = now
        state.stage_started_at = now
    issue_age = now - state.issue_started_at
    stage_age = now - state.stage_started_at
    if state.stage == "waiting_manual_login":
        return None
    if state.stage == "recovery_failed":
        if stage_age >= config.escalation_seconds:
            # Start a fresh grace period instead of retrying a failed restart
            # every watchdog poll or remaining stuck forever.
            state.stage = "healthy"
            state.issue_started_at = now
            state.stage_started_at = now
        return None
    if not port_ready:
        if state.stage == "healthy" and issue_age >= config.port_grace_seconds:
            return "restart_napcat_saved"
        if state.stage == "napcat_saved_restarted" and stage_age >= config.escalation_seconds:
            return "start_napcat_interactive"
        return None
    if gateway_connected and qq_send_available is False:
        # The OneBot port and WebSocket can stay alive after the QQ account has
        # been kicked offline. Restarting only the gateway cannot restore the
        # account session, so recover NapCat directly.
        if state.stage == "healthy" and issue_age >= config.gateway_grace_seconds:
            return "restart_napcat_saved"
        if state.stage == "napcat_saved_restarted" and stage_age >= config.escalation_seconds:
            return "start_napcat_interactive"
        return None
    if not gateway_connected:
        if state.stage == "healthy" and issue_age >= config.gateway_grace_seconds:
            return "restart_gateway"
        if state.stage == "gateway_restarted" and stage_age >= config.escalation_seconds:
            return "restart_napcat_saved"
        return None
    if send_attempt >= 2:
        if state.stage == "healthy":
            return "restart_gateway"
        if (
            state.stage == "gateway_restarted"
            and stage_age >= config.escalation_seconds
            and send_attempt > state.observed_attempt
        ):
            return "restart_napcat_saved"
        if (
            state.stage == "napcat_saved_restarted"
            and stage_age >= config.escalation_seconds
            and send_attempt > state.observed_attempt
        ):
            return "start_napcat_interactive"
    return None


class QQWatchdog:
    def __init__(self, config: WatchdogConfig):
        config.validate()
        self.config = config
        self.state_path = config.state_root / "qq_watchdog.json"
        self.lock_path = config.state_root / "qq_watchdog.lock"
        self.state = RecoveryState.load(self.state_path)
        # monotonic() values are meaningful only inside one OS/process uptime.
        # Never reuse persisted timing or a stale manual-login stage after a
        # watchdog restart; keep only the human-readable last action fields.
        self.state.stage = "healthy"
        self.state.issue_started_at = 0.0
        self.state.stage_started_at = 0.0
        self.state.observed_attempt = 0
        self.state.healthy_polls = 0

    def _run_script(self, script_name: str, *arguments: str) -> None:
        script = (self.config.project_root / "scripts" / script_name).resolve()
        scripts_root = (self.config.project_root / "scripts").resolve()
        if script.parent != scripts_root or not script.is_file():
            raise RuntimeError(f"recovery script is missing: {script.name}")
        command = [
            "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(script), *arguments,
        ]
        completed = subprocess.run(
            command,
            cwd=self.config.project_root,
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=90,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or "no script output").strip()
            raise RuntimeError(
                f"{script.name} exited with {completed.returncode}: {detail[:800]}"
            )

    def _perform(self, action: str, attempt: int) -> None:
        LOGGER.warning("QQ recovery action: %s", action)
        if action == "restart_gateway":
            self._run_script(
                "restart_onebot_gateway.ps1", "-Lifecycle", "restarted", "-SkipNotification",
            )
            stage = "gateway_restarted"
        elif action == "restart_napcat_saved":
            self._run_script("restart_napcat_safe.ps1", "-LoginMode", "saved")
            stage = "napcat_saved_restarted"
        elif action == "start_napcat_interactive":
            self._run_script("restart_napcat_safe.ps1", "-LoginMode", "interactive")
            stage = "waiting_manual_login"
        else:
            raise ValueError(f"unknown QQ recovery action: {action}")
        now = time.monotonic()
        self.state.stage = stage
        self.state.stage_started_at = now
        self.state.observed_attempt = attempt
        self.state.last_action = action
        self.state.last_action_at = _iso()
        self.state.save(self.state_path)

    def step(self) -> None:
        port_ready = _port_open()
        gateway = _read_json(self.config.state_root / "onebot_gateway.json")
        gateway_connected = gateway.get("connected") is True
        qq_send_available = gateway.get("qq_send_available")
        send_attempt, detail = _transient_pending_failure(self.config.state_root)
        now = time.monotonic()
        action = choose_action(
            port_ready=port_ready, gateway_connected=gateway_connected,
            qq_send_available=qq_send_available,
            send_attempt=send_attempt, state=self.state, now=now, config=self.config,
        )
        if action:
            try:
                self._perform(action, send_attempt)
            except Exception as error:
                LOGGER.error("QQ recovery action failed: %s: %s", type(error).__name__, error)
                self.state.last_action = f"{action}_failed"
                self.state.last_action_at = _iso()
                self.state.stage = "recovery_failed"
                self.state.stage_started_at = now
        elif detail and send_attempt:
            LOGGER.debug("QQ send channel remains unhealthy: attempt=%s error=%s", send_attempt, detail)
        self.state.save(self.state_path)

    def serve_forever(self) -> None:
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        if self.lock_path.is_file():
            try:
                old_pid = int(self.lock_path.read_text(encoding="ascii").strip())
                os.kill(old_pid, 0)
            except (OSError, ValueError):
                self.lock_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(f"QQ watchdog is already running with PID={old_pid}")
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as error:
            raise RuntimeError("QQ watchdog is already running") from error
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            os.close(descriptor)
            while True:
                self.step()
                time.sleep(self.config.poll_seconds)
        finally:
            try:
                self.lock_path.unlink()
            except OSError:
                pass


def _configure_logging(project_root: Path) -> None:
    log_path = project_root / "logs" / "qq_watchdog.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)


def main() -> int:
    parser = argparse.ArgumentParser(prog="oopz-qq-watchdog")
    parser.add_argument("command", choices=("serve", "check"))
    args = parser.parse_args()
    from .env_loader import load_project_env
    load_project_env()
    config = WatchdogConfig.from_env()
    _configure_logging(config.project_root)
    try:
        watchdog = QQWatchdog(config)
        if args.command == "check":
            watchdog.step()
            print(json.dumps(_read_json(watchdog.state_path), ensure_ascii=False, indent=2))
            return 0
        watchdog.serve_forever()
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
