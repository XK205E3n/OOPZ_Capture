"""Cross-platform (Windows + Linux) portability checks for deployment assets."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from oopz_capture import pdf_reports

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LINUX_SCRIPTS = [
    "scripts/linux/install_prerequisites.sh",
    "scripts/linux/install_release.sh",
    "scripts/linux/update_release.sh",
    "scripts/linux/rollback_release.sh",
]
LINUX_SCRIPT_MODES = {
    "scripts/linux/install_prerequisites.sh": "100755",
    "scripts/linux/install_release.sh": "100755",
    "scripts/linux/update_release.sh": "100755",
    "scripts/linux/rollback_release.sh": "100755",
}


def _find_bash() -> str | None:
    bash = shutil.which("bash")
    if bash is None and sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    return bash


def test_linux_installer_checks_node_dependencies_in_release_directory(tmp_path: Path) -> None:
    """The caller's cwd must not decide which release's dependencies are checked."""
    bash = _find_bash()
    if bash is None:
        pytest.skip("bash is not installed")
    node = pdf_reports.resolve_node_path()
    release = tmp_path / "release with spaces"
    package = release / "node_modules/md-to-pdf"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "md-to-pdf", "main": "index.js"}), encoding="utf-8")
    (package / "index.js").write_text("console.log('REVIEW_FIXTURE_SELECTED'); module.exports = {};\n", encoding="utf-8")
    caller = tmp_path / "outside-release"
    caller.mkdir()
    installer = (PROJECT_ROOT / "scripts/linux/install_release.sh").read_text(encoding="utf-8")
    command = next(line for line in installer.splitlines() if "--input-type=module" in line).rstrip().removesuffix("\\").rstrip()
    env = {**os.environ, "RELEASE_PATH": release.as_posix(), "NODE_BIN": node.as_posix()}
    result = subprocess.run([bash, "-c", command], cwd=caller, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "REVIEW_FIXTURE_SELECTED" in result.stdout
    assert "Node report dependency OK" in result.stdout


# --- L03: PDF Node runtime resolution ---------------------------------------


def test_resolve_node_path_prefers_explicit_override(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "node"
    override.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("OOPZ_NODE_PATH", str(override))
    assert pdf_reports.resolve_node_path() == override


def test_resolve_node_path_rejects_missing_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OOPZ_NODE_PATH", str(tmp_path / "missing"))
    with pytest.raises(FileNotFoundError, match="OOPZ_NODE_PATH"):
        pdf_reports.resolve_node_path()


def test_resolve_node_path_posix_falls_back_to_path_node(tmp_path: Path, monkeypatch) -> None:
    system_node = tmp_path / "bin" / "node"
    system_node.parent.mkdir(parents=True, exist_ok=True)
    system_node.write_text("fixture", encoding="utf-8")
    monkeypatch.setattr(pdf_reports, "NODE_PATH", tmp_path / "missing" / "node.exe")
    monkeypatch.setattr(pdf_reports.shutil, "which", lambda name: str(system_node))
    resolved = pdf_reports.resolve_node_path(posix=True)
    assert resolved == system_node


def test_resolve_node_path_posix_prefers_project_runtime(tmp_path: Path, monkeypatch) -> None:
    pinned = tmp_path / "tools" / "node" / "node"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text("fixture", encoding="utf-8")
    monkeypatch.delenv("OOPZ_NODE_PATH", raising=False)
    monkeypatch.setattr(pdf_reports, "NODE_PATH", tmp_path / "tools" / "node" / "node.exe")
    monkeypatch.setattr(pdf_reports, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(pdf_reports.shutil, "which", lambda name: "/usr/bin/node")
    assert pdf_reports.resolve_node_path(posix=True) == pinned


def test_resolve_node_path_error_lists_candidates(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OOPZ_NODE_PATH", raising=False)
    missing = tmp_path / "missing" / "node.exe"
    monkeypatch.setattr(pdf_reports, "NODE_PATH", missing)
    monkeypatch.setattr(pdf_reports.shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError) as error:
        pdf_reports.resolve_node_path(posix=True)
    assert "OOPZ_NODE_PATH" in str(error.value)
    assert str(missing) in str(error.value)


# --- L04: PDF browser lookup on Linux ---------------------------------------


def _run_node_program(program: str) -> subprocess.CompletedProcess[str]:
    node = pdf_reports.resolve_node_path()
    return subprocess.run(
        [str(node), "--input-type=module", "-e", program],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_find_chrome_supports_linux_and_macos_candidates() -> None:
    node = pdf_reports.resolve_node_path()
    if not node.is_file():
        pytest.skip("Node runtime is not installed in tools/node or on PATH")
    program = r'''import {findChrome} from './tools/md_to_pdf.mjs';
import assert from 'node:assert/strict';
assert.equal(findChrome({}, p => p === '/usr/bin/chromium', 'linux'), '/usr/bin/chromium');
assert.equal(findChrome({}, p => p === '/usr/bin/google-chrome-stable', 'linux'), '/usr/bin/google-chrome-stable');
assert.throws(() => findChrome({}, () => false, 'linux'), /Chromium/);
assert.equal(findChrome({}, p => p === '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', 'darwin'), '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
assert.equal(findChrome({}, p => p === edge, 'win32'), edge);
'''
    result = _run_node_program(program)
    assert result.returncode == 0, result.stderr


# --- L08/L09: graceful SIGTERM stop for the gateway --------------------------


class _FakeChannel:
    def __init__(self) -> None:
        self.connected = False
        self.disconnected = False

    async def connect_until_ready(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True


class _FakeGateway:
    def __init__(self, stop_after_drains: int, stop_future) -> None:
        self.stop_after_drains = stop_after_drains
        self.stop_future = stop_future
        self.drain_calls = 0
        self.notices: list[str] = []
        self.reconcile_calls = 0
        self.cleanup_calls = 0

    async def send_lifecycle_notice(self, text: str) -> None:
        self.notices.append(text)

    async def drain_outbox(self) -> None:
        self.drain_calls += 1
        if self.drain_calls >= self.stop_after_drains and not self.stop_future.done():
            self.stop_future.set_result(None)

    async def reconcile_publications(self) -> None:
        self.reconcile_calls += 1

    async def cleanup_expired_sessions(self) -> None:
        self.cleanup_calls += 1


def test_serve_gateway_stops_gracefully_on_signalled_future() -> None:
    from oopz_capture.feishu_cli import serve_gateway

    async def scenario():
        stop = asyncio.get_running_loop().create_future()
        channel = _FakeChannel()
        gateway = _FakeGateway(stop_after_drains=2, stop_future=stop)
        await serve_gateway(channel, gateway, lifecycle="restarted", stop_future=stop)
        return channel, gateway

    channel, gateway = asyncio.run(scenario())
    assert channel.connected
    assert channel.disconnected
    assert gateway.drain_calls == 2
    # Housekeeping scheduling depends on monotonic uptime (an uptime above one
    # hour triggers a first reconcile), so only the graceful stop is asserted.
    assert any("重启完成" in notice for notice in gateway.notices)


def test_serve_gateway_skips_loop_when_stop_already_requested() -> None:
    from oopz_capture.feishu_cli import serve_gateway

    async def scenario():
        stop = asyncio.get_running_loop().create_future()
        stop.set_result(None)
        channel = _FakeChannel()
        gateway = _FakeGateway(stop_after_drains=99, stop_future=stop)
        await serve_gateway(channel, gateway, lifecycle=None, stop_future=stop)
        return channel, gateway

    channel, gateway = asyncio.run(scenario())
    assert channel.connected and channel.disconnected
    assert gateway.drain_calls == 0


def test_find_chrome_prefers_playwright_cache_before_distro_paths() -> None:
    node = pdf_reports.resolve_node_path()
    if not node.is_file():
        pytest.skip("Node runtime is not installed in tools/node or on PATH")
    program = r'''import {findChrome} from './tools/md_to_pdf.mjs';
import assert from 'node:assert/strict';
const dirs = {'/home/oopz/.cache/ms-playwright': ['chromium-1234', 'chromium-9999']};
const files = new Set([
  '/home/oopz/.cache/ms-playwright/chromium-9999/chrome-linux/chrome',
  '/usr/bin/chromium',
]);
const listed = (dir) => dirs[dir] || [];
// newest playwright build wins over the distro chromium
assert.equal(
  findChrome({HOME: '/home/oopz'}, (f) => files.has(f), 'linux', listed),
  '/home/oopz/.cache/ms-playwright/chromium-9999/chrome-linux/chrome');
// empty cache falls back to distro candidates
assert.equal(
  findChrome({HOME: '/home/oopz'}, (f) => f === '/usr/bin/chromium', 'linux', () => []),
  '/usr/bin/chromium');
'''
    result = _run_node_program(program)
    assert result.returncode == 0, result.stderr


def test_sigterm_handler_installation_degrades_on_windows() -> None:
    from oopz_capture.feishu_cli import _install_sigterm_stop

    async def scenario():
        loop = asyncio.get_running_loop()
        stop = loop.create_future()
        installed = _install_sigterm_stop(loop, stop)
        return installed, stop

    installed, stop = asyncio.run(scenario())
    if sys.platform == "win32":
        assert installed is False
    else:
        assert installed is True
    assert not stop.done()


# --- L10/L11: Linux deployment assets are release-ready ----------------------


@pytest.mark.parametrize("relative", LINUX_SCRIPTS)
def test_linux_scripts_use_lf_line_endings(relative: str) -> None:
    data = (PROJECT_ROOT / relative).read_bytes()
    assert b"\r\n" not in data, f"{relative} contains CRLF line endings"


def test_linux_scripts_are_staged_executable() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "-s", "scripts/linux/"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modes = {}
    for line in listed.splitlines():
        _mode, _hash, _stage, path = line.split()
        modes[path] = _mode
    for relative, mode in LINUX_SCRIPT_MODES.items():
        assert modes.get(relative) == mode, (
            f"{relative} must be committed with mode {mode} so a Linux Git "
            "checkout can execute it (git update-index --chmod=+x)"
        )


def test_linux_scripts_pass_bash_syntax_check() -> None:
    bash = _find_bash()
    if not bash:
        pytest.skip("bash is not available on this machine")
    for relative in LINUX_SCRIPTS:
        result = subprocess.run([bash, "-n", str(PROJECT_ROOT / relative)], capture_output=True, text=True)
        assert result.returncode == 0, f"{relative}: {result.stderr}"


def test_service_and_logrotate_templates_carry_install_root_placeholder() -> None:
    unit = (PROJECT_ROOT / "scripts/linux/oopz-capture.service").read_text(encoding="utf-8")
    logrotate = (PROJECT_ROOT / "scripts/linux/oopz-capture.logrotate").read_text(encoding="utf-8")
    assert "__INSTALL_ROOT__" in unit
    assert "feishu_cli serve" in unit and "User=oopz" in unit
    assert "__INSTALL_ROOT__" in logrotate and "copytruncate" in logrotate
