"""Behavior tests for the Linux install/update/rollback scripts.

Runs the real bash scripts in an isolated Git Bash sandbox: every external
command that would touch the OS (systemctl, runuser, chown, node, npx, pip,
playwright, curl) is replaced by a PATH shim that either succeeds, produces
the expected side effect, or fails according to FAKE_FAIL_* points. Real
unzip/sha256sum/sed/ln and the real python helpers (guard, verify, node
check) execute for actual logic. Failure injection asserts the transaction's
observable side effects (service log order, current link, unit file), not
script text. This is NOT equivalent to Ubuntu/systemd acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from test_linux_portability import _find_bash

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PROJECT_ROOT / "scripts" / "linux"
READY_TEXT = "飞书长连接已就绪"

pytestmark = pytest.mark.skipif(_find_bash() is None, reason="bash is not installed")


def _shim(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)



def _git_bash_tool(name: str) -> str:
    """Absolute path of a real Git Bash coreutils tool (never the shim)."""
    bash = _find_bash()
    candidates = []
    if bash:
        bash_path = Path(bash)
        for usr_bin in (bash_path.parent.parent / "usr" / "bin", bash_path.parent / "usr" / "bin"):
            candidates.append(usr_bin / (name + ".exe"))
            candidates.append(usr_bin / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate).replace("\\", "/")
    return f"/usr/bin/{name}"


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.install_root = tmp_path / "oopz"
        self.bin = tmp_path / "shimbin"
        self.unit_dir = tmp_path / "etc-systemd"
        self.logrotate_dir = tmp_path / "etc-logrotate"
        for directory in (self.bin, self.unit_dir, self.logrotate_dir,
                          self.install_root / "artifacts",
                          self.install_root / "shared" / "config",
                          self.install_root / "shared" / "logs",
                          self.install_root / "shared" / "feishu_state",
                          self.install_root / "shared" / "output",
                          self.install_root / "shared" / "tools" / "node",
                          self.install_root / "releases"):
            directory.mkdir(parents=True, exist_ok=True)
        self.systemd_log = tmp_path / "systemd.calls"
        self.runtime_log = self.install_root / "shared" / "logs" / "feishu_runtime.log"
        self.runtime_log.touch()
        self.service_state = tmp_path / "service.state"
        self.service_state.write_text("inactive", encoding="utf-8")
        self._write_shims()
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "OOPZ_INSTALLER_SELFTEST": "1",
            "OOPZ_UNIT_DIR": str(self.unit_dir),
            "OOPZ_LOGROTATE_DIR": str(self.logrotate_dir),
            "FAKE_SYSTEMD_LOG": str(self.systemd_log),
            "FAKE_RUNTIME_LOG": str(self.runtime_log),
            "FAKE_SERVICE_STATE": str(self.service_state),
        }

    # -- shims ---------------------------------------------------------------
    def _write_shims(self) -> None:
        # Plain strings with __TOKEN__ replacement: f-strings would eat the
        # shell's ${VAR:-default} parameter expansions.
        real_python = sys.executable.replace("\\", "/")
        _shim(self.bin / "python3.12", '''\
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  venv_path="$3"
  mkdir -p "$venv_path/bin"
  cat > "$venv_path/bin/python" <<'VENVSH'
#!/usr/bin/env bash
case "${1:-}" in
  -m)
    case "$2" in
      pip)
        if [[ "${FAKE_FAIL_PIP:-0}" == "1" ]]; then
          for a in "$@"; do [[ "$a" == "-e" ]] && exit 1; done
        fi
        exit 0 ;;
      playwright) [[ "${FAKE_FAIL_CHROMIUM:-0}" == "1" ]] && exit 1; exit 0 ;;
    esac ;;
  -c) [[ "${FAKE_FAIL_IMPORTS:-0}" == "1" ]] && exit 1; exit 0 ;;
esac
if [[ "$*" == *"download_sensevoice_model.py"* ]]; then
  [[ "${FAKE_FAIL_MODEL:-0}" == "1" ]] && exit 1
  target=""; prev=""
  for a in "$@"; do [[ "$prev" == "--target" ]] && target="$a"; prev="$a"; done
  mkdir -p "$target" && : > "$target/model.pt"
  exit 0
fi
exit 0
VENVSH
  chmod +x "$venv_path/bin/python"
  exit 0
fi
exec "__REAL_PY__" "$@"
'''.replace("__REAL_PY__", real_python))
        _shim(self.bin / "node", '''\
if [[ "${1:-}" == "--version" ]]; then echo "${FAKE_NODE_VERSION:-v22.14.0}"; exit 0; fi
for a in "$@"; do
  if [[ "$a" == *.pdf ]]; then
    [[ "${FAKE_FAIL_PDF:-0}" == "1" ]] && exit 1
    printf '%s' "%PDF-fake-ok" > "$a"
    exit 0
  fi
done
exit 0
''')
        _shim(self.bin / "npx", '[[ "${FAKE_FAIL_NPM:-0}" == "1" ]] && exit 1\nexit 0\n')
        _shim(self.bin / "chown", "exit 0\n")
        _shim(self.bin / "runuser", '''\
[[ "${1:-}" == "-u" ]] && shift 2
[[ "${1:-}" == "--" ]] && shift
if [[ "${1:-}" == "env" ]]; then
  shift
  while [[ "$#" -gt 0 && "$1" == *=* ]]; do export "${1%%=*}=${1#*=}"; shift; done
fi
exec "$@"
''')
        _shim(self.bin / "systemctl", '''\
echo "systemctl $*" >> "$FAKE_SYSTEMD_LOG"
cmd="$1"; shift
state_file="${FAKE_SERVICE_STATE:?}"
case "$cmd" in
  is-active) [[ -f "$state_file" && "$(cat "$state_file")" == "active" ]] && exit 0; exit 3 ;;
  start)
    [[ "${FAKE_FAIL_START:-0}" == "1" ]] && exit 1
    echo active > "$state_file"
    [[ "${FAKE_FAIL_HEALTH:-0}" != "1" ]] && printf '%s\\n' "${FAKE_READY_TEXT:-__READY__}" >> "$FAKE_RUNTIME_LOG"
    exit 0 ;;
  stop) echo inactive > "$state_file"; exit 0 ;;
  daemon-reload) [[ "${FAKE_FAIL_RELOAD:-0}" == "1" ]] && exit 1; exit 0 ;;
  *) exit 0 ;;
esac
'''.replace("__READY__", READY_TEXT))
        # Portable link emulation: this Windows user cannot create symlinks, so
        # `ln -sfn` builds a directory containing a .target marker, readlink -f
        # resolves the chain, and rm removes marker directories. Real files
        # fall through to the real commands.
        real_rm = _git_bash_tool("rm")
        _shim(self.bin / "ln", f'''\
if [[ "${{1:-}}" == "-sfn" || "${{1:-}}" == "-sn" || "${{1:-}}" == "-s" ]]; then
  target="$2"; link="$3"
  "{real_rm}" -rf "$link"
  if [[ -d "$target" && ! -f "$target/.target" ]]; then
    mkdir -p "$link"
    printf '%s' "$target" > "$link/.target"
  else
    printf '%s' "$target" > "$link"
  fi
  exit 0
fi
exec "{_git_bash_tool('ln')}" "$@"
''')
        _shim(self.bin / "readlink", f'''\
if [[ "${{1:-}}" == "-f" && $# -ge 2 ]]; then
  path="$2"
  if [[ -d "$path" && -f "$path/.target" ]]; then
    target="$(cat "$path/.target")"
    case "$target" in /*|?:/*) printf '%s\\n' "$target"; exit 0 ;; esac
  fi
fi
exec "{_git_bash_tool('readlink')}" "$@"
''')
        _shim(self.bin / "rm", f'''\
# Remove marker-directory links directly; forward everything else.
has_other=0
for arg in "$@"; do
  case "$arg" in -*) continue ;; esac
  if [[ -d "$arg" && -f "$arg/.target" ]]; then
    "{real_rm}" -rf "$arg"
  else
    has_other=1
  fi
done
if [[ "$has_other" -eq 1 ]]; then exec "{real_rm}" "$@"; fi
exit 0
''')
        _shim(self.bin / "mv", f'''\
# marker-link emulation: renaming onto an existing marker directory needs an
# explicit remove first (real symlinks rename atomically on Linux).
if [[ "$#" -eq 3 && ( "${{1:-}}" == "-Tf" || "${{1:-}}" == "-fT" || "${{1:-}}" == "-T" ) ]]; then
  if [[ -f "$2/.target" || -f "$3/.target" ]]; then
    "{real_rm}" -rf "$3"
  fi
fi
exec "{_git_bash_tool('mv')}" "$@"
''')
        # shared Node runtime link points at the shim
        node_shim = self.bin / "node"
        node_link_dir = self.install_root / "shared" / "tools" / "node"
        if node_link_dir.is_dir() and not node_link_dir.is_symlink():
            _shim(node_link_dir / "node", f'exec "{node_shim.as_posix()}" "$@"\n')
            _shim(node_link_dir / "npx", f'exec "{(self.bin / "npx").as_posix()}" "$@"\n')

    # -- fixtures ------------------------------------------------------------
    def make_artifact(self, release_id: str, *, installer_body: str | None = None,
                      directory: Path | None = None) -> tuple[Path, Path]:
        staging = self.root / f"stage-{release_id}"
        staging.mkdir(exist_ok=True)
        (staging / "RELEASE_MANIFEST.json").write_text(json.dumps({"release_id": release_id}), encoding="utf-8")
        linux = staging / "scripts" / "linux"
        linux.mkdir(parents=True, exist_ok=True)
        (linux / "oopz-capture.service").write_text(
            (SCRIPTS / "oopz-capture.service").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        (linux / "oopz-capture.logrotate").write_text(
            (SCRIPTS / "oopz-capture.logrotate").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        if installer_body is not None:
            (linux / "install_release.sh").write_text(installer_body, encoding="utf-8", newline="\n")
        (staging / "scripts" / "download_sensevoice_model.py").write_text("# stub\n", encoding="utf-8")
        (staging / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n", encoding="utf-8")
        (staging / ".env.example").write_text("# stub\n", encoding="utf-8")
        tools = staging / "tools"
        tools.mkdir(exist_ok=True)
        (tools / "md_to_pdf.mjs").write_text("// stub\n", encoding="utf-8")
        artifact_dir = directory if directory is not None else self.install_root / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = artifact_dir / f"oopz-capture-{release_id}.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        checksum = artifact.with_name(artifact.name + ".sha256")
        checksum.write_text(f"{digest}  {artifact.name}\n", encoding="utf-8", newline="\n")
        return artifact, checksum

    def write_env(self, *, admin_chat_id: str | None = "oc_test123") -> Path:
        env = self.install_root / "shared" / "config" / ".env"
        lines = ["OOPZ_FEISHU_APP_ID=app", "OOPZ_FEISHU_APP_SECRET=secret"]
        if admin_chat_id:
            lines.append(f"OOPZ_FEISHU_ADMIN_CHAT_ID={admin_chat_id}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return env

    def write_controller(self, payload) -> None:
        (self.install_root / "shared" / "feishu_state" / "controller.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")

    def make_installed_release(self, release_id: str) -> Path:
        release = self.install_root / "releases" / release_id
        (release / "scripts" / "linux").mkdir(parents=True, exist_ok=True)
        # A distinguishable unit per release so restoration is observable.
        (release / "scripts" / "linux" / "oopz-capture.service").write_text(
            f"# release: {release_id}\n"
            + (SCRIPTS / "oopz-capture.service").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        (release / "RELEASE_MANIFEST.json").write_text(json.dumps({"release_id": release_id}), encoding="utf-8")
        (release / ".prepare-complete").touch()
        venv_bin = release / ".venv" / "bin"
        venv_bin.mkdir(parents=True, exist_ok=True)
        _shim(venv_bin / "python", "exit 0\n")
        return release

    def set_service_active(self, active: bool) -> None:
        self.service_state.write_text("active" if active else "inactive", encoding="utf-8")

    def set_current(self, release_id: str) -> None:
        # Marker-directory link emulation (this Windows user has no symlink
        # privilege); mirrors what the ln shim produces.
        current = self.install_root / "current"
        if current.is_dir():
            import shutil as _shutil

            _shutil.rmtree(current)
        elif current.exists():
            current.unlink()
        current.mkdir()
        (current / ".target").write_text(
            (self.install_root / "releases" / release_id).as_posix(), encoding="utf-8")

    def current_target(self) -> str | None:
        current = self.install_root / "current"
        marker = current / ".target"
        if current.is_dir() and marker.is_file():
            return marker.read_text(encoding="utf-8").strip()
        return None

    # -- runner ----------------------------------------------------------------
    def run(self, script: str, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [_find_bash(), str(SCRIPTS / script), *args],
            env=self.env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(self.root), timeout=timeout,
        )
        return result

    def run_install(self, artifact: Path, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
        env = {**self.env, **env_extra}
        return subprocess.run(
            [_find_bash(), str(SCRIPTS / "install_release.sh"), "-f", artifact.as_posix(),
             "-r", self.install_root.as_posix(), "-t", "3", *args],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(self.root), timeout=120,
        )

    def systemd_calls(self) -> list[str]:
        if not self.systemd_log.exists():
            return []
        return [line.strip() for line in self.systemd_log.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- R3: prepare failures never touch the service ----------------------------


def test_prepare_failure_leaves_service_untouched(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    artifact, _ = box.make_artifact("v9.9.9-preparefail")
    result = box.run_install(artifact, FAKE_FAIL_MODEL="1")
    assert result.returncode != 0
    assert "SenseVoiceSmall" in (result.stdout + result.stderr)
    assert box.systemd_calls() == [], "prepare must not call systemctl"
    assert not (box.install_root / "current").exists()
    release = box.install_root / "releases" / "v9.9.9-preparefail"
    assert release.is_dir(), "incomplete prepare is kept for diagnosis"
    assert not (release / ".prepare-complete").exists()


def test_prepare_retry_after_failure_succeeds(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    artifact, _ = box.make_artifact("v9.9.9-retry")
    assert box.run_install(artifact, "--prepare-only", FAKE_FAIL_PIP="1").returncode != 0
    result = box.run_install(artifact, "--prepare-only")
    assert result.returncode == 0, result.stderr
    assert (box.install_root / "releases" / "v9.9.9-retry" / ".prepare-complete").is_file()
    assert box.systemd_calls() == []


def test_prepare_refuses_current_target(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.make_installed_release("v1.0.0-current")
    box.set_current("v1.0.0-current")
    # An incomplete prepare of the ACTIVE target must never be wiped by retry.
    (box.install_root / "releases" / "v1.0.0-current" / ".prepare-complete").unlink()
    artifact, _ = box.make_artifact("v1.0.0-current")
    result = box.run_install(artifact, "--prepare-only")
    assert result.returncode != 0
    assert "current target" in (result.stdout + result.stderr)
    assert (box.install_root / "releases" / "v1.0.0-current").is_dir(), "active release directory preserved"


# --- R3: switch-time failures restore previous state --------------------------


def test_full_install_success_switches_and_starts(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.write_env()
    artifact, _ = box.make_artifact("v2.0.0-good")
    result = box.run_install(artifact)
    assert result.returncode == 0, result.stderr
    assert '"status": "deployed"' in result.stdout
    from pathlib import Path as _P
    assert _P(box.current_target()) == box.install_root / "releases" / "v2.0.0-good"
    unit = (box.unit_dir / "oopz-capture.service").read_text(encoding="utf-8")
    assert box.install_root.as_posix() in unit and "__INSTALL_ROOT__" not in unit
    calls = box.systemd_calls()
    assert "systemctl enable oopz-capture" in calls
    assert "systemctl start oopz-capture" in calls
    assert not any(call.startswith("systemctl stop") for call in calls), "first install has nothing to stop"


def test_health_failure_restores_previous_release(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    old = box.make_installed_release("v1.0.0-old")
    box.set_current("v1.0.0-old")
    old_unit = box.unit_dir / "oopz-capture.service"
    old_unit.parent.mkdir(exist_ok=True)
    old_unit.write_text("[Unit]\n# previous unit\n", encoding="utf-8")
    box.write_env()
    artifact, _ = box.make_artifact("v2.0.0-healthfail")
    box.set_service_active(True)
    result = box.run_install(artifact, FAKE_FAIL_HEALTH="1")
    assert result.returncode != 0
    assert "health check failed" in (result.stdout + result.stderr)
    from pathlib import Path as _P
    assert _P(box.current_target()) == old, "current must return to the previous release"
    assert old_unit.read_text(encoding="utf-8").startswith("# release: v1.0.0-old"), "previous unit must be restored"
    calls = box.systemd_calls()
    assert calls.count("systemctl stop oopz-capture") >= 2, "new service stopped during restore"
    assert calls[-1] == "systemctl start oopz-capture", "previously-running service restarted"
    assert (box.install_root / "releases" / "v2.0.0-healthfail" / ".prepare-complete").is_file()


def test_first_install_health_failure_leaves_no_current_or_unit(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.write_env()
    artifact, _ = box.make_artifact("v2.0.0-firstfail")
    result = box.run_install(artifact, FAKE_FAIL_HEALTH="1")
    assert result.returncode != 0
    assert not (box.install_root / "current").exists()
    assert not (box.unit_dir / "oopz-capture.service").exists()
    assert "systemctl disable oopz-capture" in box.systemd_calls()


def test_unit_write_failure_restores_before_switch(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    old = box.make_installed_release("v1.0.0-old")
    box.set_current("v1.0.0-old")
    (box.unit_dir / "oopz-capture.service").write_text("[Unit]\n# previous\n", encoding="utf-8")
    box.write_env()
    artifact, _ = box.make_artifact("v2.0.0-unitfail")
    box.set_service_active(True)
    result = box.run_install(artifact, FAKE_FAIL_RELOAD="1")
    assert result.returncode != 0
    from pathlib import Path as _P
    assert _P(box.current_target()) == old
    assert "systemctl start oopz-capture" == box.systemd_calls()[-1]


# --- R4: the guard blocks switching ------------------------------------------


def test_activate_blocked_while_background_analysis_running(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.make_installed_release("v1.0.0-old")
    box.set_current("v1.0.0-old")
    box.write_env()
    box.write_controller({"active": None, "last_job": {"status": "analyzing"}})
    artifact, _ = box.make_artifact("v2.0.0-guard")
    box.set_service_active(True)
    result = box.run_install(artifact)
    assert result.returncode != 0
    assert "active tasks" in (result.stdout + result.stderr)
    assert not any(call.startswith("systemctl stop") for call in box.systemd_calls()), "service never stopped"
    from pathlib import Path as _P
    assert _P(box.current_target()).name == "v1.0.0-old"


def test_activate_force_overrides_guard(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.write_env()
    box.write_controller({"active": None, "last_job": {"status": "analyzing"}})
    artifact, _ = box.make_artifact("v2.0.0-forced")
    box.set_service_active(True)
    result = box.run_install(artifact, "--force")
    assert result.returncode == 0, result.stderr
    from pathlib import Path as _P
    assert _P(box.current_target()).name == "v2.0.0-forced"


# --- R1: the Node gate --------------------------------------------------------


def test_incompatible_node_refused_during_prepare(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    artifact, _ = box.make_artifact("v2.0.0-node18")
    result = box.run_install(artifact, "--prepare-only", FAKE_NODE_VERSION="v18.16.0")
    assert result.returncode != 0
    assert "22.12.0" in (result.stdout + result.stderr)
    assert box.systemd_calls() == []


# --- R6: staged bootstrap ------------------------------------------------------


def test_activate_requires_prepare_marker(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.write_env()
    artifact, _ = box.make_artifact("v2.0.0-unprepared")
    result = box.run_install(artifact, "--activate")
    assert result.returncode != 0
    assert "prepare" in (result.stdout + result.stderr)


def test_prepare_then_activate_bootstrap_without_env(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    artifact, _ = box.make_artifact("v2.0.0-bootstrap")
    result = box.run_install(artifact, "--prepare-only")
    assert result.returncode == 0, result.stderr
    assert not (box.install_root / "current").exists()
    assert box.systemd_calls() == []
    box.write_env(admin_chat_id=None)  # binding still pending
    result = box.run_install(artifact, "--activate", FAKE_READY_TEXT="尚未绑定控制群；请将机器人邀请至目标群聊")
    assert result.returncode == 0, result.stderr
    from pathlib import Path as _P
    assert _P(box.current_target()).name == "v2.0.0-bootstrap"


# --- R2: the updater verifies before executing new code ------------------------


def _make_update_fixture(box: Sandbox, *, correct_checksum: bool, sha_assets: list[tuple[str, str]]) -> Path:
    """Returns the package path; it lives OUTSIDE artifacts/ so the simulated
    download can never overwrite its own source."""
    marker = box.root / "new-installer-ran"
    installer = (
        "#!/usr/bin/env bash\n"
        f'echo ran >> "{marker.as_posix()}"\n'
        "exit 0\n"
    )
    artifact, checksum = box.make_artifact("v9.9.9-new", installer_body=installer,
                                           directory=box.root / "download-src")
    zip_name = artifact.name
    if not correct_checksum:
        checksum.write_text(("0" * 64) + f"  {zip_name}\n", encoding="utf-8", newline="\n")
    assets = [
        (zip_name, str(artifact)),
        *[(name, str(checksum) if name == zip_name + ".sha256" else str(box.root / "decoy.sha256"))
          for name, _ in sha_assets],
    ]
    decoy = box.root / "decoy.sha256"
    decoy.write_text(("1" * 64) + "  other-file.zip\n", encoding="utf-8")
    api = {
        "tag_name": "v9.9.9-new",
        "assets": [{"name": name, "browser_download_url": f"file://{path}"} for name, path in assets],
    }
    (box.root / "api.json").write_text(json.dumps(api), encoding="utf-8", newline="\n")


def _curl_shim(box: Sandbox) -> None:
    _shim(box.bin / "curl", '''
out=""; prev=""
for a in "$@"; do [[ "$prev" == "-o" ]] && out="$a"; prev="$a"; done
url="${@: -1}"
case "$url" in
  *api.github.com*) cat "${FAKE_API_JSON:?}" ;;
  *.sha256) cat "${FAKE_SHA_SOURCE:?}" ;;
  *.) exit 1 ;;
  *.zip) cat "${FAKE_ZIP_SOURCE:?}" ;;
  *) exit 1 ;;
esac > "$out" || exit 1
exit 0
''')


def _run_update(box: Sandbox) -> subprocess.CompletedProcess[str]:
    box.make_installed_release("v0.0.1-old")
    box.set_current("v0.0.1-old")
    env = {**box.env,
           "FAKE_API_JSON": str(box.root / "api.json"),
           "FAKE_ZIP_SOURCE": (box.root / "download-src" / "oopz-capture-v9.9.9-new.zip").as_posix(),
           "FAKE_SHA_SOURCE": str(box.root / "sha-source")}
    return subprocess.run(
        [_find_bash(), str(SCRIPTS / "update_release.sh"), "-r", box.install_root.as_posix(), "-t", "3"],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(box.root), timeout=120,
    )


def test_update_bad_checksum_never_runs_new_installer(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    _curl_shim(box)
    (box.root / "sha-source").write_text(("0" * 64) + "  oopz-capture-v9.9.9-new.zip\n", encoding="utf-8")
    _make_update_fixture(box, correct_checksum=False,
                         sha_assets=[("oopz-capture-v9.9.9-new.zip.sha256", "")])
    result = _run_update(box)
    assert result.returncode != 0
    assert "verification failed" in (result.stdout + result.stderr)
    assert not (box.root / "new-installer-ran").exists(), "new package code must not run"


def test_update_paired_checksum_runs_installer_after_verification(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    _curl_shim(box)
    # Build the package first, then mirror its real digest into the sha asset
    # source; the decoy listed LAST must not win the selection.
    _make_update_fixture(box, correct_checksum=True,
                         sha_assets=[("other-file.zip.sha256", ""), ("oopz-capture-v9.9.9-new.zip.sha256", "")])
    artifact = box.root / "download-src" / "oopz-capture-v9.9.9-new.zip"
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    (box.root / "sha-source").write_text(f"{digest}  oopz-capture-v9.9.9-new.zip\n", encoding="utf-8")
    result = _run_update(box)
    assert result.returncode == 0, result.stderr
    assert (box.root / "new-installer-ran").exists()


def test_update_missing_paired_checksum_aborts(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    _curl_shim(box)
    (box.root / "sha-source").write_text(("2" * 64) + "  other-file.zip\n", encoding="utf-8")
    _make_update_fixture(box, correct_checksum=True, sha_assets=[("other-file.zip.sha256", "")])
    result = _run_update(box)
    assert result.returncode != 0
    assert "paired checksum" in (result.stdout + result.stderr)
    assert not (box.root / "new-installer-ran").exists()


# --- rollback behavior ---------------------------------------------------------


def _run_rollback(box: Sandbox, *args: str, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = {**box.env, **env_extra}
    return subprocess.run(
        [_find_bash(), str(SCRIPTS / "rollback_release.sh"), "-r", box.install_root.as_posix(), *args],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(box.root), timeout=120,
    )


def test_rollback_health_failure_restores_original(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.make_installed_release("v1.0.0-now")
    box.make_installed_release("v2.0.0-target")
    box.set_current("v1.0.0-now")
    box.write_env()
    box.set_service_active(True)
    result = _run_rollback(box, "--to", "v2.0.0-target", "--health-timeout", "3", FAKE_FAIL_HEALTH="1")
    assert result.returncode != 0
    assert "health check" in (result.stdout + result.stderr)
    from pathlib import Path as _P
    assert _P(box.current_target()).name == "v1.0.0-now"
    assert box.systemd_calls()[-1] == "systemctl start oopz-capture"


def test_rollback_success_switches_without_touching_shared(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)
    box.make_installed_release("v1.0.0-now")
    box.make_installed_release("v2.0.0-target")
    box.set_current("v1.0.0-now")
    env_file = box.write_env()
    before = env_file.read_text(encoding="utf-8")
    box.set_service_active(True)
    result = _run_rollback(box, "--to", "v2.0.0-target", "--health-timeout", "3")
    assert result.returncode == 0, result.stderr
    from pathlib import Path as _P
    assert _P(box.current_target()).name == "v2.0.0-target"
    assert env_file.read_text(encoding="utf-8") == before
