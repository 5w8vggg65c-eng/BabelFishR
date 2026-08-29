#!/usr/bin/env bash
# Sign (and, when possible, notarize) BabelFishR.app - and say honestly which
# of the two happened.
#
#   packaging/sign_macos.sh dist/BabelFishR.app [report-file]
#
# Two supported paths:
#
#   1. Developer ID.  Set CODESIGN_IDENTITY to the certificate's common name.
#      Every nested framework and native library is signed, then the bundle,
#      with the hardened runtime and packaging/entitlements.plist. If
#      notarization credentials are also present the image is submitted,
#      the ticket is stapled, and the result is verified.
#
#   2. Ad-hoc.  With no certificate the bundle is still signed, with "-", so
#      it has a stable code identity and macOS can attach a microphone
#      permission grant to it. This is NOT an Apple-notarized build and this
#      script never claims it is: Gatekeeper will warn on first launch and the
#      operator has to right-click the app and choose Open once.
#
# Notarization needs EITHER NOTARY_PROFILE (a stored `notarytool store-credentials`
# profile) OR all three of APPLE_ID / APPLE_TEAM_ID / APPLE_APP_PASSWORD.
set -euo pipefail

APP="${1:-dist/BabelFishR.app}"
REPORT="${2:-dist/signing-status.txt}"
ENTITLEMENTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/entitlements.plist"

[[ -d "$APP" ]] || { echo "FAIL: no bundle at $APP" >&2; exit 1; }
[[ -f "$ENTITLEMENTS" ]] || { echo "FAIL: no entitlements at $ENTITLEMENTS" >&2; exit 1; }
mkdir -p "$(dirname "$REPORT")"

log() { echo "$*" | tee -a "$REPORT"; }
: > "$REPORT"

IDENTITY="${CODESIGN_IDENTITY:-}"
MODE="ad-hoc"
if [[ -n "$IDENTITY" ]]; then
  MODE="developer-id"
else
  IDENTITY="-"
fi

log "BabelFishR signing report"
log "bundle          : $APP"
log "date            : $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
log "signing mode    : $MODE"
if [[ "$MODE" == "developer-id" ]]; then
  log "signing identity: $CODESIGN_IDENTITY"
else
  log "signing identity: - (ad-hoc; no Apple Developer ID certificate was available)"
fi

# Nested code first: codesign refuses to seal a bundle whose contents change
# after it has been signed.
echo "==> Signing embedded frameworks and native libraries"
NESTED=0
while IFS= read -r -d '' lib; do
  codesign --force --timestamp=none --options runtime \
           --sign "$IDENTITY" "$lib" >/dev/null 2>&1 \
    || codesign --force --options runtime --sign "$IDENTITY" "$lib"
  NESTED=$((NESTED + 1))
done < <(find "$APP" -type f \( -name '*.dylib' -o -name '*.so' \) -print0)

while IFS= read -r -d '' framework; do
  codesign --force --options runtime --sign "$IDENTITY" "$framework"
  NESTED=$((NESTED + 1))
done < <(find "$APP" -type d -name '*.framework' -print0)
log "nested items signed: $NESTED"

echo "==> Signing the application bundle"
TIMESTAMP_FLAG=(--timestamp)
[[ "$MODE" == "developer-id" ]] || TIMESTAMP_FLAG=(--timestamp=none)
codesign --force --options runtime "${TIMESTAMP_FLAG[@]}" \
  --entitlements "$ENTITLEMENTS" \
  --sign "$IDENTITY" "$APP"

echo "==> Verifying the signature"
codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 | tee -a "$REPORT"
{
  echo "--- codesign -dv ---"
  codesign -dv --verbose=4 "$APP" 2>&1 || true
  echo "--- entitlements as sealed ---"
  codesign -d --entitlements - --xml "$APP" 2>/dev/null || true
} >> "$REPORT"

if ! codesign -d --entitlements - --xml "$APP" 2>/dev/null \
     | grep -q "com.apple.security.device.audio-input"; then
  log "WARNING: the audio-input entitlement is not present in the sealed signature"
fi

NOTARIZED="no"
if [[ "$MODE" == "developer-id" ]]; then
  SUBMIT=()
  if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    SUBMIT=(--keychain-profile "$NOTARY_PROFILE")
  elif [[ -n "${APPLE_ID:-}" && -n "${APPLE_TEAM_ID:-}" && -n "${APPLE_APP_PASSWORD:-}" ]]; then
    SUBMIT=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID"
            --password "$APPLE_APP_PASSWORD")
  fi
  if [[ "${#SUBMIT[@]}" -gt 0 ]]; then
    echo "==> Notarizing"
    ZIP="$(dirname "$APP")/BabelFishR-notarize.zip"
    ditto -c -k --keepParent "$APP" "$ZIP"
    # Credentials are in the argv of this call only; nothing is echoed.
    if xcrun notarytool submit "$ZIP" "${SUBMIT[@]}" --wait 2>&1 \
         | sed -E 's/(password|--password)[[:space:]]+[^[:space:]]+/\1 ****/g' \
         | tee -a "$REPORT" | grep -q "status: Accepted"; then
      xcrun stapler staple "$APP" 2>&1 | tee -a "$REPORT"
      xcrun stapler validate "$APP" 2>&1 | tee -a "$REPORT"
      NOTARIZED="yes"
    else
      log "FAIL: notarization was attempted and did not succeed"
      rm -f "$ZIP"
      exit 1
    fi
    rm -f "$ZIP"
  else
    log "notarization    : SKIPPED - a Developer ID certificate was used but no"
    log "                  notarization credentials (NOTARY_PROFILE, or APPLE_ID +"
    log "                  APPLE_TEAM_ID + APPLE_APP_PASSWORD) were provided."
  fi
else
  log "notarization    : NOT POSSIBLE - notarization requires an Apple Developer"
  log "                  ID certificate. This build is ad-hoc signed only."
fi

log "notarized       : $NOTARIZED"
if [[ "$NOTARIZED" != "yes" ]]; then
  log ""
  log "UNNOTARIZED ALPHA. macOS will refuse to open this app on first launch"
  log "with a message about an unidentified developer. To open it anyway:"
  log "right-click (or Control-click) BabelFishR in Applications, choose Open,"
  log "then click Open in the dialog. This is only needed once."
fi

echo
echo "Signing report written to $REPORT"
