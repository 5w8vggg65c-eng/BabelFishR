#!/usr/bin/env bash
# Build BabelFishR.app on macOS. Run from the repository root.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This builds a macOS .app and must run on macOS." >&2
  exit 1
fi

echo "==> Python: $(python3 --version)"
python3 -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate

echo "==> Installing BabelFishR and build tooling"
pip install --upgrade pip
pip install -e ".[gui,audio,asr,translate]" pyinstaller

echo "==> Running the test suite before packaging"
python -m pytest -q

echo "==> Building the app bundle"
rm -rf build dist
pyinstaller packaging/babelfishr.spec --noconfirm

APP="dist/BabelFishR.app"
[[ -d "$APP" ]] || { echo "build failed: $APP not produced" >&2; exit 1; }

echo "==> Verifying the bundle metadata"
/usr/libexec/PlistBuddy -c "Print :NSMicrophoneUsageDescription" \
  "$APP/Contents/Info.plist" >/dev/null \
  || { echo "Info.plist lacks NSMicrophoneUsageDescription" >&2; exit 1; }

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Signing with $CODESIGN_IDENTITY"
  codesign --deep --force --options runtime --timestamp \
    --entitlements packaging/entitlements.plist \
    --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo "==> To notarize:"
  echo "    ditto -c -k --keepParent \"$APP\" BabelFishR.zip"
  echo "    xcrun notarytool submit BabelFishR.zip --keychain-profile AC --wait"
  echo "    xcrun stapler staple \"$APP\""
else
  echo "==> CODESIGN_IDENTITY not set: the app is UNSIGNED."
  echo "    macOS Gatekeeper will refuse it on another Mac, and an unsigned"
  echo "    app is often denied microphone access. To open it locally:"
  echo "    right-click the app, choose Open, then confirm."
fi

echo "==> Built $APP"
