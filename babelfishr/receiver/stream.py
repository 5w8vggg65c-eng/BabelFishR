"""Audio from the receiver, as AudioSources the capture pipeline already
understands.

Two shapes. :class:`PcmTcpSource` is SDR++'s demodulated audio itself
(analog voice: FM/AM/SSB), read straight from the network sink.
:class:`DecodedVoiceSource` puts that same stream through dsd-neo and yields
the decoded speech dsd-neo produces (digital voice: DMR, P25 ...).

Both are :class:`~babelfishr.sources.SignalSource`s, so what the receiver
confirms about its tuning travels with every captured transmission, with
SDR provenance only for what was actually confirmed. A change of tuning is
a *boundary* in the stream: whatever was buffered before it is dropped,
whatever the detector held open is closed under the tuning it was received
under, and only then does audio under the new tuning flow.
"""

from __future__ import annotations

import collections
import datetime as _dt
import logging
import os
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
_VOICE = re.compile(r"\bVC\d|\bVOICE\b|voice", re.I)
_FIELDS = (("color_code", re.compile(r"Color Code[=:\s]+(\d+)")),
           ("talkgroup", re.compile(r"(?:TG|TGT|Talkgroup)[=:\s]+(\d+)")),
           ("unit_id", re.compile(r"(?:SRC|Source)[=:\s]+(\d+)")),
           ("nac", re.compile(r"NAC[=:\s]+([0-9A-Fa-f]+)")))
#: dsd-neo names encryption as "ENC", "encrypted", or a P25 algorithm id;
#: ALG ID 0x80 is P25's *unencrypted* value (observed on the P25 fixture:
#: "LDU2 ALG ID: 0x80 KEY ID: 0x0000") and clears the flag for that call.
_ENCRYPTED = re.compile(r"\bENC\b|encrypt|ALG ID: 0x(?!80\b)[0-9A-Fa-f]{2}", re.I)
_CLEAR = re.compile(r"ALG ID: 0x80\b")
#: Observed on the real binary when the PCM producer goes away and returns:
#: "Connection to TCP Server Interrupted. Trying again in 300 ms." (one
#: retry follows), "TCP Socket Reconnected Successfully." (it worked), and
#: "Connection to TCP Server Disconnected." - the retry failed too and
#: dsd-neo has given the TCP input up FOR GOOD: dsd_symbol.c
#: symbol_read_sample_tcp() then opens its own audio input (a sound device)
#: in its place, so anything it decodes from then on is not the receiver.
#: Its TCP reads carry a 1.5 s receive timeout (dsd_rigctl.c Connect()), so
#: a producer that stays connected but silent for longer counts as lost.
#: While idle: "WARNING: Input Level LOW -120.0 dBFS".
_UPSTREAM_LOST = re.compile(r"Connection to TCP Server Interrupted")
_UPSTREAM_ABANDONED = re.compile(r"Connection to TCP Server Disconnected")
_UPSTREAM_BACK = re.compile(r"TCP Socket Reconnected Successfully")
_LEVEL_LOW = re.compile(r"Input Level LOW")

#: How long a call's identifiers stay attached after its last voice frame.
CALL_HOLD_SECONDS = 2.0
#: Silence the decoded source synthesises when dsd-neo emits nothing (it
#: writes audio only while decoding voice - observed), so the detector sees
#: the gaps between calls and closes each one.
IDLE_FILL_SECONDS = 0.1
#: Bounded buffering: this much audio may wait for a slow consumer; beyond
#: it the oldest is dropped and the loss is reported.
QUEUE_SECONDS = 10.0


