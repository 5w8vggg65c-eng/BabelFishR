#!/usr/bin/env bash
# Prove that a built BabelFishR.app is genuinely standalone.
#
#   packaging/verify_independence.sh dist/BabelFishR.app
#
# The bundle is exercised the way a stranger's Mac would exercise it: from a
# working directory outside the repository, with PYTHONPATH / PYTHONHOME /
# VIRTUAL_ENV removed and a minimal PATH, so nothing on the build machine can
# quietly satisfy an import. Every check is fatal.
set -euo pipefail

APP="${1:-dist/BabelFishR.app}"
[[ -d "$APP" ]] || { echo "FAIL: no bundle at $APP" >&2; exit 1; }
APP="$(cd "$APP" && pwd)"
BINARY="$APP/Contents/MacOS/BabelFishR"
[[ -x "$BINARY" ]] || { echo "FAIL: no executable at $BINARY" >&2; exit 1; }

FAILURES=0
fail() { echo "FAIL: $*" >&2; FAILURES=$((FAILURES + 1)); }
pass() { echo "ok:   $*"; }

# A deliberately hostile environment: if the app needs any of this, it is not
# standalone. HOME stays because the app legitimately writes to Application
# Support; TMPDIR stays because PyInstaller unpacks there.
run_isolated() {
  (cd / && env -i \
      PATH=/usr/bin:/bin:/usr/sbin:/sbin \
      HOME="$HOME" \
      TMPDIR="${TMPDIR:-/tmp}" \
      "$BINARY" "$@")
}

# The same, with a hard wall-clock limit. macOS has no GNU `timeout`, so this
# is done by hand. Used for the checks that talk to system daemons: a build
# must fail in a minute, not sit on a runner for two hours because coreaudiod
# never answered.
run_isolated_limited() {
  local limit="$1"; shift
  local output_file status
  output_file="$(mktemp)"
  ( run_isolated "$@" >"$output_file" 2>&1 ) &
  local pid=$!
  ( sleep "$limit"; kill -9 "$pid" 2>/dev/null ) 2>/dev/null &
  local watcher=$!
  wait "$pid"; status=$?
  kill "$watcher" 2>/dev/null || true
  cat "$output_file"
  rm -f "$output_file"
  return "$status"
}

echo "== running the frozen app from / with a scrubbed environment =="
for flag in --version --help --selftest-import; do
  if OUTPUT=$(run_isolated "$flag" 2>&1); then
    pass "$flag -> $(echo "$OUTPUT" | head -n 1)"
  else
    fail "$flag failed: $OUTPUT"
  fi
done

echo
echo "== import origins, Qt plugin paths =="
if OUTPUT=$(run_isolated --selftest-independence 2>&1); then
  echo "$OUTPUT"
  pass "no required module resolves outside the bundle"
else
  echo "$OUTPUT"
  fail "the frozen app loads code from outside its own bundle"
fi

echo
echo "== CoreAudio, from inside the bundle =="
# The only place in this project where the real CoreAudio ABI is exercised.
# It proves the frameworks load and the property selectors and struct layout
# are right on this machine. It does NOT prove audio capture works: a hosted
# runner has no audio hardware, so zero devices is the expected result there.
if OUTPUT=$(run_isolated_limited 90 --selftest-coreaudio); then
  echo "$OUTPUT"
  pass "CoreAudio ABI check"
else
  STATUS=$?
  echo "$OUTPUT"
  if [[ "$STATUS" -eq 137 ]]; then
    fail "the CoreAudio check did not finish within 90s and was killed"
  else
    fail "the bundled app could not use CoreAudio (exit $STATUS)"
  fi
fi

echo
echo "== a real, verified HTTPS request from inside the bundle =="
# The only proof that the shipped app can actually download anything. A
# certificate failure here is fatal: it is precisely the defect that made every
# Argos route fail on a real Mac. No egress at all is reported and tolerated,
# because a sandboxed runner must not masquerade as a broken bundle.
if OUTPUT=$(run_isolated_limited 90 --selftest-https); then
  echo "$OUTPUT"
  pass "HTTPS trust store"
else
  echo "$OUTPUT"
  fail "the bundled app cannot verify certificates"
fi

echo
echo "== every Argos directory resolves inside Application Support =="
# Fatal. Argos resolves its data, config and cache roots at import time, so a
# bundle that imports it before those roots are set writes an index and a
# download cache into three folders in the operator's home that BabelFishR
# neither manages nor removes.
if OUTPUT=$(run_isolated_limited 90 --selftest-argos-paths); then
  echo "$OUTPUT"
  pass "Argos directories are managed"
else
  echo "$OUTPUT"
  fail "Argos writes outside the managed Application Support root"
fi

echo
echo "== main window construction (offscreen) =="
if OUTPUT=$(run_isolated --selftest-gui 2>&1); then
  pass "$(echo "$OUTPUT" | tail -n 1)"
else
  echo "$OUTPUT"
  fail "the main window did not open offscreen"
fi

echo
echo "== native libraries must not reference build-machine paths =="
if command -v otool >/dev/null 2>&1; then
  # /usr/lib and /System are OS-provided and present on every Mac.
  # @rpath, @loader_path and @executable_path are bundle-relative.
  # Anything else absolute came from the machine that did the build.
  BAD=$(find "$APP" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 \
        | xargs -0 -n 25 otool -L 2>/dev/null \
        | grep -E '^\s+/' \
        | grep -vE '^\s+(/usr/lib/|/System/)' \
        | sort -u || true)
  if [[ -n "$BAD" ]]; then
    echo "$BAD"
    fail "native libraries reference absolute paths outside the OS"
  else
    pass "every native dependency is bundle-relative or an OS library"
  fi
else
  echo "skip: otool unavailable on this host"
fi

echo
if [[ "$FAILURES" -gt 0 ]]; then
  echo "$FAILURES independence failure(s); this bundle is not standalone." >&2
  exit 1
fi
echo "Independence verification passed."
