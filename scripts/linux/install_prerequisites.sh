#!/usr/bin/env bash
# Install system prerequisites for OOPZ Capture on Ubuntu Server 24.04 LTS x86_64.
# Run as root:  sudo bash scripts/linux/install_prerequisites.sh [--install-root /opt/oopz]
#
# Installs Python 3.12, Node.js/npm, unzip, CJK fonts and the directory/user
# layout. It does NOT create application configuration and does NOT deploy any
# release; run scripts/linux/install_release.sh for that.
set -euo pipefail

INSTALL_ROOT="/opt/oopz"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-root) INSTALL_ROOT="${2:?--install-root needs a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 3 ;;
  esac
done

log() { printf '[oopz-prereq] %s\n' "$*"; }
die() { printf '[oopz-prereq] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  log "Non-Ubuntu system (${ID:-unknown}); continuing, but only Ubuntu 24.04 LTS is documented."
fi

log "Updating apt package lists"
apt-get update -y

log "Installing Python 3.12, Node.js, npm, unzip, curl and Noto CJK fonts"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv \
  nodejs npm \
  unzip curl ca-certificates \
  fonts-noto-cjk

command -v python3.12 >/dev/null 2>&1 || die "python3.12 is unavailable after installation"
command -v node >/dev/null 2>&1 || die "node is unavailable after installation"
command -v npm >/dev/null 2>&1 || die "npm is unavailable after installation"

if ! id -u oopz >/dev/null 2>&1; then
  log "Creating system user 'oopz' (home: $INSTALL_ROOT)"
  useradd --system --user-group --shell /bin/bash --home-dir "$INSTALL_ROOT" oopz
fi

log "Creating $INSTALL_ROOT directory layout"
mkdir -p "$INSTALL_ROOT/releases" "$INSTALL_ROOT/artifacts"
mkdir -p "$INSTALL_ROOT/shared/config" "$INSTALL_ROOT/shared/models" \
         "$INSTALL_ROOT/shared/output" "$INSTALL_ROOT/shared/feishu_state" \
         "$INSTALL_ROOT/shared/logs" "$INSTALL_ROOT/shared/tools/node"

node_bin="$INSTALL_ROOT/shared/tools/node/node"
if [[ ! -e "$node_bin" ]]; then
  ln -s "$(command -v node)" "$node_bin"
  log "Linked shared Node runtime: $node_bin -> $(command -v node)"
fi

if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" == "1" ]]; then
  log "NOTE: Ubuntu 24.04 restricts unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns=1)."
  log "NOTE: If the Chromium launch check fails later, see docs/DEPLOYMENT_UBUNTU.md section 'Chromium sandbox on Ubuntu 24.04'."
fi

log "Handing $INSTALL_ROOT to the 'oopz' user"
chown -R oopz:oopz "$INSTALL_ROOT"

log "Prerequisites complete."
log "Next: create $INSTALL_ROOT/shared/config/.env (copy .env.example from a release and fill it),"
log "then install a release: sudo bash <release>/scripts/linux/install_release.sh -f <artifact.zip>"
