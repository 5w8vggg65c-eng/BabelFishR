"""packaging/verify_independence.sh: the self-test time limit releases a
check that finishes early, and still stops one that hangs.

Root's Mac measurement: a 0.1 s check under a 2 s watcher took 2.012 s,
because the watcher (which redirected only stderr) held the caller's
command-substitution stdout open until its sleep ended; 0.117 s with the
watcher's stdout redirected too. The function is exercised as the build
calls it, OUTPUT=$(run_isolated_limited LIMIT ...), with run_isolated
replaced by a shell command runner (the real one launches the frozen app).
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import time

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "packaging" / "verify_independence.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def function_source(script=SCRIPT):
    text = script.read_text(encoding="utf-8")
    match = re.search(r"^run_isolated_limited\(\) \{\n.*?^\}\n", text, re.S | re.M)
    assert match, "run_isolated_limited not found"
    return match.group(0)


def call(limit, command, source=None):
    """Run OUTPUT=$(run_isolated_limited LIMIT COMMAND); return
    (seconds, status, output)."""
    harness = (
        'run_isolated() { bash -c "$1"; }\n'
        + (source or function_source())
        + f'OUTPUT=$(run_isolated_limited {limit} {command!r}); status=$?\n'
        + 'printf "%s\\n<status=%s>" "$OUTPUT" "$status"\n')
    started = time.monotonic()
    done = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                          timeout=60)
    elapsed = time.monotonic() - started
    output, _, status = done.stdout.rpartition("<status=")
    return elapsed, int(status.rstrip(">\n")), output.strip()


def test_a_check_that_finishes_early_returns_at_once_with_its_output():
    elapsed, status, output = call(5, "sleep 0.1; echo checked")
    assert status == 0 and output == "checked"
    assert elapsed < 2.0, f"waited {elapsed:.2f} s for a 0.1 s check under a 5 s limit"


def test_a_failing_check_returns_its_own_status_and_output_at_once():
    elapsed, status, output = call(5, "echo broken; exit 3")
    assert status == 3 and output == "broken"
    assert elapsed < 2.0


def test_a_hung_check_is_still_killed_at_the_limit():
    elapsed, status, output = call(1, "echo started; sleep 30; echo never")
    assert status != 0, "a check killed at the limit must fail the step"
    assert output == "started"
    assert 0.9 <= elapsed < 6.0, f"took {elapsed:.2f} s for a 1 s limit"


def test_the_previous_watcher_waited_out_the_whole_limit(tmp_path):
    """Non-vacuity: the same harness on the old watcher line reproduces the
    field delay, so the timing assertion above means something."""
    old = function_source().replace(
        function_source()[function_source().index("  # The watcher must not"):
                          function_source().index("  local watcher=$!")],
        '  ( sleep "$limit"; kill -9 "$pid" 2>/dev/null ) 2>/dev/null &\n')
    elapsed, status, output = call(2, "sleep 0.1; echo checked", source=old)
    assert status == 0 and output == "checked"
    assert elapsed >= 1.9, f"the old watcher returned after {elapsed:.2f} s"