class TuningState:
    """What the receiver has confirmed, shared by the controller and the
    source it built. Written by the controller under its own lock."""

    def __init__(self) -> None:
        self.requested_hz: Optional[float] = None
        self.confirmed_hz: Optional[float] = None
        self.mode: str = ""
        self.bandwidth_hz: Optional[int] = None
        self.confirmed_at: Optional[_dt.datetime] = None
        self.epoch: int = 0
        self.play_requested: bool = False

    def snapshot(self) -> "TuningState":
        copy = TuningState()
        copy.__dict__.update(self.__dict__)
        return copy

    def metadata(self, source: str, sample_rate: float, **extra) -> SignalMetadata:
        confirmed = self.confirmed_hz is not None
        record = {"requested_frequency_hz": self.requested_hz,
                  "receiver_confirmed": confirmed,
                  "confirmed_at": self.confirmed_at.isoformat() if self.confirmed_at else None,
                  "bandwidth_hz": self.bandwidth_hz,
                  "tuning_epoch": self.epoch,
                  # \start writes no reply: requested is all that is known.
                  "radio_start_requested": self.play_requested,
                  "radio_start_confirmed": None}
        record.update(extra)
        return SignalMetadata(
            # rigctl "f" is the VFO's frequency; it does not report the RF
            # centre frequency the receiver is sampling, so that stays unknown.
            center_frequency_hz=None,
            tuned_frequency_hz=self.confirmed_hz,
            sample_rate_hz=float(sample_rate),
            modulation=self.mode,
            source=source, extra=record,
            # Measured status only for what the receiver itself reported
            # back ("f"); a requested frequency it did not confirm stays
            # unverified. No RSSI or SNR: SDR++'s rigctl reports none, and
            # none is invented.
            provenance=Provenance.SDR if confirmed else Provenance.UNKNOWN)


class StreamBoundary:
    """Handed to the consumer instead of a block when the tuning changed:
    close whatever is open under *metadata* (the tuning it was received
    under), then continue under the new one."""

    def __init__(self, metadata: SignalMetadata, reason: str):
        self.metadata = metadata
        self.reason = reason
        self.samples = np.zeros(0, dtype=np.float64)


class _Reader(threading.Thread):
    """Reads a byte stream into the owner's queue as float blocks, each
    tagged with the tuning generation current *before* the read began, so a
    retune drops what was buffered under the old frequency even when the
    bytes were read across the moment of the change."""

    def __init__(self, owner, read_bytes: Callable[[int], bytes], sample_rate: int,
                 channels: int, channel: int, block_frames: int, wall_clock: bool):
        super().__init__(daemon=True, name=f"{owner.name} reader")
        self.owner = owner
        self.read_bytes = read_bytes
        self.sample_rate = sample_rate
        self.channels = channels
        self.channel = channel
        self.frame_bytes = 2 * channels
        self.block_bytes = block_frames * self.frame_bytes
        self.frames_seen = 0
        self.wall_clock = wall_clock

    def run(self) -> None:
        owner = self.owner
        pending = b""
        try:
            while owner._running:
                generation = owner._generation      # before the read, not after
                chunk = self.read_bytes(self.block_bytes)
                if not chunk:
                    owner._on_stream_end()
                    return
                if not owner._running:
                    return                     # stopped while we read: not ours to keep
                pending += chunk
                usable = len(pending) - (len(pending) % self.frame_bytes)
                if usable < self.frame_bytes:
                    continue
                frames = np.frombuffer(pending[:usable], dtype="<i2")
                pending = pending[usable:]
                if self.channels > 1:
                    frames = frames.reshape(-1, self.channels)[:, self.channel]
                samples = frames.astype(np.float64) / 32768.0
                if self.wall_clock:
                    offset = time.monotonic() - owner._start_mono - samples.size / self.sample_rate
                    offset = max(offset, owner._last_offset)
                else:
                    offset = self.frames_seen / float(self.sample_rate)
                self.frames_seen += samples.size
                owner._enqueue(generation, AudioBlock(
                    samples=samples, sample_rate=self.sample_rate,
                    timestamp=owner._start_time + _dt.timedelta(seconds=offset),
                    offset=offset), samples.size)
        except Exception as exc:  # noqa: BLE001 - reported, never lost silently
            if owner._running:
                owner._report("stream-error", f"{owner.name}: {exc}")
                owner._on_stream_end()


