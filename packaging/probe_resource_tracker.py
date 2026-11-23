"""Launch a frozen BabelFishR exactly as multiprocessing launches its resource
tracker, and require it to behave as one.

    python3 packaging/probe_resource_tracker.py <BabelFishR binary> '<flags JSON>'

The flags are the frozen interpreter's own (printed by the bundle's
--selftest-resource-tracker), because PyInstaller's runtime hook diverts the
child only when they match. The child gets the read end of a real pipe, the
write end is closed before launch, so a tracker reads EOF straight away and
exits 0. An entry point without multiprocessing.freeze_support() hands these
arguments to the CLI instead, and argparse exits 2 with "invalid choice".
Runs from / with a scrubbed environment and a throwaway HOME: no models, no
network, nothing written to the operator's Application Support.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile


def main() -> int:
    binary, flags = sys.argv[1], json.loads(sys.argv[2])
    read_fd, write_fd = os.pipe()
    os.close(write_fd)                       # the tracker will read EOF at once
    with tempfile.TemporaryDirectory(prefix="bfr-tracker-probe-") as home:
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home,
               "TMPDIR": os.environ.get("TMPDIR", "/tmp")}
        command = [binary, *flags, "-c",
                   f"from multiprocessing.resource_tracker import main;main({read_fd})"]
        try:
            done = subprocess.run(command, pass_fds=(read_fd,), env=env, cwd="/",
                                  capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            print("FAIL: the resource-tracker child did not exit within 30 s")
            return 1
        finally:
            os.close(read_fd)
    output = (done.stdout + done.stderr).strip()
    print(f"command: {command[1:]}")
    print(f"exit {done.returncode}" + (f"; output: {output[:400]}" if output else "; no output"))
    if done.returncode != 0 or "invalid choice" in output or "usage:" in output:
        print("FAIL: the child reached the CLI instead of multiprocessing")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
