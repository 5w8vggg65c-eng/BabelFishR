#!/usr/bin/env bash
# Build the distributable disk image a non-developer can actually use.
#
#   packaging/make_dmg.sh dist/BabelFishR.app dist/BabelFishR-macOS-arm64.dmg
#
# The image contains the application and a shortcut to /Applications, so the
# whole installation is "open the DMG, drag one icon onto the other". A
# .sha256 file is written beside it, and the image is mounted once to prove it
# really contains a launchable app before anybody downloads it.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "hdiutil only exists on macOS." >&2
  exit 1
fi

APP="${1:-dist/BabelFishR.app}"
DMG="${2:-dist/BabelFishR-macOS-arm64.dmg}"
VOLNAME="${DMG_VOLUME_NAME:-BabelFishR}"

[[ -d "$APP" ]] || { echo "FAIL: no bundle at $APP" >&2; exit 1; }
mkdir -p "$(dirname "$DMG")"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> Staging $APP"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "==> Creating $DMG"
rm -f "$DMG"
hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -ov \
  "$DMG"

echo "==> Verifying the image"
hdiutil verify "$DMG"

MOUNT="$(mktemp -d)"
hdiutil attach "$DMG" -readonly -nobrowse -mountpoint "$MOUNT" >/dev/null
MOUNTED_APP="$MOUNT/$(basename "$APP")"
STATUS=0
if [[ -x "$MOUNTED_APP/Contents/MacOS/BabelFishR" ]]; then
  echo "ok:   the image contains $(basename "$APP") with an executable inside"
else
  echo "FAIL: the image does not contain a launchable app" >&2
  STATUS=1
fi
if [[ -L "$MOUNT/Applications" ]]; then
  echo "ok:   the image contains the /Applications drop target"
else
  echo "FAIL: the image has no /Applications shortcut" >&2
  STATUS=1
fi
hdiutil detach "$MOUNT" >/dev/null || true
rmdir "$MOUNT" 2>/dev/null || true
[[ "$STATUS" -eq 0 ]] || exit "$STATUS"

echo "==> Checksum"
( cd "$(dirname "$DMG")" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )
cat "$DMG.sha256"

echo
echo "==> Built $DMG"
