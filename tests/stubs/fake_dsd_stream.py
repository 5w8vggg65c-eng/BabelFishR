#!/usr/bin/env python3
"""A stand-in for dsd-neo in streaming mode, at BabelFishR's boundary.

Reproduces the interface observed on the real binary (docs/cli.md,
docs/network-audio.md and the run recorded in babelfishr/receiver/contract.py):
"-i tcp:host:port" connects to the PCM producer as a client, "-s" is the
input rate, "-o -" writes decoded audio to stdout as s16le 8000 Hz two
channels (left slot 1, right slot 2), decoder events go to stderr as
"Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1", connection refused
exits 0 at once, and a producer that closes leaves dsd-neo running until
terminated. It decodes NOTHING: while the input is loud it writes the input
downsampled to 8 kHz into the selected slot's channel and silence into the
other; while the input is quiet it writes silence and no events.
"""

import os
import signal
import socket
import struct
import sys
import time


def main():
    args = sys.argv[1:]
    host, port, rate, slot = "localhost", 7355, 48000, 3
    for index, arg in enumerate(args):
        if arg == "-i" and args[index + 1].startswith("tcp"):
            parts = args[index + 1].split(":")
            if len(parts) == 3:
                host, port = parts[1], int(parts[2])
        if arg == "-s":
            rate = int(args[index + 1])
        if arg == "-V":
            slot = int(args[index + 1])
    sys.stderr.write("dsd-neo 2.9.0-fake-stream\n")
    sys.stderr.flush()
    try:
        conn = socket.create_connection((host, port), timeout=5)
    except OSError as exc:
        sys.stderr.write(f"tcp connect failed: {exc}\nNOTICE: Exiting.\n")
        sys.stderr.flush()
        return 0
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    step = rate // 8000
    out = sys.stdout.buffer
    frames_emitted = 0
    pending = b""
    while True:
        try:
            chunk = conn.recv(rate * 2 // 50)
        except OSError:
            chunk = b""
        if not chunk:
            # Producer gone: like the real dsd-neo, stay alive until stopped.
            time.sleep(0.1)
            continue
        pending += chunk
        usable = len(pending) - len(pending) % (2 * step)
        if usable <= 0:
            continue
        samples = struct.unpack(f"<{usable // 2}h", pending[:usable])
        pending = pending[usable:]
        decimated = samples[::step]
        loud = max(abs(s) for s in decimated) > 1500
        frames = []
        for value in decimated:
            voice = value if loud else 0
            left = voice if slot in (1, 3) else 0
            right = voice if slot in (2, 3) else 0
            frames.append(struct.pack("<hh", left, right))
        out.write(b"".join(frames))
        out.flush()
        frames_emitted += len(decimated)
        if loud and frames_emitted % 4000 < len(decimated):
            active = "[SLOT1]  slot2 " if slot != 2 else " slot1  [SLOT2]"
            sys.stderr.write(f"{time.strftime('%H:%M:%S')} Sync: +DMR  {active} | Color Code=02 | VC1 \n")
            sys.stderr.write("TGT=2501 SRC=1234567\n")
            sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main() or 0)
