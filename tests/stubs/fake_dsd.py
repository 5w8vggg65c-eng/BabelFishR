#!/usr/bin/env python3
"""A stand-in for dsd-neo, used to test BabelFishR's integration boundary.

It reproduces the *interface* documented at
https://github.com/arancormonk/dsd-neo/blob/main/docs/cli.md - argument shape,
stdout chatter, exit status and an optional decoded WAV - not the decoding.
Scenarios are selected with BABELFISHR_FAKE_DSD_SCENARIO so each outcome branch
can be exercised.

This proves BabelFishR invokes an external decoder correctly and interprets its
output. It proves NOTHING about decoding real digital traffic; that requires the
real binary and independently identified recordings.
"""

import math
import os
import struct
import sys
import wave

# (stdout, exit status, audio: None | "voice" | "silent")
SCENARIOS = {
    "dmr-voice": (
        "dsd-neo 1.2.3-fake\nSync: DMR voice frame detected\n"
        "Color Code: 1\nTG: 2501  SRC: 1234567\n", 0, "voice"),
    "p25p2-voice": (
        "dsd-neo 1.2.3-fake\nP25 Phase 2 sync acquired\nvoice frame\n"
        "NAC: 293\n", 0, "voice"),
    "p25-metadata": (
        "dsd-neo 1.2.3-fake\nP25 Phase 1 sync acquired\nNAC: 293\n", 0, None),
    "encrypted": (
        "dsd-neo 1.2.3-fake\nDMR sync\nVoice frame ENC: encrypted, KEY ID 42\n",
        0, None),
    "nothing": ("dsd-neo 1.2.3-fake\nNo sync detected\n", 0, None),
    "silent-output": (
        "dsd-neo 1.2.3-fake\nNo sync detected\n", 0, "silent"),
    "crash": ("", 3, None),
    "candidate": ("dsd-neo 1.2.3-fake\nNXDN48 sync\n", 0, None),
}


def write_wav(path, silent):
    """A silent file is the case that must NOT count as a voice decode."""
    frames = bytearray()
    for i in range(8000):
        value = 0 if silent else int(12000 * math.sin(2 * math.pi * 440 * i / 8000))
        frames += struct.pack("<h", value)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(bytes(frames))


def main(argv):
    if "--version" in argv or "-v" in argv:
        print("dsd-neo 1.2.3-fake")
        return 0

    output = None
    for index, arg in enumerate(argv):
        if arg == "-w" and index + 1 < len(argv):
            output = argv[index + 1]

    # Record the invocation so tests can assert on the exact flags used.
    trace = os.environ.get("BABELFISHR_FAKE_DSD_TRACE")
    if trace:
        with open(trace, "a", encoding="utf-8") as handle:
            handle.write(" ".join(argv) + "\n")

    scenario = os.environ.get("BABELFISHR_FAKE_DSD_SCENARIO", "nothing")
    text, status, audio = SCENARIOS.get(scenario, SCENARIOS["nothing"])
    sys.stdout.write(text)
    if audio and output:
        write_wav(output, silent=(audio == "silent"))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