class _StreamSource(SignalSource):
    """The shared machinery: a bounded buffer, a generation counter, boundary
    markers that are never lost, status reports, and a record of when audio
    last arrived.

    The buffer holds audio blocks and boundary markers in arrival order. A
    retune drops the *audio* buffered under the ending tuning but keeps every
    marker, so a consumer that drains late still receives each boundary in
    order and closes what it holds under the tuning it was really received
    on. Overflow drops the oldest audio, never a marker.
    """

    measures_rf = True

    def __init__(self, tuning: TuningState, sample_rate: int, name: str,
                 on_status: StatusCallback = None, block_ms: int = 20):
        self.tuning = tuning
        self.sample_rate = int(sample_rate)
        self.name = name
        self.on_status = on_status
        self.block_frames = max(1, int(self.sample_rate * block_ms / 1000))
        self.capacity = max(4, int(QUEUE_SECONDS * 1000 / block_ms))
        self._items: "collections.deque" = collections.deque()
        self._cond = threading.Condition()
        self._running = False
        self._finished = False
        self._generation = 0
        self._start_time = utcnow()
        self._start_mono = time.monotonic()
        self._last_offset = 0.0
        self._reader: Optional[_Reader] = None
        self.last_block_at: Optional[float] = None
        self.dropped_frames = 0
        self._dropping = False

    # -- the buffer -----------------------------------------------------------
    @property
    def buffered(self) -> int:
        """Audio blocks waiting for the consumer (markers not counted)."""
        with self._cond:
            return sum(1 for _, item in self._items if isinstance(item, AudioBlock))

    def _enqueue(self, generation: int, item, frames: int) -> None:
        if frames > 0:
            # Audio arrived from the receiver's side (a socket read, decoded
            # output) - whether or not a consumer has taken it yet. Silence
            # this source synthesises passes frames=0 and does not count.
            self.last_block_at = time.monotonic()
        with self._cond:
            if isinstance(item, AudioBlock):
                audio = sum(1 for _, other in self._items if isinstance(other, AudioBlock))
                if audio >= self.capacity:
                    # Drop the oldest audio block; markers stay where they are.
                    for index, (_, other) in enumerate(self._items):
                        if isinstance(other, AudioBlock):
                            del self._items[index]
                            self.dropped_frames += int(other.samples.size)
                            break
                    if not self._dropping:
                        self._dropping = True
                        self._cond.notify_all()
                        self._report("audio-dropped", f"{self.name}: the consumer is not "
                                                      f"keeping up; the oldest buffered audio "
                                                      f"is being dropped")
                elif self._dropping:
                    self._dropping = False
                    self._report("audio-resumed", f"{self.name}: buffering caught up "
                                                  f"({self.dropped_frames} frames lost so far)")
            self._items.append((generation, item))
            self._cond.notify_all()

    def _boundary(self, reason: str) -> None:
        """A tuning change: drop the audio buffered under the ending tuning,
        keep every earlier marker, and add one carrying the ending epoch's
        metadata (taken now, before the new values are written)."""
        ending = self.metadata()
        with self._cond:
            self._generation += 1
            kept = [(g, item) for g, item in self._items if isinstance(item, StreamBoundary)]
            self._items.clear()
            self._items.extend(kept)
            self._items.append((self._generation, StreamBoundary(ending, reason)))
            self._cond.notify_all()

    def flush(self) -> None:
        """Drop the audio buffered so far; markers stay."""
        with self._cond:
            self._generation += 1
            kept = [(g, item) for g, item in self._items if isinstance(item, StreamBoundary)]
            self._items.clear()
            self._items.extend(kept)

    def retuned(self, reason: str = "retune") -> None:
        """The controller confirmed a new tuning: mark the boundary."""
        self._boundary(reason)

    # -- AudioSource -----------------------------------------------------
    @property
    def running(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self._finished

    def read(self, timeout: float = 1.0):
        deadline = time.monotonic() + timeout
        while True:
            with self._cond:
                while not self._items:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not self._cond.wait(remaining):
                        return None
                generation, item = self._items.popleft()
                current = self._generation
            if item is None:
                return None                    # a wake-up, not audio
            if isinstance(item, StreamBoundary):
                return item                    # every marker reaches the consumer
            if generation != current:
                continue                       # buffered under an earlier tuning
            self._last_offset = item.offset
            return item

    @property
    def receiving(self) -> bool:
        """Audio from the receiver's side arrived within the last two
        seconds (not counting silence synthesised here)."""
        return self.last_block_at is not None and time.monotonic() - self.last_block_at < 2.0

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
        self.read_wake()

    def read_wake(self) -> None:
        with self._cond:
            self._items.append((-1, None))
            self._cond.notify_all()

    def end(self, reason: str) -> None:
        """The controller lost the receiver: the stream is over."""
        self._report("receiver-lost", reason)
        self._on_stream_end()


class PcmTcpSource(_StreamSource):
    """SDR++'s network sink, TCP mode: s16le mono at the sink's sample rate.
    BabelFishR is the one client the sink accepts. SDR++ streams
    continuously while the radio runs, so time follows the sample count."""

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
        self._start_mono = time.monotonic()
        self._reader = _Reader(self, self._recv, self.sample_rate, 1, 0, self.block_frames,
                               wall_clock=False)
        self._reader.start()
        self._report("connected", f"receiving {self.sample_rate} Hz audio from "
                                  f"SDR++ at {self.host}:{self.port}")

    def _recv(self, size: int) -> bytes:
        try:
            return self._sock.recv(size)
        except OSError:
            return b""

    def stop(self) -> None:
        """Cheap and non-blocking: closing the socket ends the reader."""
        self._running = False
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self.read_wake()

    def metadata(self) -> SignalMetadata:
        return self.tuning.metadata("sdrpp", self.sample_rate, path="analog",
                                    dropped_frames=self.dropped_frames)


class CallTracker:
    """dsd-neo's events, kept per call and per slot.

    Identifiers are attached only while a call on the *selected* slot is
    active (voice within CALL_HOLD_SECONDS) and only from lines that belong
    to it: a Sync line marks which slot is active; talkgroup/unit lines
    without a slot of their own are taken to describe the slot the latest
    Sync line marked. A new call starts clean, so nothing leaks between
    calls, slots or tuning epochs; the encrypted flag is per call and a
    clear ALG ID 0x80 clears it.
    """

    def __init__(self, selected_slot: int):
        self.selected_slot = selected_slot
        self.protocol = ""
        self.active_slot: Optional[int] = None
        self.calls: Dict[int, Dict[str, object]] = {}
        self.sync_lines = 0
        self.last_voice: Dict[int, float] = {}
        self.upstream_lost = False
        self.upstream_abandoned = False
        self.level_low = False

    def note(self, line: str, now: Optional[float] = None) -> Optional[str]:
        """Consume one stderr line. Returns a status kind to report, or None."""
        now = time.monotonic() if now is None else now
        if _UPSTREAM_ABANDONED.search(line):
            self.upstream_lost = True
            self.upstream_abandoned = True
            return "upstream-abandoned"
        if _UPSTREAM_LOST.search(line):
            self.upstream_lost = True
            return "upstream-lost"
        if _UPSTREAM_BACK.search(line):
            self.upstream_lost = False
            return "upstream-restored"
        if _LEVEL_LOW.search(line):
            self.level_low = True
            return "input-level-low"
        match = _SYNC.search(line)
        if match:
            self.protocol = match.group("proto")
            self.sync_lines += 1
            self.level_low = False
            slot = _SLOT.search(match.group("rest"))
            if slot:
                self.active_slot = int(slot.group(1))
                if _VOICE.search(match.group("rest")):
                    self._voice(self.active_slot, now)
            elif "[" not in match.group("rest"):
                self.active_slot = 1 if self.protocol not in ("DMR", "P25p2", "NXDN96", "X2") \
                    else self.active_slot
                if _VOICE.search(match.group("rest")) and self.active_slot:
                    self._voice(self.active_slot, now)
        slot = self.active_slot
        if slot is not None:
            call = self.calls.get(slot)
            if call is not None and now - self.last_voice.get(slot, 0.0) <= CALL_HOLD_SECONDS:
                for key, pattern in _FIELDS:
                    found = pattern.search(line)
                    if found:
                        call[key] = found.group(1)
                if _CLEAR.search(line):
                    call["encrypted"] = False
                elif _ENCRYPTED.search(line):
                    call["encrypted"] = True
        return None

    def _voice(self, slot: int, now: float) -> None:
        last = self.last_voice.get(slot)
        if last is None or now - last > CALL_HOLD_SECONDS:
            self.calls[slot] = {"encrypted": False}      # a new call starts clean
        self.last_voice[slot] = now

    def current(self, now: Optional[float] = None) -> Dict[str, object]:
        """The selected slot's active call, or nothing."""
        now = time.monotonic() if now is None else now
        last = self.last_voice.get(self.selected_slot)
        if last is None or now - last > CALL_HOLD_SECONDS:
            return {}
        return dict(self.calls.get(self.selected_slot, {}))

    def reset(self) -> None:
        self.calls.clear()
        self.last_voice.clear()
        self.active_slot = None


class DecodedVoiceSource(_StreamSource):
    """dsd-neo between the receiver and the pipeline - with BabelFishR
    owning the receiver connection.

    BabelFishR connects to SDR++'s network sink itself and pumps the raw PCM
    into dsd-neo's standard input (``-i -``: raw s16le mono at ``-s`` Hz,
    docs/cli.md line 80). dsd-neo then has exactly one possible input: when
    that pipe reaches end-of-file it requests its own shutdown
    (dsd_symbol.c symbol_read_sample_stdin(): sf_read_short returning 0 →
    dsd_request_shutdown) instead of opening a sound device, as its TCP
    input does after a failed reconnect (symbol_read_sample_tcp()). Verified
    on the pinned binary here: a 2.5 s gap with no data only blocks it; EOF
    ends it (exit 0) within half a second. So nothing but the receiver can
    ever reach the decoder, whatever the timing of its diagnostic lines.

    The decoded speech comes back on stdout (8 kHz, two channels, left =
    slot 1, right = slot 2); one slot is taken. dsd-neo writes audio only
    while decoding voice (observed), so this source keeps time by the clock
    and fills the gaps with silence, and the detector sees each call end.
    A producer that goes away is reconnected for a short while (dsd-neo
    waits on its pipe meanwhile); one that does not come back ends the
    stream, and the pipe is closed so the decoder ends too. Encrypted or
    unsupported traffic yields no speech from dsd-neo, so it yields no
    transmission here - it is never presented as decoded.
    """

    #: How long the pump tries to get SDR++'s sink back after losing it
    #: before declaring the receiver lost.
    RECONNECT_SECONDS = 5.0

    def __init__(self, executable: str, host: str, port: int, tuning: TuningState,
                 protocol_flag: str = "-fa", slot: int = 1,
                 input_sample_rate: int = DSD_NEO.input_sample_rate,
                 on_status: StatusCallback = None, extra_args: Optional[List[str]] = None,
                 connect_timeout: float = 5.0,
                 name: str = "SDR++ receiver → DSD-neo (digital)"):
        super().__init__(tuning, DSD_NEO.output_sample_rate, name, on_status)
        self.executable = executable
        self.host, self.port = host, int(port)
        self.protocol_flag = protocol_flag
        self.slot = 1 if int(slot) not in (1, 2) else int(slot)
        self.input_sample_rate = int(input_sample_rate)
        self.extra_args = list(extra_args or [])
        self.connect_timeout = connect_timeout
        self.process: Optional[subprocess.Popen] = None
        self._sock: Optional[socket.socket] = None
        self._pump: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._filler: Optional[threading.Thread] = None
        self.calls = CallTracker(self.slot)
        self.stderr_tail: List[str] = []
        self._last_audio_mono: Optional[float] = None
        self._last_pcm_mono: Optional[float] = None
        self.pcm_bytes_in = 0
        self._filled_until: float = 0.0
        self.exit_code: Optional[int] = None
        self._stopped_by_us = False
        self._producer_ended = False
        self._stdin_lock = threading.Lock()

    def command(self) -> List[str]:
        return [self.executable, "-i", "-",
                "-s", str(self.input_sample_rate), self.protocol_flag,
                "-V", str(self.slot), "-o", "-", *self.extra_args]

    def start(self) -> None:
        # The receiver connection first: without a producer no decoder is
        # started at all, and there is nothing else it could listen to.
        self._sock = socket.create_connection((self.host, self.port),
                                              timeout=self.connect_timeout)
        self._sock.settimeout(None)
        self.process = subprocess.Popen(
            self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=dict(os.environ), start_new_session=True)
        self._running = True
        self._finished = False
        self._start_time = utcnow()
        self._start_mono = time.monotonic()
        self._filled_until = 0.0
        self._reader = _Reader(self, self._read_stdout, self.sample_rate,
                               DSD_NEO.output_channels, self.slot - 1, self.block_frames,
                               wall_clock=True)
        self._reader.start()
        self._stderr_thread = threading.Thread(target=self._watch_stderr, daemon=True,
                                               name="dsd-neo events")
        self._stderr_thread.start()
        self._filler = threading.Thread(target=self._fill_silence, daemon=True,
                                        name="dsd-neo idle fill")
        self._filler.start()
        self._pump = threading.Thread(target=self._pump_pcm, daemon=True,
                                      name="receiver → dsd-neo pump")
        self._pump.start()
        self._report("connected", f"dsd-neo (pid {self.process.pid}) decoding "
                                  f"{self.protocol_flag} slot {self.slot}; BabelFishR feeds it "
                                  f"SDR++'s audio from {self.host}:{self.port}")

    # -- the receiver → decoder pump ---------------------------------------------
    def _recv(self) -> bytes:
        sock = self._sock
        if sock is None:
            return b""
        try:
            return sock.recv(8192)
        except OSError:
            return b""

    def _pump_pcm(self) -> None:
        stdin = self.process.stdin
        while self._running:
            data = self._recv()
            if not data:
                if not self._running or not self._reconnect():
                    break
                continue
            self.pcm_bytes_in += len(data)
            self._last_pcm_mono = time.monotonic()
            with self._stdin_lock:
                try:
                    stdin.write(data)
                    stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break                          # the decoder is gone; its watcher says so
        # Whatever ended it, the decoder's one input is closed now: it reads
        # end-of-file and ends itself (verified), so nothing else can be
        # decoded in the receiver's place.
        self._close_stdin()
        if self._running and not self._stopped_by_us:
            self._producer_ended = True
            self._running = False
            self._finished = True
            self._report("receiver-lost",
                         f"SDR++'s audio stopped arriving at {self.host}:{self.port} and did "
                         f"not come back within {self.RECONNECT_SECONDS:.0f}s; the stream is "
                         f"over and dsd-neo was closed with it. Nothing else is recorded in "
                         f"the receiver's place. Check SDR++ is running with its radio "
                         f"started, then start again.")
            self.read_wake()

    def _reconnect(self) -> bool:
        """The producer went away: try to get it back for a bounded time.
        dsd-neo simply waits on its pipe meanwhile."""
        self._report("upstream-lost", f"SDR++'s audio at {self.host}:{self.port} stopped; "
                                      f"reconnecting for up to {self.RECONNECT_SECONDS:.0f}s")
        self.calls.upstream_lost = True
        old, self._sock = self._sock, None
        if old is not None:
            try:
                old.close()
            except OSError:
                pass
        deadline = time.monotonic() + self.RECONNECT_SECONDS
        while self._running and time.monotonic() < deadline:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=1.0)
            except OSError:
                time.sleep(0.3)
                continue
            sock.settimeout(None)
            self._sock = sock
            self.calls.upstream_lost = False
            self._report("upstream-restored", "SDR++'s audio is arriving again")
            return True
        return False

    def _close_stdin(self) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        with self._stdin_lock:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass

    def _read_stdout(self, size: int) -> bytes:
        try:
            data = self.process.stdout.read1(size)
        except (OSError, ValueError, AttributeError):
            return b""
        if data:
            self._last_audio_mono = time.monotonic()
        return data

    def _fill_silence(self) -> None:
        """While dsd-neo is silent, keep the clock honest for the detector:
        emit zero blocks covering the idle time, at the block cadence."""
        block_seconds = self.block_frames / float(self.sample_rate)
        while self._running:
            time.sleep(block_seconds)
            now = time.monotonic()
            last = self._last_audio_mono if self._last_audio_mono is not None else self._start_mono
            if now - last < IDLE_FILL_SECONDS:
                continue
            elapsed = now - self._start_mono
            if elapsed - self._filled_until < block_seconds:
                continue
            self._filled_until = max(self._filled_until, elapsed - block_seconds)
            offset = max(self._filled_until, self._last_offset)
            self._filled_until = offset + block_seconds
            self._enqueue(self._generation, AudioBlock(
                samples=np.zeros(self.block_frames, dtype=np.float64),
                sample_rate=self.sample_rate,
                timestamp=self._start_time + _dt.timedelta(seconds=offset),
                offset=offset), 0)

    def _watch_stderr(self) -> None:
        try:
            for raw in iter(self.process.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.stderr_tail.append(line)
                del self.stderr_tail[:-200]
                kind = self.calls.note(line)
                if kind == "input-level-low":
                    self._report("input-level-low", "dsd-neo hears nothing on the channel (input level low)")
                elif kind in ("upstream-lost", "upstream-abandoned"):
                    # Cannot happen with a pipe input (there is no TCP input to
                    # lose) - reported all the same, never relied on.
                    self._report("decoder-warning", f"dsd-neo: {line.strip()}")
        except (OSError, ValueError):
            pass
        process = self.process
        code = process.poll() if process is not None else None
        if code is None and process is not None:
            try:
                code = process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                code = None
        self.exit_code = code
        if not self._stopped_by_us and not self._producer_ended:
            # dsd-neo ended on its own (killed, crashed): nothing more will be
            # decoded - said in its own words, whichever pipe closed first.
            self._report("decoder-exited", f"dsd-neo ended (exit {code}); "
                                           f"last output: {self.stderr_tail[-1] if self.stderr_tail else ''}")
            self._on_stream_end()

    def _note_event(self, line: str) -> None:            # kept for tests and callers
        self.calls.note(line)

    @property
    def receiving(self) -> bool:
        """PCM from SDR++ arrived at the pump within the last two seconds -
        the receiver's audio, decoded or not."""
        return self._last_pcm_mono is not None and time.monotonic() - self._last_pcm_mono < 2.0

    @property
    def decoding(self) -> bool:
        """dsd-neo produced speech within the last two seconds."""
        return self._last_audio_mono is not None and time.monotonic() - self._last_audio_mono < 2.0

    @property
    def decoder(self) -> Dict[str, object]:
        """A summary for status: protocol, sync count, the selected slot's
        current call, upstream state."""
        current = self.calls.current()
        return {"protocol": self.calls.protocol, "sync_lines": self.calls.sync_lines,
                "active_slot": self.calls.active_slot, "encrypted": current.get("encrypted", False),
                "talkgroup": current.get("talkgroup"), "unit_id": current.get("unit_id"),
                "color_code": current.get("color_code"), "nac": current.get("nac"),
                "upstream_lost": self.calls.upstream_lost,
                "producer_ended": self._producer_ended,
                "pcm_bytes_in": self.pcm_bytes_in, "decoding": self.decoding,
                "level_low": self.calls.level_low}

    def stop(self) -> None:
        """Return at once: the receiver socket and the decoder's input are
        closed (it ends itself on end-of-file) and it is asked to end. The
        process is waited for by whoever called stop (the capture's stopper
        thread), never the GUI thread: settle() does the waiting."""
        self._running = False
        self._stopped_by_us = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        self._close_stdin()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.read_wake()

    def settle(self, timeout: float = 5.0) -> bool:
        """Wait for dsd-neo to end; kill it if it will not. True when gone."""
        process = self.process
        if process is None:
            return True
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
        for stream in (process.stdout, process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self.exit_code = process.returncode
        return True

    @property
    def settled(self) -> bool:
        return self.process is None or self.process.poll() is not None

    def metadata(self) -> SignalMetadata:
        call = self.calls.current()
        meta = self.tuning.metadata(
            "sdrpp+dsd-neo", self.sample_rate, path="digital",
            decoder_flag=self.protocol_flag, slot=self.slot,
            decoder_input="stdin",
            encrypted=bool(call.get("encrypted", False)),
            color_code=call.get("color_code"),
            nac=call.get("nac"),
            sync_lines=self.calls.sync_lines,
            upstream_lost=self.calls.upstream_lost,
            dropped_frames=self.dropped_frames)
        meta.protocol = str(self.calls.protocol or "") if call else ""
        meta.talkgroup = str(call.get("talkgroup") or "")
        meta.unit_id = str(call.get("unit_id") or "")
        return meta
