#!/usr/bin/env bash
# Regenerate packaging/requirements-macos-arm64.txt, the hash-pinned lock
# build_macos.sh installs. Run from the repository root on any OS: uv resolves
# for the release target (macOS arm64, CPython 3.12), not for this machine.
#
# Every version and sha256 comes from PyPI via uv; nothing is typed by hand.
# Review the diff before committing it. See packaging/SUPPLY_CHAIN.md.
#
# MACOSX_DEPLOYMENT_TARGET matches the release runner (macos-26), so the
# resolution is the one pip on that runner would make: some wheels (PySide6
# 6.12, recent torch) are tagged for newer macOS than uv's default of 12.0.
set -euo pipefail

command -v uv >/dev/null 2>&1 || {
  echo "uv is required: https://docs.astral.sh/uv/" >&2; exit 1; }

MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-26.0}" \
uv pip compile packaging/requirements-macos-arm64.in \
  --python-platform aarch64-apple-darwin \
  --python-version 3.12 \
  --only-binary :all: \
  --generate-hashes \
  --no-emit-package babelfishr \
  --custom-compile-command "packaging/refresh_macos_lock.sh" \
  --quiet \
  "$@" \
  -o packaging/requirements-macos-arm64.txt

# Keep only the hashes of wheels macOS arm64 / CPython 3.12 can install, each
# confirmed against PyPI's JSON API for that exact file.
python3 -I packaging/narrow_lock_hashes.py packaging/requirements-macos-arm64.txt
