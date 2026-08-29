#!/usr/bin/env bash
# Build the distributable disk image a non-developer can actually use.
#
#   packaging/make_dmg.sh dist/BabelFishR.app dist/BabelFishR-macOS-arm64.dmg
#
# The image contains three things:
#
#   BabelFishR.app            drag this onto Applications to install
#   Uninstall BabelFishR.app  double-click this to remove everything
#   Applications              the drop target
#
# so the whole installation is "open the DMG, drag one icon onto the other",
# and removal is "open the DMG again and double-click the other app". The
# uninstaller stays on the image; there is nothing to install for it.
#
# A .sha256 file is written beside the image, and the image is mounted once to
# prove it really contains BOTH launchable apps and the shortcut before
# anybody downloads it. A DMG missing any of the three fails this script.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "hdiutil only exists on macOS." >&2
  exit 1
fi

APP="${1:-dist/BabelFishR.app}"
DMG="${2:-dist/BabelFishR-macOS-arm64.dmg}"
UNINSTALLER="${3:-$(dirname "$APP")/Uninstall BabelFishR.app}"
VOLNAME="${DMG_VOLUME_NAME:-BabelFishR}"

[[ -d "$APP" ]] || { echo "FAIL: no bundle at $APP" >&2; exit 1; }
# Not optional: an image without the uninstaller leaves operators with no
# supported way to remove several gigabytes of models and their recordings.
[[ -d "$UNINSTALLER" ]] || {
  echo "FAIL: no uninstaller bundle at $UNINSTALLER" >&2
  echo "      Build it with: pyinstaller packaging/babelfishr.spec" >&2
  exit 1
}
mkdir -p "$(dirname "$DMG")"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

echo "==> Staging $APP"
cp -R "$APP" "$STAGE/"
echo "==> Staging $UNINSTALLER"
cp -R "$UNINSTALLER" "$STAGE/"
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
STATUS=0
MOUNTED_APP="$MOUNT/$(basename "$APP")"
if [[ -x "$MOUNTED_APP/Contents/MacOS/BabelFishR" ]]; then
  echo "ok:   the image contains $(basename "$APP") with an executable inside"
else
  echo "FAIL: the image does not contain a launchable app" >&2
  STATUS=1
fi
MOUNTED_UNINSTALLER="$MOUNT/$(basename "$UNINSTALLER")"
if [[ -x "$MOUNTED_UNINSTALLER/Contents/MacOS/UninstallBabelFishR" ]]; then
  echo "ok:   the image contains $(basename "$UNINSTALLER") with an executable inside"
else
  echo "FAIL: the image does not contain a launchable uninstaller" >&2
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
