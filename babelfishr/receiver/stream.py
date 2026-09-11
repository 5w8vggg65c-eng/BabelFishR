"""Audio from the receiver, as AudioSources the capture pipeline already
understands.

Two shapes. :class:`PcmTcpSource` is SDR++'s demodulated audio itself
(analog voice: FM/AM/SSB), read straight from the network sink.
:class:`DecodedVoiceSource` puts that same stream through dsd-neo and yields
the decoded speech dsd-neo produces (digital voice: DMR, P25 ...).

Both are :class:`~babelfishr.sources.SignalSource`s, so what the receiver
confirms about its tuning travels with every captured transmission, with
SDR provenance only for what was actually confirmed.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from ..audio.source import AudioBlock
from ..models import Provenance, utcnow
from ..sources import SignalMetadata, SignalSource
from .contract import DSD_NEO

log = logging.getLogger(__name__)

StatusCallback = Optional[Callable[[str, str], None]]

#: dsd-neo's decoder event line (stderr), e.g.
#: "06:13:40 Sync: +DMR  [SLOT1]  slot2  | Color Code=02 | VC1"
_SYNC = re.compile(r"Sync:\s+\+?(?P<proto>[A-Za-z0-9]+)(?P<rest>[^\n]*)")
_SLOT = re.compile(r"\[SLOT(\d)\]")
_FIELDS = (("color_code", re.compile(r"Color Code[=:\s]+(\d+)")),
           ("talkgroup", re.compile(r"(?:TG|TGT|Talkgroup)[=:\s]+(\d+)")),
           ("unit_id", re.compile(r"(?:SRC|Source)[=:\s]+(\d+)")),
           ("nac", re.compile(r"NAC[=:\s]+([0-9A-Fa-f]+)")))
#: dsd-neo names encryption as "ENC", "encrypted", or a P25 algorithm id;
#: ALG ID 0x80 is P25's *unencrypted* value and is not a flag (observed on
#: the P25 fixture: "LDU2 ALG ID: 0x80 KEY ID: 0x0000").
_ENCRYPTED = re.compile(r"\bENC\b|encrypt|ALG ID: 0x(?!80\b)[0-9A-Fa-f]{2}", re.I)


class TuningState:
    """What the receiver has confirmed, shared by the controller and the
    source it built. Written by the controller under its own lock."""

    def __init__(self) -> None:
        self.requested_hz: Optional[float] = None
        self.confirmed_hz: Optional[float] = None
        self.mode: str = ""
        self.bandwidth_hz: Optional[int] = None
        self.confirmed_at: Optional[_dt.datetime] = None

    def metadata(self, source: str, sample_rate: float, **extra) -> SignalMetadata:
        confirmed = self.confirmed_hz is not None
        record = {"requested_frequency_hz": self.requested_hz,
                  "receiver_confirmed": confirmed,
                  "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
                  "bandwidth_hz": self.bandwidth_hz}
        record.update(extra)
        return SignalMetadata(
            center_frequency_hz=self.confirmed_hz,
            tuned_frequency_hz=self.confirmed_hz,
            sample_rate_hz=float(sample_rate),
            modulation=self.mode,
            source=source, extra=record,
            # Measured status only for what the receiver itself reported
            # back ("f"); a requested frequency it did not confirm stays
            # unverified. No RSSI or SNR: SDR++'s rigctl reports none, and
            # none is invented.
            provenance=Provenance.SDR if confirmed else Provenance.UNKNOWN)


class _Reader(threading.Thread):
    """Reads a byte stream into a queue of float blocks, tagged with the
    generation current when they were read, so a retune can drop what was
    buffered under the old frequency."""

    def __init__(self, owner, read_bytes: Callable[[int], bytes], sample_rate: int,
                 channels: int, channel: int, block_frames: int):
        super().__init__(daemon=True, name=f"{owner.name} reader")
        self.owner = owner
        self.read_bytes = read_bytes
        self.sample_rate = sample_rate
        self.channels = channels
        self.channel = channel
        self.frame_bytes = 2 * channels
        self.block_bytes = block_frames * self.frame_bytes
        self.frames_seen = 0

    def run(self) -> None:
        owner = self.owner
        pending = b""
        try:
            while owner._running:
                chunk = self.read_bytes(self.block_bytes)
                if not chunk:
                    owner._on_stream_end()
                    return
                pending += chunk
                usable = len(pending) - (len(pending) % self.frame_bytes)
                if usable < self.frame_bytes:
                    continue
                frames = np.frombuffer(pending[:usable], dtype="<i2")
                pending = pending[usable:]
                if self.channels > 1:
                    frames = frames.reshape(-1, self.channels)[:, self.channel]
                samples = frames.astype(np.float64) / 32768.0
                offset = self.frames_seen / float(self.sample_rate)
                self.frames_seen += samples.size
                owner._queue.put((owner._generation, AudioBlock(
                    samples=samples, sample_rate=self.sample_rate,
                    timestamp=owner._start_time + _dt.timedelta(seconds=offset),
                    offset=offset)))
        except Exception as exc:  # noqa: BLE001 - reported, never lost silently
            if owner._running:
                owner._report("stream-error", f"{owner.name}: {exc}")
                owner._on_stream_end()


class _StreamSource(SignalSource):
    """The shared machinery: a queue, a generation counter, status reports."""

    measures_rf = True

    def __init__(self, tuning: TuningState, sample_rate: int, name: str,
                 on_status: StatusCallback = None, block_ms: int = 20):
        self.tuning = tuning
        self.sample_rate = int(sample_rate)
        self.name = name
        self.on_status = on_status
        self.block_frames = max(1, int(self.sample_rate * block_ms / 1000))
        self._queue: "queue.Queue" = queue.Queue()
        self._running = False
        self._finished = False
        self._generation = 0
        self._start_time = utcnow()
        self._reader: Optional[_Reader] = None
        self._lock = threading.Lock()
        self.events: Dict[str, object] = {}

    # -- AudioSource -----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self._finished

    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                generation, block = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if generation != self._generation:
                continue                       # buffered under an earlier tuning
            return block

    def flush(self) -> None:
        """Drop everything buffered so far: after a retune, audio that was
        received under the previous frequency must not appear under the
        new one."""
        with self._lock:
            self._generation += 1
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break

    def _report(self, kind: str, message: str) -> None:
        log.info("%s: %s %s", self.name, kind, message)
        if self.on_status is not None:
            try:
                self.on_status(kind, message)
            except Exception:  # noqa: BLE001
                log.exception("receiver status callback failed")

    def _on_stream_end(self) -> None:
        if self._running:
            self._running = False
            self._finished = True
            self._report("disconnected", f"{self.name}: the audio stream ended")
        self._queue.put((-1, None))            # wake a blocked read()

    def read_wake(self) -> None:
        self._queue.put((-1, None))


class PcmTcpSource(_StreamSource):
    """SDR++'s network sink, TCP mode: s16le mono at the sink's sample rate.
    BabelFishR is the one client the sink accepts."""

    def __init__(self, host: str, port: int, tuning: TuningState,
                 sample_rate: int = 48000, on_status: StatusCallback = None,
                 connect_timeout: float = 5.0, name: str = "SDR++ receiver (analog)"):
        super().__init__(tuning, sample_rate, name, on_status)
        self.host, self.port = host, int(port)
        self.connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None

    def start(self) -> None:
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.connect_timeout)
        self._sock.settimeout(None)
        self._running = True
        self._finished = False
        self._start_time = utcnow()
        self._reader = _Reader(self, self._recv, self.sample_rate, 1, 0, self.block_frames)
        self._reader.start()
        self._report("connected", f"receiving {self.sample_rate} Hz audio from "
                                  f"SDR++ at {self.host}:{self.port}")

    def _recv(self, size: int) -> bytes:
        try:
            return self._sock.recv(size)
        except OSError:
            return b""

    def stop(self) -> None:
        self._running = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.read_wake()
        if self._reader is not None:
            self._reader.join(timeout=2.0)

    def metadata(self) -> SignalMetadata:
        return self.tuning.metadata("sdrpp", self.sample_rate, path="analog")


class DecodedVoiceSource(_StreamSource):
    """dsd-neo between the receiver and the pipeline.

    dsd-neo connects to SDR++'s network sink itself (``-i tcp:host:port``),
    decodes with the chosen profile and writes the decoded speech to its
    stdout (``-o -``: 8 kHz, two channels, left = slot 1, right = slot 2).
    One slot is taken: two simultaneous conversations must not be mixed into
    one transcript. Decoder events on stderr become metadata (protocol,
    slot, colour code, talkgroup, source unit) and an *encrypted* flag.
    Encrypted or unsupported traffic yields no speech from dsd-neo, so it
    yields no transmission here - it is never presented as decoded.
    """

    def __init__(self, executable: str, host: str, port: int, tuning: TuningState,
                 protocol_flag: str = "-fa", slot: int = 1,
                 input_sample_rate: int = DSD_NEO.input_sample_rate,
                 on_status: StatusCallback = None, extra_args: Optional[List[str]] = None,
                 name: str = "SDR++ receiver → DSD-neo (digital)"):
        super().__init__(tuning, DSD_NEO.output_sample_rate, name, on_status)
        self.executable = executable
        self.host, self.port = host, int(port)
        self.protocol_flag = protocol_flag
        self.slot = 1 if int(slot) not in (1, 2) else int(slot)
        self.input_sample_rate = int(input_sample_rate)
        self.extra_args = list(extra_args or [])
        self.process: Optional[subprocess.Popen] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self.decoder: Dict[str, object] = {"protocol": "", "encrypted": False,
                                           "sync_lines": 0}
        self.stderr_tail: List[str] = []

    def command(self) -> List[str]:
        return [self.executable, "-i", f"tcp:{self.host}:{self.port}",
                "-s", str(self.input_sample_rate), self.protocol_flag,
                "-V", str(self.slot), "-o", "-", *self.extra_args]

    def start(self) -> None:
        env = dict(os.environ)
        self.process = subprocess.Popen(
            self.command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, start_new_session=True)
        self._running = True
        self._finished = False
        self._start_time = utcnow()
        self._reader = _Reader(self, self._read_stdout, self.sample_rate,
                               DSD_NEO.output_channels, self.slot - 1, self.block_frames)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._watch_stderr, daemon=True,
                                               name="dsd-neo events")
        self._stderr_thread.start()
        self._report("connected", f"dsd-neo (pid {self.process.pid}) decoding "
                                  f"{self.protocol_flag} slot {self.slot} from "
                                  f"SDR++ at {self.host}:{self.port}")

    def _read_stdout(self, size: int) -> bytes:
        try:
            return self.process.stdout.read1(size) if hasattr(self.process.stdout, "read1") \
                else self.process.stdout.read(size)
        except (OSError, ValueError):
            return b""

    def _watch_stderr(self) -> None:
        try:
            for raw in iter(self.process.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.stderr_tail.append(line)
                del self.stderr_tail[:-200]
                self._note_event(line)
        except (OSError, ValueError):
            pass
        code = self.process.poll() if self.process is not None else None
        if self._running:
            # dsd-neo exits at once when there is no producer to connect to
            # (observed: exit 0 on connection refused), and otherwise only
            # when stopped. Either way, nothing more will be decoded.
            self._report("decoder-exited", f"dsd-neo ended (exit {code}); "
                                           f"last output: {self.stderr_tail[-1] if self.stderr_tail else ''}")
            self._on_stream_end()

    def _note_event(self, line: str) -> None:
        match = _SYNC.search(line)
        if match:
            self.decoder["protocol"] = match.group("proto")
            self.decoder["sync_lines"] = int(self.decoder.get("sync_lines", 0)) + 1
            slot = _SLOT.search(match.group("rest"))
            if slot:
                self.decoder["active_slot"] = int(slot.group(1))
        for key, pattern in _FIELDS:
            found = pattern.search(line)
            if found:
                self.decoder[key] = found.group(1)
        if _ENCRYPTED.search(line):
            self.decoder["encrypted"] = True

    def stop(self) -> None:
        self._running = False
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)
        if process is not None:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except OSError:
                    pass
        self.read_wake()
        if self._reader is not None:
            self._reader.join(timeout=2.0)

    def metadata(self) -> SignalMetadata:
        meta = self.tuning.metadata(
            "sdrpp+dsd-neo", self.sample_rate, path="digital",
            decoder_flag=self.protocol_flag, slot=self.slot,
            encrypted=bool(self.decoder.get("encrypted")),
            color_code=self.decoder.get("color_code"),
            nac=self.decoder.get("nac"),
            sync_lines=self.decoder.get("sync_lines", 0))
        meta.protocol = str(self.decoder.get("protocol") or "")
        meta.talkgroup = str(self.decoder.get("talkgroup") or "")
        meta.unit_id = str(self.decoder.get("unit_id") or "")
        return meta
