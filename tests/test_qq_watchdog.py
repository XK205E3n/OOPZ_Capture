from __future__ import annotations

from pathlib import Path

from oopz_capture.qq_watchdog import RecoveryState, WatchdogConfig, choose_action


def config(tmp_path: Path) -> WatchdogConfig:
    return WatchdogConfig(
        project_root=tmp_path,
        state_root=tmp_path / "controller_state",
        poll_seconds=10,
        port_grace_seconds=60,
        gateway_grace_seconds=45,
        escalation_seconds=120,
    )


def test_healthy_state_never_restarts_any_service(tmp_path: Path) -> None:
    state = RecoveryState()
    action = choose_action(
        port_ready=True, gateway_connected=True, send_attempt=0,
        state=state, now=1000, config=config(tmp_path),
    )
    assert action is None
    assert state.stage == "healthy"


def test_gateway_failure_restarts_only_gateway_after_grace(tmp_path: Path) -> None:
    state = RecoveryState()
    assert choose_action(
        port_ready=True, gateway_connected=False, send_attempt=0,
        state=state, now=1000, config=config(tmp_path),
    ) is None
    assert choose_action(
        port_ready=True, gateway_connected=False, send_attempt=0,
        state=state, now=1046, config=config(tmp_path),
    ) == "restart_gateway"


def test_logged_out_qq_restarts_napcat_even_when_websocket_stays_connected(tmp_path: Path) -> None:
    state = RecoveryState()
    assert choose_action(
        port_ready=True, gateway_connected=True, qq_send_available=False, send_attempt=0,
        state=state, now=1000, config=config(tmp_path),
    ) is None
    assert choose_action(
        port_ready=True, gateway_connected=True, qq_send_available=False, send_attempt=0,
        state=state, now=1046, config=config(tmp_path),
    ) == "restart_napcat_saved"


def test_repeated_send_failures_escalate_without_touching_oopz(tmp_path: Path) -> None:
    state = RecoveryState()
    assert choose_action(
        port_ready=True, gateway_connected=True, send_attempt=2,
        state=state, now=1000, config=config(tmp_path),
    ) == "restart_gateway"
    state.stage = "gateway_restarted"
    state.stage_started_at = 1000
    state.observed_attempt = 2
    assert choose_action(
        port_ready=True, gateway_connected=True, send_attempt=3,
        state=state, now=1121, config=config(tmp_path),
    ) == "restart_napcat_saved"
    state.stage = "napcat_saved_restarted"
    state.stage_started_at = 1121
    state.observed_attempt = 3
    assert choose_action(
        port_ready=True, gateway_connected=True, send_attempt=4,
        state=state, now=1242, config=config(tmp_path),
    ) == "start_napcat_interactive"
    state.stage = "waiting_manual_login"
    assert choose_action(
        port_ready=False, gateway_connected=False, send_attempt=5,
        state=state, now=2000, config=config(tmp_path),
    ) is None
