#!/usr/bin/env python3
"""A stand-in for dsd-neo in streaming mode, at BabelFishR's boundary.

Reproduces the interface observed on the real binary (docs/cli.md,
docs/network-audio.md and the run recorded in babelfishr/receiver/contract.py):
"-i -" reads raw s16le mono from stdin and ends at end of file (the
input BabelFishR uses); "-i tcp:host:port" connects to the PCM producer as
a client; "-s" is the input rate, "-o -" writes decoded audio to stdout as s16le 8000 Hz two
channels (left slot 1, right slot 2), decoder events go to stderr as
"Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1", connection refused
exits 0 at once, and a producer that closes leaves dsd-neo running: it
prints "Connection to TCP Server Interrupted. Trying again in 300 ms." and
retries, then "TCP Socket Reconnected Successfully." or "Connection to TCP
Server Disconnected." - all observed on the real binary. It decodes
NOTHING: while the input is loud it writes the input downsampled to 8 kHz
into the selected slot's channel and silence into the other; while the
input is quiet it writes NOTHING (the real binary emits audio only while
decoding voice - observed).
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
    use_stdin = False
    for index, arg in enumerate(args):
        if arg == "-i" and args[index + 1] == "-":
            use_stdin = True
        elif arg == "-i" and args[index + 1].startswith("tcp"):
            parts = args[index + 1].split(":")
            if len(parts) == 3:
                host, port = parts[1], int(parts[2])
        if arg == "-s":
            rate = int(args[index + 1])
        if arg == "-V":
            slot = int(args[index + 1])
    sys.stderr.write("dsd-neo 2.9.0-fake-stream\n")
    sys.stderr.write(f"NOTICE: WAV input sample rate: {rate} Hz (interp=1)\n")
    sys.stderr.flush()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    delay = float(os.environ.get("FAKE_DSD_SIGTERM_DELAY") or 0)
    if delay:
        def slow_exit(*_):
            time.sleep(delay)
            sys.exit(0)
        signal.signal(signal.SIGTERM, slow_exit)
    conn = None
    if use_stdin:
        # "-i -": raw s16le mono on stdin (docs/cli.md line 80). Observed on
        # the real binary: a gap with no data only blocks the read; end of
        # file ends the program (exit 0, "NOTICE: Exiting.") - it never opens
        # another input. The same here.
        sys.stderr.write("Audio In/Out Device: -\n")
        sys.stderr.flush()
        source = sys.stdin.buffer
        def read_chunk():
            try:
                return source.read1(rate * 2 // 50)
            except (OSError, ValueError):
                return b""
    else:
        try:
            conn = socket.create_connection((host, port), timeout=5)
        except OSError as exc:
            sys.stderr.write(f"tcp connect failed: {exc}\nNOTICE: Exiting.\n")
            sys.stderr.flush()
            return 0
        def read_chunk():
            try:
                return conn.recv(rate * 2 // 50)
            except OSError:
                return b""
    step = rate // 8000
    out = sys.stdout.buffer
    frames_emitted = 0
    pending = b""
    while True:
        chunk = read_chunk()
        if not chunk:
            if use_stdin:
                sys.stderr.write("NOTICE: Exiting.\n")
                sys.stderr.flush()
                return 0
            # TCP mode: producer gone. Like the real dsd-neo, stay alive, say so, retry once.
            sys.stderr.write("\nConnection to TCP Server Interrupted. Trying again in 300 ms.\n")
            sys.stderr.flush()
            time.sleep(0.3)
            try:
                conn.close()
                conn = socket.create_connection((host, port), timeout=2)
                sys.stderr.write("TCP Socket Reconnected Successfully.\n")
            except OSError:
                # Observed on the real binary (dsd_symbol.c, symbol_read_sample_tcp):
                # one retry after 300 ms; when that fails too it prints this
                # line, gives the TCP input up for good and opens its own
                # audio input instead (a sound device, when it has one) -
                # BEFORE printing the line. From here on whatever it decodes
                # is NOT the receiver. This stand-in plays the part with a
                # loud tone, in real time.
                sys.stderr.write("Connection to TCP Server Disconnected.\n")
                sys.stderr.flush()
                import math
                tone = b"".join(struct.pack("<hh", v, v) for v in
                                (int(12000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(160)))
                while True:
                    out.write(tone)
                    out.flush()
                    time.sleep(0.02)
            sys.stderr.flush()
            continue
        pending += chunk
        usable = len(pending) - len(pending) % (2 * step)
        if usable <= 0:
            continue
        samples = struct.unpack(f"<{usable // 2}h", pending[:usable])
        pending = pending[usable:]
        decimated = samples[::step]
        loud = max(abs(s) for s in decimated) > 1500
        if not loud:
            continue                          # no voice, no output - as observed
        frames = []
        for value in decimated:
            left = value if slot in (1, 3) else 0
            right = value if slot in (2, 3) else 0
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
