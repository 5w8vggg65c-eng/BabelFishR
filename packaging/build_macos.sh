#!/usr/bin/env bash
# Build BabelFishR.app on macOS. Run from the repository root.
#
# The venv is created clean, so every dependency the build and the test step
# need must be installed here explicitly - including the dev extra that
# provides pytest. An earlier version installed only the runtime extras and
# then invoked pytest, which fails immediately in a clean environment.
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This builds a macOS .app and must run on macOS." >&2
  exit 1
fi

VENV="${BABELFISHR_BUILD_VENV:-.venv-build}"
SKIP_TESTS="${BABELFISHR_SKIP_TESTS:-0}"

echo "==> Python: $(python3 --version)"
rm -rf "$VENV"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> Installing runtime, GUI, engine, dev and packaging dependencies"
python -m pip install --upgrade pip wheel
# 'dev' carries pytest; 'packaging' carries pyinstaller. Both are required
# below, so both are installed here rather than assumed.
python -m pip install -e ".[gui,audio,asr,translate,dev,packaging]"

echo "==> Verifying the build toolchain is actually present"
python -c "import pytest, PyInstaller, PySide6; print('pytest', pytest.__version__)"
python -c "import PyInstaller; print('pyinstaller', PyInstaller.__version__)"

if [[ "$SKIP_TESTS" == "1" ]]; then
  echo "==> Skipping tests (BABELFISHR_SKIP_TESTS=1)"
else
  echo "==> Running the full deterministic test suite before packaging"
  # The WHOLE suite, not a marker subset. Most of the original regression
  # coverage (capture invariant, pipeline, storage, CLI, acceptance) predates
  # the `unit` marker, so `-m unit` collected well under half of it and the
  # build was verifying far less than it appeared to.
  #
  # Tests that genuinely need hardware, real models or a real dsd-neo binary
  # skip themselves honestly, so a plain run is both complete and portable.
  #
  # Field assets live in Application Support and must not be touched by a
  # build; point the suite at a scratch home so it cannot write there.
  BABELFISHR_HOME="$(mktemp -d)/BabelFishR-buildtest" python -m pytest -q
fi

echo "==> Building the app bundle"
rm -rf build dist
pyinstaller packaging/babelfishr.spec --noconfirm

APP="dist/BabelFishR.app"
[[ -d "$APP" ]] || { echo "build failed: $APP not produced" >&2; exit 1; }

echo "==> Verifying the bundle (metadata and a real launch)"
# Fatal: `set -e` plus a non-zero exit from verify_bundle.sh stops the build.
# A bundle that cannot start must never reach signing or notarization.
./packaging/verify_bundle.sh "$APP"

if [[ -n "${CODESIGN_IDENTITY:-}" ]]; then
  echo "==> Signing with $CODESIGN_IDENTITY"
  codesign --deep --force --options runtime --timestamp \
    --entitlements packaging/entitlements.plist \
    --sign "$CODESIGN_IDENTITY" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"

  if [[ -n "${NOTARY_PROFILE:-}" ]]; then
    echo "==> Notarizing with keychain profile $NOTARY_PROFILE"
    ditto -c -k --keepParent "$APP" BabelFishR.zip
    xcrun notarytool submit BabelFishR.zip --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$APP"
  else
    echo "==> NOTARY_PROFILE not set: the app is signed but NOT notarized."
  fi
else
  echo "==> CODESIGN_IDENTITY not set: the app is UNSIGNED and NOT notarized."
  echo "    Gatekeeper will refuse it on another Mac, and an unsigned app is"
  echo "    often denied microphone access. To open it locally: right-click"
  echo "    the app, choose Open, then confirm."
fi

echo
echo "==> Built $APP"
echo "    Field assets in ~/Library/Application Support/BabelFishR were not touched."
