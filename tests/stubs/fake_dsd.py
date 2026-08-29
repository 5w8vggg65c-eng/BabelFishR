#!/usr/bin/env python3
"""A stand-in for dsd-neo, used to test BabelFishR's integration boundary.

It reproduces the *interface* - argument shape, stdout chatter, exit status and
an optional decoded WAV - not the decoding. Scenarios are selected with
BABELFISHR_FAKE_DSD_SCENARIO so each outcome branch can be exercised.

This proves BabelFishR drives an external decoder correctly. It proves nothing
whatsoever about decoding real digital traffic.
"""

import os
import sys
import wave

SCENARIOS = {
    "dmr-voice": (
        "dsd-neo 1.2.3-fake\nSync: DMR voice frame detected\n"
        "Color Code: 1\nTG: 2501  SRC: 1234567\n", 0, True),
    "p25-metadata": (
        "dsd-neo 1.2.3-fake\nP25 Phase 1 sync acquired\nNAC: 293\n", 0, False),
    "encrypted": (
        "dsd-neo 1.2.3-fake\nDMR sync\nVoice frame ENC: encrypted, KEY ID 42\n",
        0, False),
    "nothing": ("dsd-neo 1.2.3-fake\nNo sync detected\n", 0, False),
    "crash": ("", 3, False),
    "candidate": ("dsd-neo 1.2.3-fake\nNXDN 48 sync\n", 0, False),
}


def write_silent_wav(path):
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(b"\x00\x01" * 8000)


def main(argv):
    if "--version" in argv or "-v" in argv:
        print("dsd-neo 1.2.3-fake")
        return 0

    output = None
    for index, arg in enumerate(argv):
        if arg == "-w" and index + 1 < len(argv):
            output = argv[index + 1]

    scenario = os.environ.get("BABELFISHR_FAKE_DSD_SCENARIO", "nothing")
    text, status, writes_audio = SCENARIOS.get(scenario, SCENARIOS["nothing"])
    sys.stdout.write(text)
    if writes_audio and output:
        write_silent_wav(output)
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
