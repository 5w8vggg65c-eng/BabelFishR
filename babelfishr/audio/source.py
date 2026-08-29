"""Audio input sources.

Live capture and WAV replay implement the *same* interface and feed the *same*
pipeline, so anything that can be reproduced from a file can be tested without
hardware - which is the only way this project can be developed anywhere other
than in front of the radio.
"""

from __future__ import annotations

import abc
import dataclasses
import datetime as _dt
import logging
import queue
import threading
import time
from typing import Callable, Iterator, List, Optional

import numpy as np

from ..dsp.filters import dbfs, rms
from ..models import utcnow
from .devices import (AudioBackendUnavailable, AudioDevice, DeviceIdentity,
                      InputDeviceMissing, find_device, resolve_identity)
from .wavefile import read_wav

log = logging.getLogger(__name__)

CLIP_THRESHOLD = 0.999


@dataclasses.dataclass
class AudioBlock:
    """A contiguous chunk of mono audio, exactly as captured."""

    samples: np.ndarray
    sample_rate: int
    timestamp: _dt.datetime
    offset: float
    """Seconds since the source started."""

    @property
    def duration(self) -> float:
        return self.samples.size / float(self.sample_rate or 1)

    @property
    def peak(self) -> float:
        return float(np.max(np.abs(self.samples))) if self.samples.size else 0.0

    @property
    def peak_dbfs(self) -> float:
        return dbfs(self.peak)

    @property
    def rms_dbfs(self) -> float:
        return dbfs(rms(self.samples))

    @property
    def clipped_samples(self) -> int:
        if not self.samples.size:
            return 0
        return int(np.count_nonzero(np.abs(self.samples) >= CLIP_THRESHOLD))

    @property
    def clipped(self) -> bool:
        return self.clipped_samples > 0


class AudioSource(abc.ABC):
    """Common interface for anything that produces audio blocks."""

    name: str = "source"
    sample_rate: int = 48_000

    @abc.abstractmethod
    def start(self) -> None:
        ...

    @abc.abstractmethod
    def stop(self) -> None:
        ...

    @abc.abstractmethod
    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        """Next block, or ``None`` on timeout / end of stream."""

    @property
    @abc.abstractmethod
    def running(self) -> bool:
        ...

    @property
    def finished(self) -> bool:
        """True when no further audio will ever arrive (files end; live does not)."""
        return False

    def blocks(self, timeout: float = 1.0) -> Iterator[AudioBlock]:
        while self.running or not self.finished:
            block = self.read(timeout=timeout)
            if block is None:
                if self.finished:
                    return
                continue
            yield block

    def __enter__(self) -> "AudioSource":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()


class ReplayAudioSource(AudioSource):
    """Feed a WAV file through the pipeline exactly as if it were live.

    ``realtime=True`` paces the blocks at wall-clock speed, which is what you
    want when exercising the UI; the default runs as fast as possible, which is
    what you want in tests.
    """

    def __init__(self, path: str, block_size: int = 2048, realtime: bool = False,
                 start_time: Optional[_dt.datetime] = None, loop: bool = False):
        self.path = str(path)
        self.block_size = int(block_size)
        self.realtime = realtime
        self.loop = loop
        self._samples, self.sample_rate = read_wav(self.path)
        self.name = f"replay:{self.path}"
        self._position = 0
        self._running = False
        self._finished = False
        self._start_time = start_time or utcnow()
        self._t0 = 0.0

    @property
    def duration(self) -> float:
        return self._samples.size / float(self.sample_rate or 1)

    def start(self) -> None:
        self._running = True
        self._finished = False
        self._position = 0
        self._t0 = time.monotonic()

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self._finished

    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        if not self._running:
            return None
        if self._position >= self._samples.size:
            if self.loop:
                self._position = 0
            else:
                self._finished = True
                self._running = False
                return None

        chunk = self._samples[self._position:self._position + self.block_size]
        offset = self._position / float(self.sample_rate)
        self._position += chunk.size

        if self.realtime:
            target = self._t0 + offset
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(min(delay, timeout))

        return AudioBlock(
            samples=np.asarray(chunk, dtype=np.float64),
            sample_rate=self.sample_rate,
            timestamp=self._start_time + _dt.timedelta(seconds=offset),
            offset=offset,
        )


