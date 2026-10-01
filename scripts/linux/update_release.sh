#!/usr/bin/env bash
# Update OOPZ Capture to the latest public GitHub release on Ubuntu 24.04 LTS.
# Run as root:
#   sudo bash <release>/scripts/linux/update_release.sh
#
# Trust model: the ZIP and its .sha256 are downloaded first, then verified by
# THIS script (the trusted, already-installed code) before anything from the
# new package is extracted or executed. The checksum is matched to the ZIP by
# exact filename pairing (zip name + ".sha256"), not by "last asset that
# looks right". CRLF checksum files are tolerated.
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

# OOPZ_INSTALLER_SELFTEST=1 skips ONLY the uid check so the staged logic can
# run in isolated behavior tests; verification, guards and rollback stay on.
if [[ "${OOPZ_INSTALLER_SELFTEST:-}" != "1" ]]; then
  [[ "$(id -u)" -eq 0 ]] || die "Run as root (sudo)."
fi
command -v curl >/dev/null 2>&1 || die "curl is missing; run scripts/linux/install_prerequisites.sh"
command -v python3.12 >/dev/null 2>&1 || die "python3.12 is missing; run scripts/linux/install_prerequisites.sh"
command -v unzip >/dev/null 2>&1 || die "unzip is missing; run scripts/linux/install_prerequisites.sh"

CURRENT_MANIFEST="$INSTALL_ROOT/current/RELEASE_MANIFEST.json"
CURRENT_ID=""
if [[ -f "$CURRENT_MANIFEST" ]]; then
  # utf-8-sig tolerates a BOM written by older builders.
  CURRENT_ID=$(python3.12 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8-sig"))["release_id"])' "$CURRENT_MANIFEST")
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
        print(name + "\t" + asset.get("browser_download_url", ""))
PY
)
TAG="${ASSETS[0]%$'\r'}"
ZIP_URL=""
for row in "${ASSETS[@]:1}"; do
  row="${row%$'\r'}"  # tolerate CRLF stdout of the JSON helper
  name="${row%%$'\t'*}"
  url="${row#*$'\t'}"
  if [[ "$name" == *.zip ]]; then
    ZIP_URL="$url"
    ZIP_NAME="$name"
  fi
done
[[ -n "$TAG" && -n "$ZIP_URL" ]] || die "Latest release has no ZIP asset."
# Pair the checksum asset with the chosen ZIP by exact filename.
SHA_URL=""
for row in "${ASSETS[@]:1}"; do
  row="${row%$'\r'}"
  name="${row%%$'\t'*}"
  url="${row#*$'\t'}"
  if [[ "$name" == "${ZIP_NAME}.sha256" ]]; then
    SHA_URL="$url"
  fi
done
[[ -n "$SHA_URL" ]] || die "Release $TAG has no '${ZIP_NAME}.sha256' asset; refusing to update without a paired checksum."

if [[ -n "$CURRENT_ID" && "$ZIP_NAME" == "oopz-capture-$CURRENT_ID.zip" ]]; then
  log "Already on the latest release ($CURRENT_ID); nothing to do."
  exit 0
fi

ARTIFACT_DIR="$INSTALL_ROOT/artifacts"
mkdir -p "$ARTIFACT_DIR"
ZIP_FILE="$ARTIFACT_DIR/$ZIP_NAME"
SHA_FILE="$ARTIFACT_DIR/${ZIP_NAME}.sha256"
log "Downloading $ZIP_NAME and its checksum"
curl -fL --retry 5 --max-time 600 -o "$ZIP_FILE" "$ZIP_URL" || die "Release ZIP download failed."
curl -fL --retry 5 --max-time 60 -o "$SHA_FILE" "$SHA_URL" || die "Checksum download failed."

# Verify with trusted local code BEFORE extracting or executing anything from
# the new package. (Kept in sync with scripts/linux/verify_artifact.py; the
# updater carries its own copy because the helper inside the new ZIP is not
# trusted until this check passes.)
python3.12 - "$ZIP_FILE" "$SHA_FILE" <<'PY' || die "Artifact verification failed; the new package was NOT executed."
import hashlib, re, sys
artifact, checksum = sys.argv[1], sys.argv[2]
expected = None
for line in open(checksum, encoding="utf-8-sig", errors="strict").read().replace("\r\n", "\n").split("\n"):
    line = line.strip()
    match = re.match(r"^([0-9A-Fa-f]{64})\s+\*?(.+)$", line) if line else None
    if match and match.group(2).strip() == artifact.rsplit("/", 1)[-1]:
        expected = match.group(1).lower()
if expected is None:
    print(f"VERIFY-FAIL no checksum entry for {artifact}"); sys.exit(1)
digest = hashlib.sha256()
with open(artifact, "rb") as stream:
    for chunk in iter(lambda: stream.read(1 << 20), b""):
        digest.update(chunk)
actual = digest.hexdigest()
if actual != expected:
    print(f"VERIFY-FAIL expected {expected}, got {actual}"); sys.exit(1)
print("VERIFY-OK", artifact)
PY

log "Installing $TAG with the newly verified package's own installer (this stops the gateway briefly)"
NEW_INSTALLER="$ARTIFACT_DIR/.installer-$$.sh"
unzip -p "$ZIP_FILE" "scripts/linux/install_release.sh" > "$NEW_INSTALLER" \
  || die "Could not extract the installer from the verified ZIP."
bash "$NEW_INSTALLER" -f "$ZIP_FILE" -r "$INSTALL_ROOT" -t "$HEALTH_TIMEOUT"
