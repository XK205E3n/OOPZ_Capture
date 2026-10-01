#!/usr/bin/env bash
# Transactional release installer for OOPZ Capture on Ubuntu Server 24.04 LTS x86_64.
# Run as root:
#   sudo bash scripts/linux/install_release.sh -f /opt/oopz/artifacts/oopz-capture-v<version>-<commit>.zip
#
# Mirrors scripts/install_release.ps1: verify SHA-256, extract, create an
# isolated venv, install Python/Node dependencies and the pinned SenseVoice
# model, link shared data, switch `current`, start the systemd service and
# wait for the Feishu long-connection ready marker. Any failure before the
# health check leaves `current` and shared data untouched.
#
# Options:
#   -f <artifact.zip>       release ZIP to install (required); expects <zip>.sha256 next to it
#   -r <install-root>       installation root (default /opt/oopz)
#   -t <seconds>            health check timeout in seconds (default 120)
#   --force                 switch even when an active recording/analysis is tracked in controller state
set -euo pipefail

ARTIFACT=""
INSTALL_ROOT="/opt/oopz"
HEALTH_TIMEOUT=120
FORCE_SWITCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f) ARTIFACT="${2:?--file needs a value}"; shift 2 ;;
    -r|--install-root) INSTALL_ROOT="${2:?--install-root needs a value}"; shift 2 ;;
    -t|--health-timeout) HEALTH_TIMEOUT="${2:?--health-timeout needs a value}"; shift 2 ;;
    --force) FORCE_SWITCH=1; shift ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 3 ;;
  esac
done

log() { printf '[oopz-install] %s\n' "$*"; }
die() { printf '[oopz-install] ERROR: %s\n' "$*" >&2; exit 1; }
SERVICE_NAME="oopz-capture"
READY_MARKER="飞书长连接已就绪"

