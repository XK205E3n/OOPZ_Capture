#!/usr/bin/env bash
# Transactional release installer for OOPZ Capture on Ubuntu Server 24.04 LTS x86_64.
# Run as root:
#   sudo bash scripts/linux/install_release.sh -f /opt/oopz/artifacts/<artifact>.zip
#
# Staged transaction (see docs/DEPLOYMENT_UBUNTU.md section 3):
#   PREPARE  extract, venv, Python/Node deps, Chromium, model, PDF render check.
#            Never touches the running service, `current`, or the systemd unit.
#            A failed prepare keeps the incomplete directory for diagnosis;
#            re-running the same artifact cleans and retries it.
#   ACTIVATE guard against active tasks, stop old service, write the unit,
#            switch `current`, start, and wait for the Feishu marker. Any
#            failure after the switch restores the previous link, unit and
#            service state; a failed first install leaves no `current`.
#
# Options:
#   -f <artifact.zip>       release ZIP to install (required); expects <zip>.sha256 next to it
#   -r <install-root>       installation root (default /opt/oopz)
#   -t <seconds>            health check timeout in seconds (default 120)
#   --prepare-only          stop after PREPARE (no .env required; bootstrap first stage)
#   --activate              only run ACTIVATE on an already-prepared release directory
#   --force                 skip the active-task guard (explicit operator decision)
set -euo pipefail

ARTIFACT=""
INSTALL_ROOT="/opt/oopz"
HEALTH_TIMEOUT=120
FORCE_SWITCH=0
PREPARE_ONLY=0
ACTIVATE_ONLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -f) ARTIFACT="${2:?--file needs a value}"; shift 2 ;;
    -r|--install-root) INSTALL_ROOT="${2:?--install-root needs a value}"; shift 2 ;;
    -t|--health-timeout) HEALTH_TIMEOUT="${2:?--health-timeout needs a value}"; shift 2 ;;
    --force) FORCE_SWITCH=1; shift ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --activate) ACTIVATE_ONLY=1; shift ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 3 ;;
  esac
done

log() { printf '[oopz-install] %s\n' "$*"; }
die() { printf '[oopz-install] ERROR: %s\n' "$*" >&2; exit 1; }
SERVICE_NAME="oopz-capture"
# Overridable output paths (defaults are the production system locations).
UNIT_DIR="${OOPZ_UNIT_DIR:-/etc/systemd/system}"
LOGROTATE_DIR="${OOPZ_LOGROTATE_DIR:-/etc/logrotate.d}"
READY_MARKER="飞书长连接已就绪"
BOOTSTRAP_MARKER="尚未绑定控制群"
PREPARE_MARKER=".prepare-complete"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"  # scripts/linux inside the running (trusted) release

