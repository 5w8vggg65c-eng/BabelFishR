"""The RECORDING WAVEFORM panel shown above the transcript.

It draws the selected transmission's own WAV clip: a true signed min/max
envelope streamed from the file, a clip-relative time ruler and, only while
that clip owns playback, a position marker.

Design notes
------------
* :func:`compute_envelope` is pure (no Qt). It streams the file through the
  stdlib ``wave`` module in bounded chunks and buckets by frame index, so
  memory is O(buckets + chunk) whatever the file size.
* No per-file normalisation that could make silence look like signal: the
  envelope holds raw sample values. :attr:`Envelope.display_gain` lifts
  quiet-but-real audio for readability only when its peak is above
  ``DISPLAY_GAIN_FLOOR_DBFS``; below that the waveform is drawn as recorded.
* Loading runs on a worker thread. Each request carries a monotonically
  increasing token and a completion with an older token is discarded, so a
  slow clip can never overwrite the clip selected after it.
* The playhead can be clicked or dragged (Left/Right from the keyboard) to
  seek - only within the recording being played or paused and drawn here,
  through the same controller as the thread's player, so the two never
  compete. Nothing else in the widget changes playback.
"""

from __future__ import annotations

import collections
import os
import threading
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np
import shiboken6
from PySide6 import QtCore, QtGui, QtWidgets

DEFAULT_BUCKETS = 1200
#: Frames read per ``readframes`` call while streaming...
CHUNK_FRAMES = 65_536
#: ...but never more bytes than this per read, whatever the header claims:
#: frames alone would let an extreme channel count ask for gigabytes.
MAX_CHUNK_BYTES = 4 * 1024 * 1024
#: Display gain is applied only when the clip's peak exceeds this level.
DISPLAY_GAIN_FLOOR_DBFS = -40.0
#: Never lift a clip by more than this factor (about +26 dB).
DISPLAY_GAIN_MAX = 20.0

STATE_EMPTY = "empty"        # nothing selected
STATE_NONE = "none"          # message has no recording
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_MISSING = "missing"
STATE_ERROR = "error"
STATE_CLIP_EMPTY = "clip_empty"  # a valid WAV with zero frames

NO_SELECTION_TEXT = "Select a message to see its recording."
NO_RECORDING_TEXT = "This message has no recording."
LOADING_TEXT = "Loading waveform…"

_FALLBACK_COLOURS = {
    "window": "#0b1622", "surface": "#0f1d2b", "surface_raised": "#142636",
    "border": "#24384a", "text": "#d8e3ea", "text_muted": "#8aa0b0",
    "accent": "#2ec4b6", "warning": "#e0a526", "error": "#e05a4f",
    "ok": "#3fb950",
}


def same_audio(a: Optional[str], b: Optional[str]) -> bool:
    """Do two recording paths name the same file?"""
    if not a or not b:
        return False
    try:
        return os.path.realpath(str(a)) == os.path.realpath(str(b))
    except (OSError, ValueError):
        return str(a) == str(b)


class WaveformCancelled(Exception):
    """The envelope was no longer wanted; reading stopped at a chunk boundary."""


class WaveformError(ValueError):
    """The file is not a readable PCM WAV. ``str(exc)`` is a short reason."""


@dataclass
class Envelope:
    mins: np.ndarray
    maxs: np.ndarray
    sample_rate: int
    frames: int
    duration_s: float
    channels: int
    sample_width: int
    #: Frames actually analysed (less than ``frames`` if capped).
    analysed_frames: int = 0

    @property
    def empty(self) -> bool:
        return self.frames == 0 or self.mins.size == 0

    @property
    def truncated(self) -> bool:
        return self.analysed_frames < self.frames

    @property
    def peak(self) -> float:
        if self.mins.size == 0:
            return 0.0
        return float(max(abs(float(self.mins.min())), abs(float(self.maxs.max()))))

    @property
    def display_gain(self) -> float:
        peak = self.peak
        if peak <= 10 ** (DISPLAY_GAIN_FLOOR_DBFS / 20.0):
            return 1.0
        return float(max(1.0, min(0.95 / peak, DISPLAY_GAIN_MAX)))


