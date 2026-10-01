#!/usr/bin/env bash
# Roll back OOPZ Capture to a previously installed release on Ubuntu 24.04 LTS.
# Run as root:
#   sudo bash <release>/scripts/linux/rollback_release.sh                 # newest other release
#   sudo bash <release>/scripts/linux/rollback_release.sh -t v0.11.14-<commit>
#
# Only switches code and dependencies; shared config and business data
# (shared/) are never touched. Refuses to switch while the controller tracks
# an active recording/analysis unless --force is given. A failed health check
# restores the previous current automatically.
set -euo pipefail

INSTALL_ROOT="/opt/oopz"
TARGET_ID=""
HEALTH_TIMEOUT=120
FORCE_SWITCH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--to) TARGET_ID="${2:?--to needs a release id}"; shift 2 ;;
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
READY_MARKER="飞书长连接已就绪"

[[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
CURRENT_PATH="$INSTALL_ROOT/current"
RELEASES_ROOT="$INSTALL_ROOT/releases"
SHARED_ROOT="$INSTALL_ROOT/shared"
RUNTIME_LOG="$SHARED_ROOT/logs/feishu_runtime.log"

[[ -L "$CURRENT_PATH" || -d "$CURRENT_PATH" ]] || die "No current release installed under $INSTALL_ROOT."
CURRENT_ID=$(basename "$(readlink -f "$CURRENT_PATH")")

if [[ -z "$TARGET_ID" ]]; then
  TARGET_ID=$(ls -1t "$RELEASES_ROOT" | grep -v "^$CURRENT_ID$" | head -n 1 || true)
  [[ -n "$TARGET_ID" ]] || die "No other installed release found to roll back to; list $RELEASES_ROOT and pass -t <release-id>."
fi

TARGET_PATH="$RELEASES_ROOT/$TARGET_ID"
[[ -d "$TARGET_PATH" ]] || die "Release directory not found: $TARGET_PATH"
[[ -x "$TARGET_PATH/.venv/bin/python" ]] || die "Release $TARGET_ID has no virtual environment and cannot be activated."

if [[ "$FORCE_SWITCH" -ne 1 ]]; then
  CONTROLLER_STATE="$SHARED_ROOT/feishu_state/controller.json"
  if [[ -f "$CONTROLLER_STATE" ]]; then
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
fi

if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
  log "Stopping $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME"
fi

log "Switching current: $CURRENT_ID -> $TARGET_ID"
ln -sfn "$TARGET_PATH" "$INSTALL_ROOT/.current.new"
mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"

OLD_LOG_SIZE=0
if [[ -f "$RUNTIME_LOG" ]]; then
  OLD_LOG_SIZE=$(stat -c%s "$RUNTIME_LOG")
fi
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
  log "Health check failed; restoring $CURRENT_ID"
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  ln -sfn "$RELEASES_ROOT/$CURRENT_ID" "$INSTALL_ROOT/.current.new"
  mv -Tf "$INSTALL_ROOT/.current.new" "$CURRENT_PATH"
  systemctl start "$SERVICE_NAME" || true
  die "Rolled-back release $TARGET_ID failed its health check; current was restored to $CURRENT_ID."
fi

log "Rolled back to $TARGET_ID; shared config and data were not modified."
