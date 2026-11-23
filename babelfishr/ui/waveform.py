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
* The widget is deliberately **read-only**: there is no click-to-seek. It is a
  truthful picture of the recording and of where playback is, not a second
  transport control competing with the thread's player.
"""

from __future__ import annotations

import os
import threading
import wave
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

DEFAULT_BUCKETS = 1200
#: Frames read per ``readframes`` call while streaming.
CHUNK_FRAMES = 65_536
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
                     chunk_frames: int = CHUNK_FRAMES) -> Envelope:
    """Stream ``path`` and return its signed min/max envelope.

    Raises FileNotFoundError for a missing file and :class:`WaveformError`
    for anything that is not a readable 8/16/24/32-bit PCM WAV.
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
        chunk = max(1, int(chunk_frames))
        start = 0
        while start < target:
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


def _compute_for_widget(path: str, max_frames: Optional[int]) -> Envelope:
    # Looked up at call time so tests can monkeypatch compute_envelope.
    return compute_envelope(path, max_frames=max_frames)


class RecordingWaveform(QtWidgets.QWidget):
    """Waveform of the selected transmission's own clip (read-only).

    Deliberately no click-to-seek: this panel shows the recording and where
    playback is, truthfully; transport stays with the thread's controls.
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

        self.state = STATE_EMPTY
        self.message = NO_SELECTION_TEXT
        self.envelope: Optional[Envelope] = None
        self.current_tx_id: Optional[str] = None
        self.current_path: Optional[str] = None

        self.marker_visible = False
        self.marker_ms = 0
        self._marker_dur_ms = 0

        self._token = 0
        self._threads: List[threading.Thread] = []
        self._bridge = _Bridge(self)
        self._bridge.done.connect(self._on_done, QtCore.Qt.QueuedConnection)
        self._controller = None
        #: Optional cap on frames analysed per clip (None = whole file).
        self.max_frames: Optional[int] = None
        self._column_cache: Optional[Tuple[int, int, np.ndarray, np.ndarray]] = None
        self._update_accessibility()

    # -- public API ---------------------------------------------------------

    def show_recording(self, tx_id: str, path: Optional[str]) -> None:
        self._token += 1
        token = self._token
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
        thread = threading.Thread(target=self._work, args=(token, path, self.max_frames),
                                  name=f"waveform-{token}", daemon=True)
        self._threads = [t for t in self._threads if t.is_alive()] + [thread]
        thread.start()

    def clear(self, message: str = NO_SELECTION_TEXT) -> None:
        self._token += 1  # any in-flight load is now stale
        self.current_tx_id = None
        self.current_path = None
        self.envelope = None
        self._column_cache = None
        self.marker_visible = False
        self._set_state(STATE_EMPTY, message, terminal=False)

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

    def wait_until_loaded(self, timeout: float = 5.0) -> bool:
        """Process events until no load is pending. True if it settled."""
        timer = QtCore.QElapsedTimer()
        timer.start()
        app = QtWidgets.QApplication.instance()
        while self.state == STATE_LOADING:
            if timer.elapsed() > timeout * 1000:
                return False
            app.processEvents(QtCore.QEventLoop.AllEvents, 20)
            for t in list(self._threads):
                t.join(0.005)
        return True

    def wait_idle(self, timeout: float = 5.0) -> None:
        """Join every worker (stale ones too) and deliver their results."""
        for t in list(self._threads):
            t.join(timeout)
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

    def _work(self, token: int, path: str, max_frames: Optional[int]) -> None:
        try:
            result: object = _compute_for_widget(path, max_frames)
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            result = exc
        try:
            self._bridge.done.emit(token, result)
        except RuntimeError:
            pass  # widget destroyed meanwhile

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
        return state in ("playing", "paused")

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