# OOPZ_INSTALLER_SELFTEST=1 skips ONLY the uid check so the staged logic can
# run in isolated behavior tests; verification, guards and rollback stay on.
if [[ "${OOPZ_INSTALLER_SELFTEST:-}" != "1" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
fi
[[ -n "$ARTIFACT" ]] || die "Missing -f <artifact.zip>."
ARTIFACT="$(readlink -f "$ARTIFACT")"
[[ -f "$ARTIFACT" ]] || die "Artifact not found: $ARTIFACT"
[[ "$ARTIFACT" == *.zip ]] || die "Artifact must be a .zip file."

command -v unzip >/dev/null 2>&1 || die "unzip is missing; run scripts/linux/install_prerequisites.sh"
command -v python3.12 >/dev/null 2>&1 || die "python3.12 is missing; run scripts/linux/install_prerequisites.sh"
command -v runuser >/dev/null 2>&1 || die "runuser is missing; install util-linux"
command -v systemctl >/dev/null 2>&1 || die "systemctl is missing; this installer targets systemd hosts"

SHARED_ROOT="$INSTALL_ROOT/shared"
RELEASES_ROOT="$INSTALL_ROOT/releases"
CURRENT_PATH="$INSTALL_ROOT/current"
ENV_PATH="$SHARED_ROOT/config/.env"
MODEL_PATH="$SHARED_ROOT/models/SenseVoiceSmall"
RUNTIME_LOG="$SHARED_ROOT/logs/feishu_runtime.log"
GUARD="$SCRIPT_DIR/check_active_tasks.py"
VERIFY="$SCRIPT_DIR/verify_artifact.py"
CHECK_NODE="$SCRIPT_DIR/check_node.py"
for helper in "$GUARD" "$VERIFY" "$CHECK_NODE"; do
  [[ -f "$helper" ]] || die "helper script is missing from this release: $helper"
done

# Verify the artifact BEFORE reading or executing anything from it.
python3.12 "$VERIFY" "$ARTIFACT" || die "artifact verification failed; refusing to install."

for name in releases artifacts shared/config shared/models shared/output shared/feishu_state shared/logs shared/tools/node; do
  mkdir -p "$INSTALL_ROOT/$name"
done

log "Reading release manifest"
INSPECT_ROOT="$INSTALL_ROOT/.inspect-$$_$(date +%s)"
mkdir -p "$INSPECT_ROOT"
trap 'rm -rf "$INSPECT_ROOT"' EXIT
unzip -q "$ARTIFACT" -d "$INSPECT_ROOT"
[[ -f "$INSPECT_ROOT/RELEASE_MANIFEST.json" ]] || die "Release manifest is missing."
# utf-8-sig tolerates a BOM written by older builders.
RELEASE_ID=$(python3.12 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8-sig"))["release_id"])' "$INSPECT_ROOT/RELEASE_MANIFEST.json")
[[ "$RELEASE_ID" =~ ^v[0-9A-Za-z._-]+$ ]] || die "Release ID is invalid: $RELEASE_ID"
RELEASE_PATH="$RELEASES_ROOT/$RELEASE_ID"
PREVIOUS_TARGET=""
if [[ -L "$CURRENT_PATH" || -d "$CURRENT_PATH" ]]; then
  PREVIOUS_TARGET="$(readlink -f "$CURRENT_PATH")"
fi
if [[ "$ACTIVATE_ONLY" -ne 1 ]]; then
  if [[ -d "$RELEASE_PATH" ]]; then
    if [[ -f "$RELEASE_PATH/$PREPARE_MARKER" ]]; then
      log "Release $RELEASE_ID is already prepared; skipping the prepare stage."
    elif [[ "$PREVIOUS_TARGET" == "$RELEASE_PATH" ]]; then
      die "Release $RELEASE_ID is the active current target; cannot re-prepare it."
    else
      log "Removing the incomplete prepare attempt at $RELEASE_PATH and retrying."
      rm -rf "$RELEASE_PATH"
    fi
  fi
fi
if [[ "$ACTIVATE_ONLY" -eq 1 && ! -f "$RELEASE_PATH/$PREPARE_MARKER" ]]; then
  die "Release $RELEASE_ID has no completed prepare; run --prepare-only first."
fi

# ---------------------------------------------------------------------------
# PREPARE stage: no service interaction of any kind. Failures keep the
# incomplete directory (retry-safe) and leave the running system untouched.
# ---------------------------------------------------------------------------
run_prepare() {
  if [[ -f "$RELEASE_PATH/$PREPARE_MARKER" ]]; then
    return 0
  fi
  mv "$INSPECT_ROOT" "$RELEASE_PATH"
  trap - EXIT
  # git archive ZIPs carry no Unix permission bits; make the shipped scripts
  # directly executable regardless of how the artifact was extracted.
  chmod +x "$RELEASE_PATH/scripts/linux/"*.sh 2>/dev/null || true

  local NODE_BIN="$SHARED_ROOT/tools/node/node"
  [[ -e "$NODE_BIN" ]] || die "Shared Node runtime is missing: $NODE_BIN (run scripts/linux/install_prerequisites.sh)."
  log "Checking the shared Node runtime against the pnpm-lock contract"
  python3.12 "$CHECK_NODE" --node-bin "$NODE_BIN" || die "Shared Node runtime is incompatible with the locked dependencies; refusing to prepare."
  local NPX_BIN
  NPX_BIN="$(dirname "$NODE_BIN")/npx"
  [[ -e "$NPX_BIN" ]] || die "npx is missing next to the shared Node runtime: $NPX_BIN"

  log "Linking shared model/output/state/log directories into the release"
  for name in models output feishu_state logs; do
    ln -sfn "$SHARED_ROOT/$name" "$RELEASE_PATH/$name"
  done
  ln -sfn "$SHARED_ROOT/tools/node" "$RELEASE_PATH/tools/node"

  local RELEASE_PY="$RELEASE_PATH/.venv/bin/python"
  log "Creating the release virtual environment"
  python3.12 -m venv "$RELEASE_PATH/.venv"
  "$RELEASE_PY" -m pip --isolated install --index-url https://pypi.org/simple --timeout 120 --retries 5 --upgrade pip
  log "Installing Python dependencies (speech, feishu)"
  (cd "$RELEASE_PATH" && "$RELEASE_PY" -m pip --isolated install --index-url https://pypi.org/simple --timeout 120 --retries 5 -e '.[speech,feishu]') \
    || die "Python dependency installation failed (release directory kept for diagnosis; re-run to retry)."
  "$RELEASE_PY" -m pip check || die "Python dependency consistency check failed."

  log "Installing Playwright Chromium for the voice backend (runs as user oopz)"
  runuser -u oopz -- env HOME="$INSTALL_ROOT" "$RELEASE_PY" -m playwright install --no-shell chromium \
    || die "Voice Chromium installation failed (release directory kept for diagnosis)."
  "$RELEASE_PY" -m playwright install-deps chromium \
    || die "Chromium system dependencies failed to install (release directory kept for diagnosis)."
  log "Verifying Chromium launch as the service user"
  runuser -u oopz -- env HOME="$INSTALL_ROOT" "$RELEASE_PY" -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(channel='chromium',headless=True,timeout=30000); print('Voice Chromium launch OK'); b.close(); p.stop()" \
    || die "Voice Chromium launch check failed (see docs/DEPLOYMENT_UBUNTU.md section 7; release directory kept for diagnosis)."

  log "Downloading or verifying the pinned SenseVoiceSmall model"
  "$RELEASE_PY" "$RELEASE_PATH/scripts/download_sensevoice_model.py" --target "$MODEL_PATH" \
    || die "SenseVoiceSmall download or checksum verification failed (release directory kept for diagnosis)."
  [[ -f "$MODEL_PATH/model.pt" ]] || die "SenseVoiceSmall setup did not create the expected model: $MODEL_PATH"

  log "Installing locked Node dependencies with pnpm"
  export npm_config_registry=https://registry.npmjs.org
  export npm_config_fetch_retries=5
  export npm_config_fetch_retry_mintimeout=10000
  export npm_config_fetch_retry_maxtimeout=60000
  export npm_config_fetch_timeout=600000
  export npm_config_strict_ssl=true
  (cd "$RELEASE_PATH" && "$NPX_BIN" --yes pnpm@10.15.0 install --frozen-lockfile --ignore-scripts --registry=https://registry.npmjs.org --fetch-retries=5 --fetch-timeout=600000 --network-concurrency=4) \
    || die "Node dependency installation failed (release directory kept for diagnosis; re-run to retry)."
  (cd "$RELEASE_PATH" && "$NODE_BIN" --input-type=module -e "await import('md-to-pdf'); console.log('Node report dependency OK')") \
    || die "Node report dependency import failed (release directory kept for diagnosis)."

  log "Rendering a real Chinese PDF as the service user (closes the PDF browser loop)"
  local PDF_CHECK_DIR="$INSTALL_ROOT/.install-pdf-check"
  rm -rf "$PDF_CHECK_DIR"
  mkdir -p "$PDF_CHECK_DIR"
  printf '# 中文渲染检查\n\n飞书长连接、转写流水线与报告发布链路检查。\n' > "$PDF_CHECK_DIR/check.md"
  chown -R oopz:oopz "$PDF_CHECK_DIR"
  if ! runuser -u oopz -- env HOME="$INSTALL_ROOT" MD_TO_PDF_CHROME_PATH="${MD_TO_PDF_CHROME_PATH:-}" \
      "$NODE_BIN" "$RELEASE_PATH/tools/md_to_pdf.mjs" "$PDF_CHECK_DIR/check.md" "$PDF_CHECK_DIR/check.pdf" \
      >"$PDF_CHECK_DIR/out.log" 2>&1; then
    cat "$PDF_CHECK_DIR/out.log" >&2 || true
    rm -rf "$PDF_CHECK_DIR"
    die "PDF render check failed as the service user (browser/fonts/HOME; see docs/DEPLOYMENT_UBUNTU.md sections 5 and 7; release directory kept for diagnosis)."
  fi
  [[ -s "$PDF_CHECK_DIR/check.pdf" ]] || { rm -rf "$PDF_CHECK_DIR"; die "PDF render check produced an empty file."; }
  rm -rf "$PDF_CHECK_DIR"

  (cd "$RELEASE_PATH" && "$RELEASE_PY" -m pip freeze > DEPLOYED_PYTHON_PACKAGES.txt)
  "$RELEASE_PY" -c "import oopz_capture; import lark_oapi; import funasr; print('imports ok')" \
    || die "Release import smoke test failed (release directory kept for diagnosis)."

  chown -R oopz:oopz "$RELEASE_PATH"
  if [[ -d "$INSTALL_ROOT/.cache" ]]; then
    chown -R oopz:oopz "$INSTALL_ROOT/.cache"
  fi
  touch "$RELEASE_PATH/$PREPARE_MARKER"
  chown oopz:oopz "$RELEASE_PATH/$PREPARE_MARKER"
  log "Prepare stage complete for $RELEASE_ID"
}

# ---------------------------------------------------------------------------
# ACTIVATE stage: everything that can disrupt the running service.
# ---------------------------------------------------------------------------
service_is_active() {
  systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

run_guard() {
  if [[ "$FORCE_SWITCH" -eq 1 ]]; then
    log "--force given: skipping the active-task guard (operator decision)."
    return 0
  fi
  local running="no"
  service_is_active && running="yes"
  local status=0
  python3.12 "$GUARD" --state-root "$SHARED_ROOT/feishu_state" --output-root "$SHARED_ROOT/output" --service-running "$running" || status=$?
  if [[ "$status" -ne 0 ]]; then
    die "active tasks (or unreadable state) detected; stop them in the Feishu group or re-run with --force."
  fi
}

run_activate() {
  [[ -f "$ENV_PATH" ]] || die "Production config is missing: $ENV_PATH (bootstrap: run --prepare-only first, create shared config, then --activate; see docs/DEPLOYMENT_UBUNTU.md section 3.2)."
  [[ -f "$RELEASE_PATH/$PREPARE_MARKER" ]] || die "Release $RELEASE_ID is not prepared; run --prepare-only first."

  run_guard  # early, before any disruption
  ln -sfn "$ENV_PATH" "$RELEASE_PATH/.env"

  # A first start without the admin chat binding waits for the group
  # invitation instead of reaching the ready marker; treat that prompt as the
  # healthy bootstrap signal so a pending binding never triggers rollback.
  local MARKERS="$READY_MARKER"
  if ! grep -Eq '^[[:space:]]*OOPZ_FEISHU_ADMIN_CHAT_ID=oc_' "$ENV_PATH"; then
    MARKERS="$READY_MARKER|$BOOTSTRAP_MARKER"
    log "OOPZ_FEISHU_ADMIN_CHAT_ID is not set; health check accepts the first-run binding prompt."
  fi

  local SERVICE_WAS_ACTIVE=0
  service_is_active && SERVICE_WAS_ACTIVE=1
  local SWITCHED=0
  local UNIT_WRITTEN=0

  restore_previous() {
    systemctl stop "$SERVICE_NAME" 2>/dev/null || true
    if [[ "$SWITCHED" -eq 1 ]]; then
      if [[ -n "$PREVIOUS_TARGET" && -d "$PREVIOUS_TARGET" ]]; then
        ln -sfn "$PREVIOUS_TARGET" "$INSTALL_ROOT/.current.new"
        mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
      else
        rm -f "$CURRENT_PATH"
      fi
    fi
    if [[ "$UNIT_WRITTEN" -eq 1 ]]; then
      if [[ -n "$PREVIOUS_TARGET" && -f "$PREVIOUS_TARGET/scripts/linux/oopz-capture.service" ]]; then
        sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$PREVIOUS_TARGET/scripts/linux/oopz-capture.service" \
          > "$UNIT_DIR/$SERVICE_NAME.service"
      else
        rm -f "$UNIT_DIR/$SERVICE_NAME.service"
        systemctl disable "$SERVICE_NAME" >/dev/null 2>&1 || true
      fi
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
      systemctl start "$SERVICE_NAME" || true
    fi
  }
  fail_activate() { printf '[oopz-install] ERROR: %s\n' "$*" >&2; restore_previous; exit 1; }

  # Re-check immediately before stopping: tasks that started during the
  # (long) prepare stage are caught here, not just at the early guard.
  run_guard

  if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
    log "Stopping the running release"
    systemctl stop "$SERVICE_NAME" || fail_activate "old release did not stop cleanly."
  fi

  log "Installing the systemd unit and logrotate configuration"
  sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$RELEASE_PATH/scripts/linux/oopz-capture.service" \
    > "$UNIT_DIR/$SERVICE_NAME.service" || fail_activate "could not write the systemd unit."
  UNIT_WRITTEN=1
  if [[ -d "$LOGROTATE_DIR" ]]; then
    sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$RELEASE_PATH/scripts/linux/oopz-capture.logrotate" \
      > "$LOGROTATE_DIR/oopz-capture"
  fi
  systemctl daemon-reload || fail_activate "systemctl daemon-reload failed."

  log "Switching current -> $RELEASE_ID"
  ln -sfn "$RELEASE_PATH" "$INSTALL_ROOT/.current.new"
  mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
  SWITCHED=1
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true

  local OLD_LOG_SIZE=0
  if [[ -f "$RUNTIME_LOG" ]]; then
    OLD_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
  fi
  log "Starting $SERVICE_NAME and waiting up to ${HEALTH_TIMEOUT}s for the Feishu gateway"
  systemctl start "$SERVICE_NAME" || fail_activate "new release failed to start."

  local HEALTHY=0 DEADLINE=$(( SECONDS + HEALTH_TIMEOUT )) NEW_LOG_SIZE TAIL_TEXT
  while (( SECONDS < DEADLINE )); do
    sleep 2
    service_is_active || break
    if [[ -f "$RUNTIME_LOG" ]]; then
      NEW_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
      if (( NEW_LOG_SIZE > OLD_LOG_SIZE )); then
        TAIL_TEXT="$(tail -c +"$(( OLD_LOG_SIZE + 1 ))" "$RUNTIME_LOG")"
        if grep -qE "$MARKERS" <<<"$TAIL_TEXT"; then
          HEALTHY=1
          break
        fi
      fi
    fi
  done

  [[ "$HEALTHY" -eq 1 ]] || fail_activate "release health check failed; current, unit and service state were restored to '${PREVIOUS_TARGET:-<none>}'."
  log "Deployed $RELEASE_ID (previous: ${PREVIOUS_TARGET:-<none>})"
  printf '{"status": "deployed", "release_id": "%s", "path": "%s", "previous": "%s"}\n' \
    "$RELEASE_ID" "$RELEASE_PATH" "$PREVIOUS_TARGET"
}

if [[ "$ACTIVATE_ONLY" -eq 1 ]]; then
  log "Activating prepared release $RELEASE_ID"
  run_activate
elif [[ "$PREPARE_ONLY" -eq 1 ]]; then
  log "Preparing release $RELEASE_ID (service untouched)"
  run_prepare
  log "Next: create $ENV_PATH if needed, then run with --activate."
else
  run_prepare
  run_activate
fi