[[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
[[ -n "$ARTIFACT" ]] || die "Missing -f <artifact.zip>."
ARTIFACT="$(readlink -f "$ARTIFACT")"
[[ -f "$ARTIFACT" ]] || die "Artifact not found: $ARTIFACT"
[[ "$ARTIFACT" == *.zip ]] || die "Artifact must be a .zip file."
CHECKSUM="$ARTIFACT.sha256"
[[ -f "$CHECKSUM" ]] || die "Checksum file not found: $CHECKSUM"

command -v unzip >/dev/null 2>&1 || die "unzip is missing; run scripts/linux/install_prerequisites.sh"
command -v python3.12 >/dev/null 2>&1 || die "python3.12 is missing; run scripts/linux/install_prerequisites.sh"
command -v npx >/dev/null 2>&1 || die "npx is missing; run scripts/linux/install_prerequisites.sh"
command -v runuser >/dev/null 2>&1 || die "runuser is missing; install util-linux"

log "Verifying SHA-256 of $(basename "$ARTIFACT")"
# Strip CR so checksum files produced with CRLF line endings still verify;
# current builders write LF, older artifacts may not.
(cd "$(dirname "$ARTIFACT")" && sed 's/\r$//' "$(basename "$CHECKSUM")" | sha256sum -c --strict -) \
  || die "SHA-256 mismatch; refusing to install the artifact."

SHARED_ROOT="$INSTALL_ROOT/shared"
RELEASES_ROOT="$INSTALL_ROOT/releases"
CURRENT_PATH="$INSTALL_ROOT/current"
ENV_PATH="$SHARED_ROOT/config/.env"
MODEL_PATH="$SHARED_ROOT/models/SenseVoiceSmall"
RUNTIME_LOG="$SHARED_ROOT/logs/feishu_runtime.log"

[[ -f "$ENV_PATH" ]] || die "Production config is missing: $ENV_PATH (create it from .env.example first; see docs/DEPLOYMENT_UBUNTU.md)."
for name in releases artifacts shared/config shared/models shared/output shared/feishu_state shared/logs shared/tools/node; do
  mkdir -p "$INSTALL_ROOT/$name"
done

# Refuse to switch while the controller tracks an active recording/analysis.
CONTROLLER_STATE="$SHARED_ROOT/feishu_state/controller.json"
if [[ -f "$CONTROLLER_STATE" && "$FORCE_SWITCH" -ne 1 ]]; then
  ACTIVE=$(python3.12 - "$CONTROLLER_STATE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
    print("yes" if isinstance(value, dict) and value.get("active") else "no")
except Exception:
    print("no")
PY
)
  [[ "$ACTIVE" == "no" ]] || die "controller state tracks an active recording/analysis; stop it in the Feishu group first, or re-run with --force."
fi

log "Reading release manifest"
INSPECT_ROOT="$INSTALL_ROOT/.inspect-$$_$(date +%s)"
mkdir -p "$INSPECT_ROOT"
trap 'rm -rf "$INSPECT_ROOT"' EXIT
unzip -q "$ARTIFACT" -d "$INSPECT_ROOT"
[[ -f "$INSPECT_ROOT/RELEASE_MANIFEST.json" ]] || die "Release manifest is missing."
RELEASE_ID=$(python3.12 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["release_id"])' "$INSPECT_ROOT/RELEASE_MANIFEST.json")
[[ "$RELEASE_ID" =~ ^v[0-9A-Za-z._-]+$ ]] || die "Release ID is invalid: $RELEASE_ID"
RELEASE_PATH="$RELEASES_ROOT/$RELEASE_ID"
if [[ -e "$RELEASE_PATH" ]]; then
  die "Release is already installed: $RELEASE_ID"
fi
mv "$INSPECT_ROOT" "$RELEASE_PATH"
trap - EXIT
# git archive ZIPs carry no Unix permission bits; make the shipped scripts
# directly executable regardless of how the artifact was extracted.
chmod +x "$RELEASE_PATH/scripts/linux/"*.sh 2>/dev/null || true
log "Release extracted to $RELEASE_PATH"

PREVIOUS_TARGET=""
if [[ -L "$CURRENT_PATH" || -d "$CURRENT_PATH" ]]; then
  PREVIOUS_TARGET="$(readlink -f "$CURRENT_PATH")"
fi

rollback() {
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  if [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]]; then
    log "Restoring previous release: $PREVIOUS_TARGET"
    ln -sfn "$PREVIOUS_TARGET" "$INSTALL_ROOT/.current.new"
    mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
    systemctl start "$SERVICE_NAME" || true
  fi
}

fail() { printf '[oopz-install] ERROR: %s\n' "$*" >&2; rollback; exit 1; }

log "Linking shared config, data and Node runtime into the release"
ln -sfn "$ENV_PATH" "$RELEASE_PATH/.env"
for name in models output feishu_state logs; do
  ln -sfn "$SHARED_ROOT/$name" "$RELEASE_PATH/$name"
done
NODE_BIN="$SHARED_ROOT/tools/node/node"
[[ -e "$NODE_BIN" ]] || die "Shared Node runtime is missing: $NODE_BIN (run scripts/linux/install_prerequisites.sh)."
ln -sfn "$SHARED_ROOT/tools/node" "$RELEASE_PATH/tools/node"

RELEASE_PY="$RELEASE_PATH/.venv/bin/python"
log "Creating the release virtual environment"
python3.12 -m venv "$RELEASE_PATH/.venv"
"$RELEASE_PY" -m pip --isolated install --index-url https://pypi.org/simple --timeout 120 --retries 5 --upgrade pip
log "Installing Python dependencies (speech, feishu)"
(cd "$RELEASE_PATH" && "$RELEASE_PY" -m pip --isolated install --index-url https://pypi.org/simple --timeout 120 --retries 5 -e '.[speech,feishu]') \
  || fail "Python dependency installation failed."
"$RELEASE_PY" -m pip check || fail "Python dependency consistency check failed."

log "Installing Playwright Chromium for the voice backend (runs as user oopz)"
runuser -u oopz -- env HOME="$INSTALL_ROOT" "$RELEASE_PY" -m playwright install --no-shell chromium \
  || fail "Voice Chromium installation failed; current was not switched."
"$RELEASE_PY" -m playwright install-deps chromium \
  || fail "Chromium system dependencies failed to install; current was not switched."
log "Verifying Chromium launch as the service user"
runuser -u oopz -- env HOME="$INSTALL_ROOT" "$RELEASE_PY" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel='chromium',headless=True,timeout=30000); print('Voice Chromium launch OK'); b.close(); p.stop()" \
  || fail "Voice Chromium launch check failed (see docs/DEPLOYMENT_UBUNTU.md section 'Chromium sandbox on Ubuntu 24.04'); current was not switched."

log "Downloading or verifying the pinned SenseVoiceSmall model"
"$RELEASE_PY" "$RELEASE_PATH/scripts/download_sensevoice_model.py" --target "$MODEL_PATH" \
  || fail "SenseVoiceSmall download or checksum verification failed."
[[ -f "$MODEL_PATH/model.pt" ]] || fail "SenseVoiceSmall setup did not create the expected model: $MODEL_PATH"

log "Installing locked Node dependencies with pnpm"
export npm_config_registry=https://registry.npmjs.org
export npm_config_fetch_retries=5
export npm_config_fetch_retry_mintimeout=10000
export npm_config_fetch_retry_maxtimeout=60000
export npm_config_fetch_timeout=600000
export npm_config_strict_ssl=true
(cd "$RELEASE_PATH" && npx --yes pnpm@10.15.0 install --frozen-lockfile --ignore-scripts --registry=https://registry.npmjs.org --fetch-retries=5 --fetch-timeout=600000 --network-concurrency=4) \
  || fail "Node dependency installation failed; preserve this release and shared data for diagnosis."
"$NODE_BIN" --input-type=module -e "await import('md-to-pdf'); console.log('Node report dependency OK')" \
  || fail "Node report dependency import failed."

(cd "$RELEASE_PATH" && "$RELEASE_PY" -m pip freeze > DEPLOYED_PYTHON_PACKAGES.txt)
"$RELEASE_PY" -c "import oopz_capture; import lark_oapi; import funasr; print('imports ok')" \
  || fail "Release import smoke test failed."

log "Installing the systemd unit and logrotate configuration"
sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$RELEASE_PATH/scripts/linux/oopz-capture.service" \
  > "/etc/systemd/system/$SERVICE_NAME.service"
if [[ -d /etc/logrotate.d ]]; then
  sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$RELEASE_PATH/scripts/linux/oopz-capture.logrotate" \
    > /etc/logrotate.d/oopz-capture
fi
systemctl daemon-reload

chown -R oopz:oopz "$RELEASE_PATH"
if [[ -d "$INSTALL_ROOT/.cache" ]]; then
  chown -R oopz:oopz "$INSTALL_ROOT/.cache"
fi

log "Stopping the previous release and switching current -> $RELEASE_ID"
if [[ -n "$PREVIOUS_TARGET" ]] && systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  systemctl stop "$SERVICE_NAME"
fi
ln -sfn "$RELEASE_PATH" "$INSTALL_ROOT/.current.new"
mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"

OLD_LOG_SIZE=0
if [[ -f "$RUNTIME_LOG" ]]; then
  OLD_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
fi
systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
log "Starting $SERVICE_NAME and waiting up to ${HEALTH_TIMEOUT}s for the Feishu long connection"
systemctl start "$SERVICE_NAME"

HEALTHY=0
DEADLINE=$(( SECONDS + HEALTH_TIMEOUT ))
while (( SECONDS < DEADLINE )); do
  sleep 2
  if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    break
  fi
  if [[ -f "$RUNTIME_LOG" ]]; then
    NEW_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
    if (( NEW_LOG_SIZE > OLD_LOG_SIZE )); then
      if tail -c +"$(( OLD_LOG_SIZE + 1 ))" "$RUNTIME_LOG" | grep -q "$READY_MARKER"; then
        HEALTHY=1
        break
      fi
    fi
  fi
done

if [[ "$HEALTHY" -ne 1 ]]; then
  fail "Release health check failed; current was restored to '${PREVIOUS_TARGET:-<none>}'."
fi

log "Deployed $RELEASE_ID (previous: ${PREVIOUS_TARGET:-<none>})"
printf '{"status": "deployed", "release_id": "%s", "path": "%s", "previous": "%s"}\n' \
  "$RELEASE_ID" "$RELEASE_PATH" "$PREVIOUS_TARGET"
