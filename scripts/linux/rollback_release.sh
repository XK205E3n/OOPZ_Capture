#!/usr/bin/env bash
# Roll back OOPZ Capture to a previously installed release on Ubuntu 24.04 LTS.
# Run as root:
#   sudo bash <release>/scripts/linux/rollback_release.sh                 # newest other release
#   sudo bash <release>/scripts/linux/rollback_release.sh --to v0.11.14-<commit>
#
# Refuses while a recording/analysis task is running (same guard as the
# installer) and restores the original link, unit and service state when the
# rolled-back release fails to start or pass its health check. Only code and
# dependencies switch; shared config and business data are never touched.
set -euo pipefail

INSTALL_ROOT="/opt/oopz"
TARGET_ID=""
HEALTH_TIMEOUT=120
FORCE_SWITCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --to|-t) TARGET_ID="${2:?--to needs a release id}"; shift 2 ;;
    -r|--install-root) INSTALL_ROOT="${2:?--install-root needs a value}"; shift 2 ;;
    --health-timeout) HEALTH_TIMEOUT="${2:?--health-timeout needs a value}"; shift 2 ;;
    --force) FORCE_SWITCH=1; shift ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 3 ;;
  esac
done

log() { printf '[oopz-rollback] %s\n' "$*"; }
die() { printf '[oopz-rollback] ERROR: %s\n' "$*" >&2; exit 1; }
SERVICE_NAME="oopz-capture"
# Overridable output paths (defaults are the production system locations).
UNIT_DIR="${OOPZ_UNIT_DIR:-/etc/systemd/system}"
LOGROTATE_DIR="${OOPZ_LOGROTATE_DIR:-/etc/logrotate.d}"
READY_MARKER="飞书长连接已就绪"
BOOTSTRAP_MARKER="尚未绑定控制群"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUARD="$SCRIPT_DIR/check_active_tasks.py"
[[ -f "$GUARD" ]] || die "helper script is missing from this release: $GUARD"

# OOPZ_INSTALLER_SELFTEST=1 skips ONLY the uid check so the staged logic can
# run in isolated behavior tests; verification, guards and rollback stay on.
if [[ "${OOPZ_INSTALLER_SELFTEST:-}" != "1" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
fi
command -v python3.12 >/dev/null 2>&1 || die "python3.12 is missing; run scripts/linux/install_prerequisites.sh"
command -v systemctl >/dev/null 2>&1 || die "systemctl is missing; this script targets systemd hosts"

CURRENT_PATH="$INSTALL_ROOT/current"
RELEASES_ROOT="$INSTALL_ROOT/releases"
SHARED_ROOT="$INSTALL_ROOT/shared"
RUNTIME_LOG="$SHARED_ROOT/logs/feishu_runtime.log"
ENV_PATH="$SHARED_ROOT/config/.env"

[[ -L "$CURRENT_PATH" || -d "$CURRENT_PATH" ]] || die "No current release installed under $INSTALL_ROOT."
CURRENT_ID=$(basename "$(readlink -f "$CURRENT_PATH")")
CURRENT_TARGET="$(readlink -f "$CURRENT_PATH")"

if [[ -z "$TARGET_ID" ]]; then
  TARGET_ID=$(ls -1t "$RELEASES_ROOT" | grep -v "^$CURRENT_ID$" | head -n 1 || true)
  [[ -n "$TARGET_ID" ]] || die "No other installed release found to roll back to; list $RELEASES_ROOT and pass --to <release-id>."
fi

TARGET_PATH="$RELEASES_ROOT/$TARGET_ID"
[[ -d "$TARGET_PATH" ]] || die "Release directory not found: $TARGET_PATH"
[[ -x "$TARGET_PATH/.venv/bin/python" ]] || die "Release $TARGET_ID has no virtual environment and cannot be activated."

service_is_active() {
  systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null
}

if [[ "$FORCE_SWITCH" -ne 1 ]]; then
  running="no"
  service_is_active && running="yes"
  status=0
  python3.12 "$GUARD" --state-root "$SHARED_ROOT/feishu_state" --output-root "$SHARED_ROOT/output" --service-running "$running" || status=$?
  if [[ "$status" -ne 0 ]]; then
    die "active tasks (or unreadable state) detected; stop them in the Feishu group first, or re-run with --force."
  fi
fi

[[ -f "$ENV_PATH" ]] || die "Production config is missing: $ENV_PATH"
MARKERS="$READY_MARKER"
if ! grep -Eq '^[[:space:]]*OOPZ_FEISHU_ADMIN_CHAT_ID=oc_' "$ENV_PATH"; then
  MARKERS="$READY_MARKER|$BOOTSTRAP_MARKER"
fi

SERVICE_WAS_ACTIVE=0
service_is_active && SERVICE_WAS_ACTIVE=1
UNIT_WRITTEN=0
SWITCHED=0

restore_original() {
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  if [[ "$SWITCHED" -eq 1 ]]; then
    ln -sfn "$CURRENT_TARGET" "$INSTALL_ROOT/.current.new"
    mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
  fi
  if [[ "$UNIT_WRITTEN" -eq 1 ]]; then
    if [[ -f "$CURRENT_TARGET/scripts/linux/oopz-capture.service" ]]; then
      sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$CURRENT_TARGET/scripts/linux/oopz-capture.service" \
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
fail_rollback() { printf '[oopz-rollback] ERROR: %s\n' "$*" >&2; restore_original; exit 1; }

if [[ "$SERVICE_WAS_ACTIVE" -eq 1 ]]; then
  log "Stopping $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME" || fail_rollback "service did not stop cleanly."
fi

log "Installing the systemd unit from $TARGET_ID"
sed -e "s|__INSTALL_ROOT__|$INSTALL_ROOT|g" "$TARGET_PATH/scripts/linux/oopz-capture.service" \
  > "$UNIT_DIR/$SERVICE_NAME.service" || fail_rollback "could not write the systemd unit."
UNIT_WRITTEN=1
systemctl daemon-reload || fail_rollback "systemctl daemon-reload failed."

log "Switching current: $CURRENT_ID -> $TARGET_ID"
ln -sfn "$TARGET_PATH" "$INSTALL_ROOT/.current.new"
mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
SWITCHED=1

OLD_LOG_SIZE=0
if [[ -f "$RUNTIME_LOG" ]]; then
  OLD_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
fi
systemctl start "$SERVICE_NAME" || fail_rollback "release $TARGET_ID failed to start."

HEALTHY=0
DEADLINE=$(( SECONDS + HEALTH_TIMEOUT ))
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

[[ "$HEALTHY" -eq 1 ]] || fail_rollback "release $TARGET_ID failed its health check; current, unit and service state were restored to $CURRENT_ID."
log "Rolled back to $TARGET_ID; shared config and data were not modified."
