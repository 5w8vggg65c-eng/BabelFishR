#!/usr/bin/env bash
# Verify a built BabelFishR.app before it is signed or shipped.
#
#   packaging/verify_bundle.sh dist/BabelFishR.app
#
# Every check here is FATAL. A bundle that cannot start is not a bundle worth
# signing, and an earlier version let the executable self-test fail through
# `|| echo`, so a completely broken build still reported success.
#
# Split out of build_macos.sh so it can be exercised directly, including on
# non-macOS hosts: the Info.plist checks are skipped where PlistBuddy does not
# exist, but the executable self-test always runs.
set -euo pipefail

APP="${1:-dist/BabelFishR.app}"
FAILURES=0

fail() { echo "FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }
pass() { echo "ok:   $*"; }

if [[ ! -d "$APP" ]]; then
  echo "FAIL: no bundle at $APP" >&2
  exit 1
fi

BINARY="$APP/Contents/MacOS/BabelFishR"
if [[ ! -x "$BINARY" ]]; then
  echo "FAIL: no executable at $BINARY" >&2
  exit 1
fi

# --- Info.plist (macOS only; PlistBuddy is not available elsewhere) --------
PLIST="$APP/Contents/Info.plist"
if [[ -x /usr/libexec/PlistBuddy ]]; then
  for key in NSMicrophoneUsageDescription CFBundleIdentifier \
             CFBundleShortVersionString; do
    if /usr/libexec/PlistBuddy -c "Print :$key" "$PLIST" >/dev/null 2>&1; then
      pass "Info.plist has $key"
    else
      fail "Info.plist lacks $key"
    fi
  done
else
  echo "skip: PlistBuddy unavailable, not checking Info.plist on this host"
fi

# --- the executable must actually start -----------------------------------
# Fatal by design: if the frozen bundle cannot import its own code, everything
# downstream (signing, notarization, shipping) is wasted effort.
if OUTPUT=$("$BINARY" --selftest-import 2>&1); then
  pass "executable self-test: $OUTPUT"
else
  STATUS=$?
  fail "executable self-test failed (exit $STATUS): $OUTPUT"
fi

if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES verification failure(s); the bundle is not usable." >&2
  exit 1
fi

echo "Bundle verification passed."
