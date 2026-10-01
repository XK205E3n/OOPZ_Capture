#!/usr/bin/env bash
# Install system prerequisites for OOPZ Capture on Ubuntu Server 24.04 LTS x86_64.
# Run as root:  sudo bash scripts/linux/install_prerequisites.sh [--install-root /opt/oopz]
#
# Node strategy (R1): pnpm-lock.yaml's importer chain (puppeteer 25.7.0)
# requires node >=22.12.0, which Ubuntu 24.04's `apt install nodejs` (18.x)
# does not satisfy. This script therefore:
#   1. keeps an existing Node that already satisfies >=22.12.0 (linked into
#      shared/tools/node), or
#   2. installs a PINNED Node 22 LTS tarball under shared/tools/node-runtime
#      and points shared/tools/node at it. The tarball is verified against
#      the official SHASUMS256.txt fetched from the same release directory.
# Change NODE_PIN to move within the 22 LTS line (keep >=22.12.0), or bump it
# together with pnpm-lock.yaml when the contract changes.
#
# It does NOT create application configuration and does NOT deploy a release.
set -euo pipefail

INSTALL_ROOT="/opt/oopz"
NODE_PIN="v22.14.0"
NODE_MINIMUM="22.12.0"  # keep in sync with scripts/linux/check_node.py
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

log "Installing Python 3.12, unzip, curl, tar/xz and Noto CJK fonts"
# No distro nodejs/npm here: 18.x violates the locked dependency contract.
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends \
  python3.12 python3.12-venv \
  unzip curl ca-certificates tar xz-utils \
  fonts-noto-cjk

command -v python3.12 >/dev/null 2>&1 || die "python3.12 is unavailable after installation"

version_ge_minimum() {
  python3.12 - "$1" "$NODE_MINIMUM" <<'PY'
import re, sys
actual = sys.argv[1].lstrip("v")
minimum = sys.argv[2]
def parts(text):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(p) for p in match.groups()) if match else None
actual_parts, minimum_parts = parts(actual), parts(minimum)
sys.exit(0 if actual_parts and minimum_parts and actual_parts >= minimum_parts else 1)
PY
}

node_provisioned=0
if command -v node >/dev/null 2>&1; then
  SYSTEM_NODE_VERSION="$(node --version)"
  if version_ge_minimum "$SYSTEM_NODE_VERSION"; then
    log "System Node $SYSTEM_NODE_VERSION satisfies >=$NODE_MINIMUM; linking it into shared/tools/node."
    mkdir -p "$INSTALL_ROOT/shared/tools/node"
    ln -sfn "$(command -v node)" "$INSTALL_ROOT/shared/tools/node/node"
    ln -sfn "$(command -v npx)" "$INSTALL_ROOT/shared/tools/node/npx"
    node_provisioned=1
  else
    log "System Node $SYSTEM_NODE_VERSION is BELOW the >=$NODE_MINIMUM lockfile contract; installing the pinned Node $NODE_PIN runtime."
  fi
fi

if [[ "$node_provisioned" -ne 1 ]]; then
  command -v curl >/dev/null 2>&1 || die "curl is missing"
  command -v tar >/dev/null 2>&1 || die "tar is missing"
  RUNTIME_ROOT="$INSTALL_ROOT/shared/tools/node-runtime"
  ARCHIVE="node-$NODE_PIN-linux-x64"
  mkdir -p "$RUNTIME_ROOT"
  STAGE="$(mktemp -d)"
  trap 'rm -rf "$STAGE"' EXIT
  log "Downloading Node $NODE_PIN and its official SHASUMS256.txt"
  curl -fsSL --retry 5 --max-time 600 -o "$STAGE/$ARCHIVE.tar.xz" "https://nodejs.org/dist/$NODE_PIN/$ARCHIVE.tar.xz" \
    || die "Node $NODE_PIN download failed (check outbound access to nodejs.org)."
  curl -fsSL --retry 5 --max-time 60 -o "$STAGE/SHASUMS256.txt" "https://nodejs.org/dist/$NODE_PIN/SHASUMS256.txt" \
    || die "SHASUMS256.txt download failed."
  (cd "$STAGE" && grep " $ARCHIVE.tar.xz\$" SHASUMS256.txt | sha256sum -c --strict -) \
    || die "Node tarball checksum verification failed; refusing to use it."
  tar -xJf "$STAGE/$ARCHIVE.tar.xz" -C "$RUNTIME_ROOT"
  rm -rf "$STAGE"; trap - EXIT
  NODE_DIR="$RUNTIME_ROOT/$ARCHIVE"
  [[ -x "$NODE_DIR/bin/node" ]] || die "extracted Node runtime is incomplete: $NODE_DIR"
  "$NODE_DIR/bin/node" --version | grep -q "^$NODE_PIN" || die "extracted Node reports an unexpected version"
  # shared/tools/node becomes a directory symlink so node and npx resolve
  # uniformly for both provisioning modes.
  if [[ -L "$INSTALL_ROOT/shared/tools/node" ]]; then
    rm -f "$INSTALL_ROOT/shared/tools/node"
  elif [[ -d "$INSTALL_ROOT/shared/tools/node" ]]; then
    rm -rf "$INSTALL_ROOT/shared/tools/node"
  fi
  mkdir -p "$(dirname "$INSTALL_ROOT/shared/tools/node")"
  ln -sfn "$NODE_DIR" "$INSTALL_ROOT/shared/tools/node"
  log "Pinned Node $NODE_PIN installed at $NODE_DIR (shared/tools/node -> it)."
fi

if ! id -u oopz >/dev/null 2>&1; then
  log "Creating system user 'oopz' (home: $INSTALL_ROOT)"
  useradd --system --user-group --shell /bin/bash --home-dir "$INSTALL_ROOT" oopz
fi

log "Creating $INSTALL_ROOT directory layout"
mkdir -p "$INSTALL_ROOT/releases" "$INSTALL_ROOT/artifacts"
mkdir -p "$INSTALL_ROOT/shared/config" "$INSTALL_ROOT/shared/models" \
         "$INSTALL_ROOT/shared/output" "$INSTALL_ROOT/shared/feishu_state" \
         "$INSTALL_ROOT/shared/logs" "$INSTALL_ROOT/shared/tools/node"

if [[ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null || echo 0)" == "1" ]]; then
  log "NOTE: Ubuntu 24.04 restricts unprivileged user namespaces (kernel.apparmor_restrict_unprivileged_userns=1)."
  log "NOTE: If the Chromium launch check fails later, see docs/DEPLOYMENT_UBUNTU.md section 7."
fi

log "Handing $INSTALL_ROOT to the 'oopz' user"
chown -R oopz:oopz "$INSTALL_ROOT"

log "Prerequisites complete."
log "Next (bootstrap): sudo bash scripts/linux/install_release.sh -f <artifact.zip> --prepare-only"
log "then create $INSTALL_ROOT/shared/config/.env, then run the same command with --activate."
