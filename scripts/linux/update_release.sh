#!/usr/bin/env bash
# Update OOPZ Capture to the latest public GitHub release on Ubuntu 24.04 LTS.
# Run as root:
#   sudo bash <release>/scripts/linux/update_release.sh
#
# Queries the anonymous GitHub API for the latest release, downloads the ZIP
# and its SHA-256 into <install-root>/artifacts, and hands them to
# install_release.sh. Exits 0 without reinstalling when already current.
# Keep the server free of recordings/analyses while updating.
set -euo pipefail

REPO="XK205E3n/OOPZ_Capture"
INSTALL_ROOT="/opt/oopz"
HEALTH_TIMEOUT=120
while [[ $# -gt 0 ]]; do
  case "$1" in
    -r|--install-root) INSTALL_ROOT="${2:?--install-root needs a value}"; shift 2 ;;
    -t|--health-timeout) HEALTH_TIMEOUT="${2:?--health-timeout needs a value}"; shift 2 ;;
    -h|--help) grep '^#' "$0" | grep -v '^#!' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 3 ;;
  esac
done

log() { printf '[oopz-update] %s\n' "$*"; }
die() { printf '[oopz-update] ERROR: %s\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
command -v curl >/dev/null 2>&1 || die "curl is missing; run scripts/linux/install_prerequisites.sh"
command -v python3.12 >/dev/null 2>&1 || die "python3.12 is missing; run scripts/linux/install_prerequisites.sh"
command -v unzip >/dev/null 2>&1 || die "unzip is missing; run scripts/linux/install_prerequisites.sh"

CURRENT_MANIFEST="$INSTALL_ROOT/current/RELEASE_MANIFEST.json"
CURRENT_ID=""
if [[ -f "$CURRENT_MANIFEST" ]]; then
  CURRENT_ID=$(python3.12 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["release_id"])' "$CURRENT_MANIFEST")
fi

log "Querying the latest release of $REPO"
API_JSON_FILE=$(mktemp)
NEW_INSTALLER=""
trap 'rm -f "$API_JSON_FILE" "${NEW_INSTALLER:-}"' EXIT
curl -fsSL --retry 5 --max-time 60 -o "$API_JSON_FILE" "https://api.github.com/repos/$REPO/releases/latest" \
  || die "Could not query the GitHub releases API (check outbound access)."
mapfile -t ASSETS < <(python3.12 - "$API_JSON_FILE" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(data.get("tag_name", ""))
for asset in data.get("assets", []):
    name = asset.get("name", "")
    if name.endswith(".zip") or name.endswith(".sha256"):
        print(asset.get("browser_download_url", ""))
PY
)
TAG="${ASSETS[0]}"
ZIP_URL=""
SHA_URL=""
for url in "${ASSETS[@]:1}"; do
  if [[ "$url" == *.zip ]]; then
    ZIP_URL="$url"
  elif [[ "$url" == *.sha256 ]]; then
    SHA_URL="$url"
  fi
done
[[ -n "$TAG" && -n "$ZIP_URL" && -n "$SHA_URL" ]] || die "Latest release is missing a ZIP and .sha256 asset pair."

if [[ -n "$CURRENT_ID" && "$ZIP_URL" == *"$CURRENT_ID.zip" ]]; then
  log "Already on the latest release ($CURRENT_ID); nothing to do."
  exit 0
fi

ARTIFACT_DIR="$INSTALL_ROOT/artifacts"
mkdir -p "$ARTIFACT_DIR"
ZIP_FILE="$ARTIFACT_DIR/$(basename "$ZIP_URL")"
SHA_FILE="$ARTIFACT_DIR/$(basename "$SHA_URL")"
log "Downloading $(basename "$ZIP_URL")"
curl -fL --retry 5 --max-time 600 -o "$ZIP_FILE" "$ZIP_URL" || die "Release ZIP download failed."
curl -fL --retry 5 --max-time 60 -o "$SHA_FILE" "$SHA_URL" || die "Checksum download failed."

log "Installing $TAG with the new package's own installer (this stops the gateway briefly)"
NEW_INSTALLER="$ARTIFACT_DIR/.installer-$$.sh"
unzip -p "$ZIP_FILE" "scripts/linux/install_release.sh" > "$NEW_INSTALLER" \
  || die "Could not extract the installer from the new release ZIP."
bash "$NEW_INSTALLER" -f "$ZIP_FILE" -r "$INSTALL_ROOT" -t "$HEALTH_TIMEOUT"