class LiveAudioSource(AudioSource):
    """PortAudio capture with reconnection after a device disappears.

    USB audio interfaces get unplugged, and a monitoring session should survive
    it: the stream is torn down, retried on a backoff, and the operator is told
    through ``on_status`` rather than the application simply going quiet.
    """

    def __init__(self, device: Optional[str] = None, sample_rate: int = 48_000,
                 block_size: int = 2048, channels: int = 1,
                 on_status: Optional[Callable[[str, str], None]] = None,
                 reconnect: bool = True, max_queue_blocks: int = 512,
                 identity: Optional[DeviceIdentity] = None):
        self.device_selector = device
        self.identity = identity
        """The device the operator actually chose.

        Once capture has started this is always set, even when the caller only
        supplied a selector string, so reconnection can never wander onto a
        different device that happens to have inherited the old index.
        """

        self.sample_rate = int(sample_rate)
        self.block_size = int(block_size)
        self.channels = int(channels)
        self.on_status = on_status
        self.reconnect = reconnect
        self.device: Optional[AudioDevice] = None
        self.connection_log: List[tuple] = []
        """(utc timestamp, event, detail) for every connect and disconnect."""

        self.name = f"live:{device or 'default'}"

        self._queue: "queue.Queue[AudioBlock]" = queue.Queue(maxsize=max_queue_blocks)
        self._stream = None
        self._running = False
        self._samples_seen = 0
        self._start_time: Optional[_dt.datetime] = None
        self._watchdog: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.dropped_blocks = 0
        self.overflow_count = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        try:
            import sounddevice  # type: ignore # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise AudioBackendUnavailable(
                "sounddevice/PortAudio is not available. Install the audio extra "
                "(pip install 'babelfishr[audio]'), or use replay mode."
            ) from exc

        self.device = self._resolve_device()
        # Pin the identity of whatever we actually opened. From here on the
        # only thing that can be reconnected to is this exact device.
        if self.identity is None or self.identity.empty:
            self.identity = self.device.identity
        self.name = f"live:{self.device.name}"
        self._stop_event.clear()
        self._start_time = utcnow()
        self._samples_seen = 0
        self._open_stream()
        self._running = True
        if self.reconnect:
            self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                              name="babelfishr-audio-watchdog")
            self._watchdog.start()

    def _resolve_device(self) -> AudioDevice:
        """The one device this source is allowed to open.

        Once an identity is pinned this goes through :func:`resolve_identity`,
        which matches on the CoreAudio UID or on the full composite identity
        and never on the PortAudio index - so the device is still found after
        it comes back on a different index, and a different device that
        inherits the old index is not mistaken for it.
        """
        if self.identity is not None and not self.identity.empty:
            match = resolve_identity(self.identity)
            if match is None:
                raise InputDeviceMissing(self.identity)
            if match.ambiguous:
                self._notify(
                    "ambiguous-device",
                    f"more than one connected input is indistinguishable from "
                    f"{self.identity.describe()}; using the first")
            return match.device

        device = find_device(self.device_selector)
        if device is None:
            raise AudioBackendUnavailable(
                f"no usable input device matching {self.device_selector!r}. "
                "Run 'babelfishr devices' to see what is available.")
        return device

    def _open_stream(self) -> None:
        import sounddevice as sd  # type: ignore

        assert self.device is not None
        self._stream = sd.InputStream(
            device=self.device.index,
            channels=min(self.channels, self.device.max_input_channels),
            samplerate=self.sample_rate,
            blocksize=self.block_size,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        self._notify("connected", f"capturing from {self.device.name}")

    def _callback(self, indata, frames, time_info, status) -> None:  # pragma: no cover
        """PortAudio realtime thread: do the minimum and never block."""
        if status:
            if getattr(status, "input_overflow", False):
                self.overflow_count += 1
            self._notify("stream-status", str(status))
        data = np.asarray(indata, dtype=np.float64)
        mono = data.mean(axis=1) if data.ndim > 1 else data
        offset = self._samples_seen / float(self.sample_rate)
        self._samples_seen += mono.size
        block = AudioBlock(
            samples=mono.copy(), sample_rate=self.sample_rate,
            timestamp=(self._start_time or utcnow()) + _dt.timedelta(seconds=offset),
            offset=offset,
        )
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            # Better to lose the oldest block than to stall the audio callback.
            self.dropped_blocks += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(block)
            except queue.Empty:  # pragma: no cover - race, harmless
                pass

    def _watch(self) -> None:  # pragma: no cover - needs real hardware
        """Reopen the stream if the device goes away mid-session."""
        backoff = 1.0
        while not self._stop_event.wait(1.0):
            stream = self._stream
            if stream is not None and getattr(stream, "active", False):
                backoff = 1.0
                continue
            self._notify("disconnected",
                         "input device stopped; attempting to reconnect")
            try:
                self._close_stream()
                # Only ever the same device. resolve_identity returns nothing
                # rather than something else, so a missing radio interface
                # keeps the session silent instead of quietly switching to the
                # laptop microphone.
                self.device = self._resolve_device()
                self._open_stream()
                self._notify("reconnected", f"resumed on {self.device.name}")
                backoff = 1.0
            except Exception as exc:  # noqa: BLE001
                self._notify("reconnect-failed", str(exc))
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2, 30.0)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.stop()
            stream.close()
        except Exception as exc:  # noqa: BLE001
            log.debug("error closing stream: %s", exc)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        self._close_stream()
        if self._watchdog is not None:
            self._watchdog.join(timeout=2.0)
            self._watchdog = None
        self._notify("stopped", "capture stopped")

    @property
    def running(self) -> bool:
        return self._running

    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _notify(self, kind: str, message: str) -> None:
        log.debug("audio %s: %s", kind, message)
        # Disconnections and recoveries are recorded with times, because after
        # the fact the operator needs to know exactly which minutes of a watch
        # were not being received.
        if kind in ("connected", "disconnected", "reconnected",
                    "reconnect-failed", "stopped"):
            self.connection_log.append((utcnow(), kind, message))
            log.info("audio input %s: %s", kind, message)
        if self.on_status is not None:
            try:
                self.on_status(kind, message)
            except Exception:  # noqa: BLE001 - a bad callback must not kill audio
                log.exception("audio status callback failed")