def _decode(raw: bytes, width: int) -> np.ndarray:
    if width == 1:
        return (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    if width == 2:
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        expanded = np.zeros((packed.shape[0], 4), dtype=np.uint8)
        expanded[:, 1:] = packed
        return (expanded.view("<i4").ravel().astype(np.float64)
                / 2147483648.0).astype(np.float32)
    if width == 4:
        return (np.frombuffer(raw, dtype="<i4").astype(np.float64)
                / 2147483648.0).astype(np.float32)
    raise WaveformError(f"unsupported sample width ({width * 8}-bit)")


def compute_envelope(path: str, buckets: int = DEFAULT_BUCKETS,
                     max_frames: Optional[int] = None,
                     chunk_frames: int = CHUNK_FRAMES,
                     cancelled: Optional[Callable[[], bool]] = None) -> Envelope:
    """Stream ``path`` and return its signed min/max envelope.

    Raises FileNotFoundError for a missing file and :class:`WaveformError`
    for anything that is not a readable 8/16/24/32-bit PCM WAV. *cancelled*
    is asked before every chunk; when it answers True reading stops there and
    :class:`WaveformCancelled` is raised - no partial envelope is returned.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    try:
        handle = wave.open(str(path), "rb")
    except wave.Error as exc:
        raise WaveformError(f"not a PCM WAV file ({exc})") from None
    except EOFError:
        raise WaveformError("file is truncated") from None
    except Exception as exc:  # struct.error and friends on garbage headers
        raise WaveformError(f"corrupt header ({type(exc).__name__})") from None

    with handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        frames = handle.getnframes()
        if channels < 1:
            raise WaveformError("no audio channels")
        if width not in (1, 2, 3, 4):
            raise WaveformError(f"unsupported sample width ({width * 8}-bit)")
        if rate <= 0:
            raise WaveformError("invalid sample rate")
        duration = frames / float(rate)

        target = frames if max_frames is None else max(0, min(frames, int(max_frames)))
        nb = int(min(max(1, buckets), target)) if target else 0
        mins = np.zeros(nb, dtype=np.float32)
        maxs = np.zeros(nb, dtype=np.float32)
        seen = np.zeros(nb, dtype=bool)
        frame_bytes = width * channels
        chunk = max(1, min(int(chunk_frames), MAX_CHUNK_BYTES // frame_bytes))
        start = 0
        while start < target:
            if cancelled is not None and cancelled():
                raise WaveformCancelled(path)
            want = min(chunk, target - start)
            try:
                raw = handle.readframes(want)
            except Exception as exc:  # noqa: BLE001
                raise WaveformError(f"read failed ({type(exc).__name__})") from None
            got = len(raw) // (width * channels)
            if got <= 0:
                break  # header promised more data than the file holds
            data = _decode(raw[:got * width * channels], width)
            if channels > 1:
                block = data.reshape(got, channels)
                cmin, cmax = block.min(axis=1), block.max(axis=1)
            else:
                cmin = cmax = data
            idx = (np.arange(start, start + got, dtype=np.int64) * nb) // target
            # idx is sorted: reduce each run of equal bucket indices at once.
            cuts = np.concatenate(([0], np.flatnonzero(np.diff(idx)) + 1))
            b = idx[cuts]
            rmin = np.minimum.reduceat(cmin, cuts)
            rmax = np.maximum.reduceat(cmax, cuts)
            fresh = ~seen[b]
            mins[b] = np.where(fresh, rmin, np.minimum(mins[b], rmin))
            maxs[b] = np.where(fresh, rmax, np.maximum(maxs[b], rmax))
            seen[b] = True
            start += got
        analysed = start
        if analysed < target and nb:
            # Truncated data: keep only buckets that received samples.
            keep = int(seen.sum())
            mins, maxs = mins[:keep], maxs[:keep]
    return Envelope(mins=mins, maxs=maxs, sample_rate=int(rate),
                    frames=int(frames), duration_s=float(duration),
                    channels=int(channels), sample_width=int(width),
                    analysed_frames=int(analysed))


# -- formatting -----------------------------------------------------------

def format_duration(seconds: float) -> str:
    """MM:SS (H:MM:SS from an hour), rounded to the nearest second."""
    total = int(round(max(0.0, seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_tick(seconds: float, clip_duration: float) -> str:
    """Ruler label: SS.s for clips under ten seconds, else as duration."""
    if clip_duration < 10.0:
        return f"{max(0.0, seconds):04.1f}"
    return format_duration(seconds)


_NICE_STEPS = (0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 15, 20, 30, 60, 120, 300,
               600, 900, 1800, 3600)


def ruler_ticks(duration: float, width_px: int) -> List[Tuple[float, str]]:
    """Clip-relative ruler labels from 0 to ``duration`` that fit the width."""
    if duration <= 0:
        return [(0.0, format_tick(0.0, duration))]
    max_labels = max(2, min(7, int(width_px) // 70))
    step = _NICE_STEPS[-1]
    for candidate in _NICE_STEPS:
        if duration / candidate <= max_labels - 1:
            step = candidate
            break
    ticks: List[Tuple[float, str]] = []
    k = 0
    while True:
        t = k * step
        # Leave room before the end label so the two never overlap.
        if t > duration - step * 0.5 and k > 0:
            break
        ticks.append((t, format_tick(t, duration)))
        k += 1
    ticks.append((duration, format_tick(duration, duration)))
    return ticks


# -- async loading --------------------------------------------------------

class _Bridge(QtCore.QObject):
    """Lives on the GUI thread; worker threads emit through it (queued)."""

    done = QtCore.Signal(int, object)


def _compute_for_widget(path: str, max_frames: Optional[int],
                        cancelled: Optional[Callable[[], bool]] = None) -> Envelope:
    # Looked up at call time so tests can monkeypatch compute_envelope.
    return compute_envelope(path, max_frames=max_frames, cancelled=cancelled)


class _Loader:
    """One worker per waveform, one pending request, cooperative cancellation.

    - At most one envelope is being computed at any moment.
    - A new request replaces any request still waiting (only the latest is
      kept) and cancels the one running: it stops at its next chunk.
    - clear() and shutdown() cancel too. shutdown() also ends the worker
      thread; nothing waits for it on the GUI thread - it notices within one
      chunk read and exits.
    - The worker holds the loader and the bridge, never the widget, so a
      closed window is not kept alive by a read in progress. When the widget
      is gone (deleted, or collected without shutdown()) its bridge stops
      being valid: a read in progress stops at its next chunk and an idle
      worker leaves within a second. No signal from the dying widget is
      needed, so nothing is called on a half-collected object.

    Not a cap: whatever is wanted last is always read in full.
    """

    def __init__(self, bridge: "_Bridge"):
        self._bridge = bridge
        self._cond = threading.Condition()
        self._wanted = 0                     # token of the latest request
        self._pending: Optional[Tuple[int, str, Optional[int]]] = None
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        #: Bookkeeping for tests and diagnostics (bounded).
        self.running_token: Optional[int] = None
        self.started: "collections.deque[int]" = collections.deque(maxlen=64)
        self.cancelled_tokens: "collections.deque[int]" = collections.deque(maxlen=64)
        self.completed: "collections.deque[int]" = collections.deque(maxlen=64)

    # -- requests (GUI thread) ---------------------------------------------
    def request(self, token: int, path: str, max_frames: Optional[int]) -> bool:
        """Queue *token* as the one wanted. False once shut down."""
        with self._cond:
            if self._closed:
                return False
            self._wanted = token
            self._pending = (token, path, max_frames)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="waveform-loader",
                                                daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def cancel(self, token: int) -> None:
        """Nothing before *token* is wanted any more."""
        with self._cond:
            self._wanted = token
            self._pending = None
            self._cond.notify_all()

    def shutdown(self) -> None:
        with self._cond:
            self._closed = True
            self._pending = None
            self._wanted += 1
            self._cond.notify_all()

    @property
    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def join(self, timeout: float) -> bool:
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def idle(self) -> bool:
        with self._cond:
            return self._pending is None and self.running_token is None

    # -- the worker -------------------------------------------------------------
    def _widget_gone(self) -> bool:
        try:
            return not shiboken6.isValid(self._bridge)
        except Exception:  # noqa: BLE001 - treat anything odd as gone
            return True

    def _run(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    if self._widget_gone():
                        self._closed = True
                        break
                    self._cond.wait(1.0)
                if self._closed:
                    return
                token, path, max_frames = self._pending
                self._pending = None
                self.running_token = token
                self.started.append(token)

            def obsolete(token=token) -> bool:
                return self._closed or self._wanted != token or self._widget_gone()

            try:
                result: object = _compute_for_widget(path, max_frames, cancelled=obsolete)
            except WaveformCancelled:
                result = None
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                result = exc
            with self._cond:
                self.running_token = None
                if result is None or obsolete():
                    self.cancelled_tokens.append(token)
                    continue
                self.completed.append(token)
            try:
                self._bridge.done.emit(token, result)
            except RuntimeError:
                return                       # the widget is gone


class RecordingWaveform(QtWidgets.QWidget):
    """Waveform of the selected transmission's own clip (read-only).

    The playhead can be clicked or dragged to seek, but only within the
    recording that is playing (or paused) and drawn here; Left/Right move it
    by 5 s from the keyboard. Playing stays playing, paused stays paused.
    """

    loaded = QtCore.Signal(str)  # tx id, emitted on any terminal state

    TITLE_H = 18
    RULER_H = 22
    HEIGHT = 124

    def __init__(self, parent: Optional[QtWidgets.QWidget] = None):
        super().__init__(parent)
        self.setObjectName("recordingWaveform")
        self.setAccessibleName("Recording waveform")
        self.setFixedHeight(self.HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                           QtWidgets.QSizePolicy.Fixed)
        self.setMinimumWidth(160)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setAccessibleDescription(
            "While this recording plays or is paused: click or drag to move the "
            "playhead; Left and Right arrows move it 5 seconds.")
        self._drag = None

        self.state = STATE_EMPTY
        self.message = NO_SELECTION_TEXT
        self.envelope: Optional[Envelope] = None
        self.current_tx_id: Optional[str] = None
        self.current_path: Optional[str] = None

        self.marker_visible = False
        self.marker_ms = 0
        self._marker_dur_ms = 0

        self._token = 0
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._on_done, QtCore.Qt.QueuedConnection)
        self._loader = _Loader(self._bridge)
        self._controller = None
        #: Optional cap on frames analysed per clip (None = whole file).
        self.max_frames: Optional[int] = None
        self._column_cache: Optional[Tuple[int, int, np.ndarray, np.ndarray]] = None
        self._update_accessibility()

    # -- public API ---------------------------------------------------------

    def show_recording(self, tx_id: str, path: Optional[str]) -> None:
        self._token += 1
        token = self._token
        # Whatever is read now is no longer wanted - even when this message
        # needs no read at all (no recording, missing file).
        self._loader.cancel(token)
        self.current_tx_id = tx_id
        self.current_path = path or None
        self.envelope = None
        self._column_cache = None
        self._sync_marker()
        if not path:
            self._set_state(STATE_NONE, NO_RECORDING_TEXT)
            return
        if not os.path.isfile(path):
            self._set_state(STATE_MISSING, self._missing_text(path))
            return
        self._set_state(STATE_LOADING, LOADING_TEXT, terminal=False)
        # Replaces any request still waiting and cancels the one running.
        if not self._loader.request(token, path, self.max_frames):
            self._set_state(STATE_ERROR, "Waveform loading has stopped "
                                         "(the window is closing).")

    def clear(self, message: str = NO_SELECTION_TEXT) -> None:
        self._token += 1  # any in-flight load is now stale
        self._loader.cancel(self._token)   # and stops at its next chunk
        self.current_tx_id = None
        self.current_path = None
        self.envelope = None
        self._column_cache = None
        self.marker_visible = False
        self._set_state(STATE_EMPTY, message, terminal=False)

    def shutdown(self) -> None:
        """Stop loading for good: cancel any read and end the worker. Does
        not wait - the worker leaves at its next chunk boundary."""
        self._token += 1
        self._loader.shutdown()

    def set_playback(self, controller) -> None:
        """Follow ``controller`` (PlaybackController-like); None detaches."""
        old = self._controller
        if old is not None:
            for signal, slot in ((old.changed, self._on_playback_changed),
                                 (old.positionChanged, self._on_position)):
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self._controller = controller
        if controller is not None:
            controller.changed.connect(self._on_playback_changed)
            controller.positionChanged.connect(self._on_position)
        self._sync_marker()
        self.update()

    # -- seeking --------------------------------------------------------------
    SEEK_KEY_MS = 5000

    def _seek_fraction(self, x: float) -> Optional[float]:
        area = self._wave_rect()
        if area.width() <= 1:
            return None
        return max(0.0, min(1.0, (x - area.left()) / float(area.width() - 1)))

    def _duration_ms(self) -> int:
        dur = 0
        if self._controller is not None and self.current_tx_id:
            try:
                dur = int(self._controller.position_for(self.current_tx_id)[1])
            except Exception:  # noqa: BLE001
                dur = 0
        if dur <= 0 and self.envelope is not None:
            dur = int(self.envelope.duration_s * 1000)
        return dur

    def _seek_to_fraction(self, fraction: float) -> bool:
        dur = self._duration_ms()
        if dur <= 0 or not self._owns_playback():
            return False
        return bool(self._controller.seek_to(
            self.current_tx_id, self.current_path, int(round(fraction * dur))))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == QtCore.Qt.LeftButton and self._owns_playback():
            fraction = self._seek_fraction(event.position().x())
            if fraction is not None:
                # Bound to this recording: if playback moves on (the
                # sequence starts the next clip) the drag lets go.
                self._drag = (self.current_tx_id, self.current_path)
                self._seek_to_fraction(fraction)
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        drag = getattr(self, "_drag", None)
        if drag is not None and event.buttons() & QtCore.Qt.LeftButton:
            if drag != (self.current_tx_id, self.current_path) or not self._owns_playback():
                self._drag = None                  # stale: another clip owns playback
                return
            fraction = self._seek_fraction(event.position().x())
            if fraction is not None:
                self._seek_to_fraction(fraction)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        step = {QtCore.Qt.Key_Left: -self.SEEK_KEY_MS,
                QtCore.Qt.Key_Right: self.SEEK_KEY_MS}.get(event.key())
        if step is not None and self._owns_playback():
            position = self._controller.position_for(self.current_tx_id)[0]
            self._controller.seek_to(self.current_tx_id, self.current_path,
                                     int(position) + step)
            event.accept()
            return
        super().keyPressEvent(event)

    def wait_until_loaded(self, timeout: float = 5.0) -> bool:
        """Process events until no load is pending. True if it settled."""
        timer = QtCore.QElapsedTimer()
        timer.start()
        app = QtWidgets.QApplication.instance()
        while self.state == STATE_LOADING:
            if timer.elapsed() > timeout * 1000:
                return False
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            self._loader.join(0.005)
        return True

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Wait until the worker has nothing running or pending, then deliver
        any result. For tests; the application never waits on it."""
        timer = QtCore.QElapsedTimer()
        timer.start()
        while not self._loader.idle() and timer.elapsed() < timeout * 1000:
            self._loader.join(0.01)
        QtWidgets.QApplication.instance().processEvents()
        QtCore.QCoreApplication.sendPostedEvents()
        QtWidgets.QApplication.instance().processEvents()

    def ruler_labels(self, width: Optional[int] = None) -> List[Tuple[float, str]]:
        if self.envelope is None:
            return []
        w = self._wave_rect().width() if width is None else width
        return ruler_ticks(self.envelope.duration_s, w)

    def title_text(self) -> str:
        if not self.current_path:
            return ""
        name = os.path.basename(self.current_path)
        env = self.envelope
        if env is not None:
            text = f"{name} · {format_duration(env.duration_s)}"
            # Say what the drawing is not: the file's true level, or all of it.
            if env.truncated and (self.max_frames is None
                                  or env.analysed_frames < min(env.frames, self.max_frames)):
                readable = env.analysed_frames / float(env.sample_rate or 1)
                text += f" · file shorter than its header: {readable:.1f} s readable"
            gain = env.display_gain
            if gain > 1.05:
                text += f" · shown \u00d7{gain:.0f}"
            return text
        return name

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802
        return QtCore.QSize(600, self.HEIGHT)

    # -- loading ------------------------------------------------------------

    def _on_done(self, token: int, result: object) -> None:
        if token != self._token:
            return  # stale: the selection moved on
        path = self.current_path or ""
        if isinstance(result, Envelope):
            if result.empty:
                self._set_state(STATE_CLIP_EMPTY, "Recording is empty (0 s)")
                self.envelope = None
            else:
                self.envelope = result
                self._column_cache = None
                self._set_state(STATE_READY, "")
        elif isinstance(result, FileNotFoundError):
            self._set_state(STATE_MISSING, self._missing_text(path))
        else:
            reason = str(result) or type(result).__name__
            self._set_state(STATE_ERROR, f"Recording could not be read: {reason}")
        self._sync_marker()

    # -- playback -----------------------------------------------------------

    def _owns_playback(self) -> bool:
        c = self._controller
        if c is None or self.current_tx_id is None:
            return False
        try:
            state = c.state_for(self.current_tx_id)
        except Exception:  # noqa: BLE001
            return False
        if state not in ("playing", "paused"):
            return False
        # The same message can be playing a *different* file (its decoded
        # audio): this waveform is the original's, so it owns the marker
        # only when the file playing is the one drawn.
        return same_audio(getattr(c, "path", None), self.current_path)

    def _sync_marker(self) -> None:
        visible = self._owns_playback()
        if visible:
            try:
                pos, dur = self._controller.position_for(self.current_tx_id)
            except Exception:  # noqa: BLE001
                pos, dur = 0, 0
            self.marker_ms, self._marker_dur_ms = int(pos), int(dur)
        self.marker_visible = visible
        self.update()

    def _on_playback_changed(self, _ids=None) -> None:
        self._sync_marker()

    def _on_position(self, owner: str, pos_ms: int, dur_ms: int) -> None:
        if owner != self.current_tx_id:
            if self.marker_visible and not self._owns_playback():
                self.marker_visible = False
                self.update()
            return
        self.marker_ms, self._marker_dur_ms = int(pos_ms), int(dur_ms)
        self.marker_visible = self._owns_playback()
        self.update()

    def marker_fraction(self) -> float:
        dur = self._marker_dur_ms
        if dur <= 0 and self.envelope is not None:
            dur = int(self.envelope.duration_s * 1000)
        if dur <= 0:
            return 0.0
        return max(0.0, min(1.0, self.marker_ms / dur))

    # -- state/a11y ---------------------------------------------------------

    @staticmethod
    def _missing_text(path: str) -> str:
        return f"Recording file missing: {os.path.basename(path)}"

    def _set_state(self, state: str, message: str, terminal: bool = True) -> None:
        self.state = state
        self.message = message
        self._update_accessibility()
        self.update()
        if terminal and self.current_tx_id is not None:
            self.loaded.emit(self.current_tx_id)

    def _update_accessibility(self) -> None:
        if self.state == STATE_READY and self.envelope is not None:
            text = (f"{os.path.basename(self.current_path or '')}, duration "
                    f"{format_duration(self.envelope.duration_s)}. Read-only.")
        else:
            name = os.path.basename(self.current_path or "")
            text = f"{name}: {self.message}" if name else self.message
        self.setAccessibleDescription(text)

    # -- painting -----------------------------------------------------------

    def _colours(self) -> dict:
        colours = dict(_FALLBACK_COLOURS)
        try:
            from . import theme
            colours.update({k: v for k, v in theme.tokens(self).items() if v})
        except Exception:  # noqa: BLE001 - never fail a paint over theming
            pass
        return colours

    def _wave_rect(self) -> QtCore.QRect:
        r = self.rect().adjusted(8, 0, -8, 0)
        return QtCore.QRect(r.left(), self.TITLE_H + 4, max(1, r.width()),
                            max(1, self.height() - self.TITLE_H - 4 - self.RULER_H))

    def _columns(self, width: int) -> Tuple[np.ndarray, np.ndarray]:
        env = self.envelope
        cache = self._column_cache
        if cache is not None and cache[0] == id(env) and cache[1] == width:
            return cache[2], cache[3]
        n = env.mins.size
        # Analysed portion only spans its share of the width if capped.
        span = width
        if env.truncated and env.frames:
            span = max(1, int(round(width * env.analysed_frames / env.frames)))
        cols = np.arange(span, dtype=np.int64)
        starts = (cols * n) // span
        if span <= n:
            cmin = np.minimum.reduceat(env.mins, starts)
            cmax = np.maximum.reduceat(env.maxs, starts)
        else:
            cmin, cmax = env.mins[starts], env.maxs[starts]
        self._column_cache = (id(env), width, cmin, cmax)
        return cmin, cmax

    def paintEvent(self, _event) -> None:  # noqa: N802
        c = self._colours()
        p = QtGui.QPainter(self)
        try:
            p.fillRect(self.rect(), QtGui.QColor(c["surface"]))
            p.setPen(QtGui.QColor(c["border"]))
            p.drawRect(self.rect().adjusted(0, 0, -1, -1))
            font = QtGui.QFont(self.font())
            p.setFont(font)
            fm = QtGui.QFontMetrics(font)

            title = QtCore.QRect(8, 2, self.width() - 16, self.TITLE_H)
            p.setPen(QtGui.QColor(c["text_muted"]))
            p.drawText(title, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter,
                       "RECORDING WAVEFORM")
            right = fm.elidedText(self.title_text(), QtCore.Qt.ElideMiddle,
                                  max(0, title.width() - fm.horizontalAdvance(
                                      "RECORDING WAVEFORM") - 16))
            p.setPen(QtGui.QColor(c["text"]))
            p.drawText(title, QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, right)

            area = self._wave_rect()
            if self.state != STATE_READY or self.envelope is None:
                p.setPen(QtGui.QColor(c["error"] if self.state in
                                      (STATE_MISSING, STATE_ERROR)
                                      else c["text_muted"]))
                p.drawText(area.adjusted(0, 0, 0, self.RULER_H // 2),
                           QtCore.Qt.AlignCenter | QtCore.Qt.TextWordWrap,
                           self.message)
                return
            self._paint_wave(p, area, c)
            self._paint_ruler(p, area, c, font)
            if self.marker_visible:
                self._paint_marker(p, area, c, fm)
        finally:
            p.end()

    def _paint_wave(self, p: QtGui.QPainter, area: QtCore.QRect, c: dict) -> None:
        mid = area.top() + area.height() / 2.0
        half = area.height() / 2.0 - 1
        accent = QtGui.QColor(c["accent"])
        centre = QtGui.QColor(c["border"])
        p.setPen(QtGui.QPen(centre, 1))
        p.drawLine(QtCore.QPointF(area.left(), mid), QtCore.QPointF(area.right(), mid))

        cmin, cmax = self._columns(area.width())
        gain = self.envelope.display_gain
        tops = mid - np.clip(cmax * gain, -1, 1) * half
        bots = mid - np.clip(cmin * gain, -1, 1) * half
        split = (int(self.marker_fraction() * area.width())
                 if self.marker_visible else area.width() + 1)
        played = QtGui.QPen(accent, 1)
        dim = QtGui.QColor(accent)
        dim.setAlpha(150)
        unplayed = QtGui.QPen(dim, 1)
        left = area.left()
        lines_played, lines_rest = [], []
        for x in range(tops.size):
            top, bot = float(tops[x]), float(bots[x])
            if bot - top < 1.0:  # keep silence a flat hairline, not a gap
                top = bot = (top + bot) / 2.0
            line = QtCore.QLineF(left + x + 0.5, top, left + x + 0.5, bot + 0.01)
            (lines_played if x < split else lines_rest).append(line)
        if lines_played:
            p.setPen(played)
            p.drawLines(lines_played)
        if lines_rest:
            p.setPen(unplayed if self.marker_visible else played)
            p.drawLines(lines_rest)

    def _paint_ruler(self, p, area: QtCore.QRect, c: dict, font: QtGui.QFont) -> None:
        y = area.bottom() + 3
        p.setPen(QtGui.QPen(QtGui.QColor(c["border"]), 1))
        p.drawLine(area.left(), y, area.right(), y)
        small = QtGui.QFont(font)
        if small.pointSizeF() > 0:
            small.setPointSizeF(max(7.0, small.pointSizeF() - 1.5))
        p.setFont(small)
        fm = QtGui.QFontMetrics(small)
        dur = self.envelope.duration_s or 1.0
        ticks = self.ruler_labels(area.width())
        for i, (t, label) in enumerate(ticks):
            x = area.left() + int(round(t / dur * (area.width() - 1)))
            p.setPen(QtGui.QColor(c["border"]))
            p.drawLine(x, y, x, y + 3)
            w = fm.horizontalAdvance(label)
            if i == 0:
                tx = x
            elif i == len(ticks) - 1:
                tx = x - w
            else:
                tx = x - w // 2
            p.setPen(QtGui.QColor(c["text_muted"]))
            p.drawText(tx, y + 4 + fm.ascent(), label)
        p.setFont(font)

    def _paint_marker(self, p, area: QtCore.QRect, c: dict, fm) -> None:
        x = area.left() + int(round(self.marker_fraction() * (area.width() - 1)))
        accent = QtGui.QColor(c["accent"])
        p.setPen(QtGui.QPen(QtGui.QColor(c["text"]), 1))
        p.drawLine(x, area.top(), x, area.bottom())
        label = format_duration(self.marker_ms / 1000.0)
        w, h = fm.horizontalAdvance(label) + 8, fm.height() + 2
        bx = min(max(area.left(), x - w // 2), area.right() - w)
        box = QtCore.QRect(bx, area.top(), w, h)
        p.fillRect(box, QtGui.QColor(c["surface_raised"]))
        p.setPen(accent)
        p.drawRect(box.adjusted(0, 0, -1, -1))
        p.setPen(QtGui.QColor(c["text"]))
        p.drawText(box, QtCore.Qt.AlignCenter, label)
