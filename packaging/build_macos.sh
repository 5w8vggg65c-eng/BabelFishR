#!/usr/bin/env bash
# Build BabelFishR.app on macOS. Run from the repository root.
#
# The venv is created clean, so every dependency the build and the test step
# need must be installed here explicitly - including the dev extra that
# provides pytest. An earlier version installed only the runtime extras and
# then invoked pytest, which fails immediately in a clean environment.
#
# Environment:
#   BABELFISHR_BUILD_VENV   venv location (default .venv-build)
#   BABELFISHR_SKIP_TESTS   1 to skip the suite (never set this for a release)
#   BABELFISHR_REPORT_DIR   where the test report and build log are written
#   BABELFISHR_MAKE_DMG     1 to also build the distributable disk image
#   CODESIGN_IDENTITY       Developer ID common name; unset means ad-hoc
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This builds a macOS .app and must run on macOS." >&2
  exit 1
fi

ARCH="$(uname -m)"
echo "==> Architecture: $ARCH"
if [[ "$ARCH" != "arm64" ]]; then
  echo "WARNING: building on $ARCH. The Apple Silicon release must be built on" >&2
  echo "         an arm64 machine; do not label this artifact arm64." >&2
fi

# Phase timings, printed as the build goes. Without these, "the build step
# took 27 minutes" is all anyone knows, which is not enough to act on.
BUILD_STARTED="$(date +%s)"
phase() {
  printf '==> [%5ss] %s\n' "$(( $(date +%s) - BUILD_STARTED ))" "$*"
}

VENV="${BABELFISHR_BUILD_VENV:-.venv-build}"
SKIP_TESTS="${BABELFISHR_SKIP_TESTS:-0}"
REPORT_DIR="${BABELFISHR_REPORT_DIR:-build-reports}"
mkdir -p "$REPORT_DIR"
REPORT_DIR="$(cd "$REPORT_DIR" && pwd)"

phase "Python: $(python3 --version)"
rm -rf "$VENV"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"

phase "Installing runtime, GUI, engine, dev and packaging dependencies"
python -m pip install --upgrade pip wheel
# 'dev' carries pytest; 'packaging' carries pyinstaller. Both are required
# below, so both are installed here rather than assumed.
python -m pip install -e ".[gui,audio,asr,translate,dev,packaging]"

phase "Verifying the build toolchain is actually present"
python -c "import pytest, PyInstaller, PySide6; print('pytest', pytest.__version__)"
python -c "import PyInstaller; print('pyinstaller', PyInstaller.__version__)"
python -m pip freeze > "$REPORT_DIR/build-environment.txt"

if [[ "$SKIP_TESTS" == "1" ]]; then
  echo "==> Skipping tests (BABELFISHR_SKIP_TESTS=1)"
  echo "TESTS SKIPPED - BABELFISHR_SKIP_TESTS=1" > "$REPORT_DIR/test-report.txt"
else
  phase "Running the full deterministic test suite before packaging"
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
  #
  # `set -o pipefail` is on, so a failing pytest still fails the build even
  # though its output goes through tee.
  # --timeout turns a hung test into a failure naming the test, rather than a
  # build step that sits there. --durations shows the slow ones either way.
  BABELFISHR_HOME="$(mktemp -d)/BabelFishR-buildtest" python -m pytest -q -rs \
    --timeout="${BABELFISHR_TEST_TIMEOUT:-120}" --timeout-method=thread \
    --durations=15 \
    --junitxml="$REPORT_DIR/junit.xml" 2>&1 | tee "$REPORT_DIR/test-report.txt"
fi

phase "Building the app bundle"
rm -rf build dist
pyinstaller packaging/babelfishr.spec --noconfirm

APP="dist/BabelFishR.app"
[[ -d "$APP" ]] || { echo "build failed: $APP not produced" >&2; exit 1; }

phase "Verifying the bundle (metadata and a real launch)"
# Fatal: `set -e` plus a non-zero exit from verify_bundle.sh stops the build.
# A bundle that cannot start must never reach signing or notarization.
./packaging/verify_bundle.sh "$APP" 2>&1 | tee "$REPORT_DIR/bundle-verification.txt"

phase "Signing"
# Always signed: with a Developer ID when one is configured, ad-hoc otherwise.
# An unsigned bundle has no stable code identity, so macOS cannot remember a
# microphone permission grant for it.
./packaging/sign_macos.sh "$APP" "$REPORT_DIR/signing-status.txt"

phase "Proving the signed bundle is standalone"
# After signing, because signing is the last thing that can break a bundle.
./packaging/verify_independence.sh "$APP" 2>&1 \
  | tee "$REPORT_DIR/independence-report.txt"

if [[ "${BABELFISHR_MAKE_DMG:-0}" == "1" ]]; then
  phase "Building the disk image"
  ./packaging/make_dmg.sh "$APP" "dist/BabelFishR-macOS-${ARCH}.dmg"
fi

echo
phase "Built $APP"
echo "    Reports in $REPORT_DIR"
echo "    Field assets in ~/Library/Application Support/BabelFishR were not touched."