class CallbackAudioSource(AudioSource):
    """Programmatic source used by tests and by the calibration workflow."""

    def __init__(self, sample_rate: int = 48_000, name: str = "callback"):
        self.sample_rate = int(sample_rate)
        self.name = name
        self._queue: "queue.Queue[Optional[AudioBlock]]" = queue.Queue()
        self._running = False
        self._finished = False
        self._samples_seen = 0
        self._start_time = utcnow()

    def start(self) -> None:
        self._running = True
        self._finished = False

    def stop(self) -> None:
        self._running = False
        self._queue.put(None)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def finished(self) -> bool:
        return self._finished

    def push(self, samples: np.ndarray) -> None:
        offset = self._samples_seen / float(self.sample_rate)
        self._samples_seen += np.asarray(samples).size
        self._queue.put(AudioBlock(
            samples=np.asarray(samples, dtype=np.float64),
            sample_rate=self.sample_rate,
            timestamp=self._start_time + _dt.timedelta(seconds=offset),
            offset=offset,
        ))

    def read(self, timeout: float = 1.0) -> Optional[AudioBlock]:
        try:
            block = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if block is None:
            self._finished = True
            return None
        return block


def open_source(spec: str, **kwargs) -> AudioSource:
    """Build a source from a short spec string.

    ``replay:/path/to.wav`` - file replay; ``live:2`` or ``live:USB Audio`` -
    capture from a device; a bare path is treated as replay.
    """
    text = str(spec)
    if text.startswith("replay:"):
        return ReplayAudioSource(text[len("replay:"):], **kwargs)
    if text.startswith("live:"):
        selector = text[len("live:"):] or None
        return LiveAudioSource(device=selector, **kwargs)
    if text in ("live", "default"):
        return LiveAudioSource(device=None, **kwargs)
    return ReplayAudioSource(text, **kwargs)
