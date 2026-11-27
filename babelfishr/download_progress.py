"""Real progress for model downloads, safe to hand to the GUI.

faster-whisper's own ``download_model`` hides every byte behind a disabled
progress bar, so a multi-gigabyte download looked exactly like a hang. The
types here carry what huggingface_hub actually reports:

* :class:`DownloadProgress` - one immutable snapshot per callback. It is a
  frozen dataclass of plain values, so it can cross from a download thread
  to the GUI thread (a queued Qt signal) without locking or copying.
* :class:`ProgressTracker` - aggregates per-file byte counts across files,
  throttles callbacks and checks for cancellation.
* :func:`progress_bar_class` - a tqdm stand-in bound to a tracker, handed to
  ``hf_hub_download``/``snapshot_download`` as ``tqdm_class``.
* :class:`DownloadCancelled` - raised when the ``cancelled`` callable says so.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

#: The phases a preparation goes through, in order.
PHASES = ("resolving", "downloading", "verifying", "done")

ProgressCallback = Callable[["DownloadProgress"], None]
CancelCheck = Callable[[], bool]

#: At most this many byte callbacks per second (file boundaries and phase
#: changes are always delivered).
DEFAULT_INTERVAL = 0.1


class DownloadCancelled(RuntimeError):
    """The operator cancelled a model download. Whatever was already written
    is left in place; the model inspects as INCOMPLETE and can be repaired."""


@dataclasses.dataclass(frozen=True)
class DownloadProgress:
    """One snapshot of a model preparation.

    ``bytes_done`` counts bytes actually received in this run.
    ``bytes_total`` is the number of bytes this run has to fetch, known only
    when the Hub's file metadata gave every file's size (files already
    present and complete are excluded from both); otherwise ``None``.
    ``files_done``/``files_total`` count the model's files, already present
    ones included; ``files_total`` is ``None`` when the file list is unknown.
    ``file`` is the file being fetched (may be long; elide it for display).
    """

    phase: str = "resolving"
    bytes_done: int = 0
    bytes_total: Optional[int] = None
    file: str = ""
    files_done: int = 0
    files_total: Optional[int] = None

    @property
    def fraction(self) -> Optional[float]:
        """0.0 .. 1.0, or ``None`` when the total is unknown."""
        if self.bytes_total is None:
            return None
        if self.bytes_total <= 0:
            return 1.0 if self.phase != "resolving" else 0.0
        return max(0.0, min(1.0, self.bytes_done / float(self.bytes_total)))


class ProgressTracker:
    """Aggregates byte updates from (possibly several) download threads."""

    def __init__(self, progress: Optional[ProgressCallback] = None,
                 cancelled: Optional[CancelCheck] = None,
                 interval: Optional[float] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._progress = progress
        self._cancelled = cancelled
        self._interval = DEFAULT_INTERVAL if interval is None else interval
        self._clock = clock
        self._lock = threading.RLock()
        self._state = DownloadProgress()
        self._last_emit = float("-inf")
        self.cancel_requested = False

    @property
    def snapshot(self) -> DownloadProgress:
        with self._lock:
            return self._state

    def check_cancelled(self) -> None:
        if self.cancel_requested or (self._cancelled is not None
                                     and self._cancelled()):
            self.cancel_requested = True
            raise DownloadCancelled("model download cancelled")

    def update(self, force: bool = True, **changes) -> None:
        """Change fields and deliver a snapshot (throttled unless *force*)."""
        with self._lock:
            self._state = dataclasses.replace(self._state, **changes)
            self._deliver(force)

    def add_bytes(self, count: int) -> None:
        with self._lock:
            done = max(0, self._state.bytes_done + int(count))
            self._state = dataclasses.replace(self._state, bytes_done=done)
            self._deliver(False)
        self.check_cancelled()

    def _deliver(self, force: bool) -> None:
        if self._progress is None:
            return
        now = self._clock()
        if not force and now - self._last_emit < self._interval:
            return
        self._last_emit = now
        self._progress(self._state)


def _tqdm_base():
    """tqdm's own class (huggingface_hub depends on tqdm), or a stand-in
    offering the same calls when it is not importable."""
    try:
        from tqdm.std import tqdm  # type: ignore

        return tqdm
    except Exception:  # noqa: BLE001
        return _TqdmStandIn


class _TqdmStandIn:
    """The tqdm calls huggingface_hub makes, as no-ops (only without tqdm)."""

    def __init__(self, iterable=None, *args, total=None, initial=0, **kwargs):
        self.iterable, self.total, self.n, self.disable = iterable, total, initial or 0, True

    def __iter__(self):
        return iter(self.iterable or ())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):          # set_*, refresh, reset, clear, display...
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: None

    @classmethod
    def get_lock(cls):
        import threading

        lock = getattr(cls, "_stand_in_lock", None)
        if lock is None:
            lock = cls._stand_in_lock = threading.RLock()
        return lock

    @classmethod
    def set_lock(cls, lock):
        cls._stand_in_lock = lock


class _ProgressBar(_tqdm_base()):
    """A real tqdm (subclass), always disabled - it never draws - so it
    honours every call huggingface_hub makes on its tqdm_class, on every
    path: per-file downloads, the snapshot_download fallback
    (set_description_str, set_postfix_str with format_dict["rate"],
    refresh, a reassigned total), hf_thread_map's file counter (as a
    context manager and with tqdm's thread lock), reset and close.

    Only counting is ours: the real bytes written, from bars measured in
    bytes, go to the tracker once. snapshot_download's separate network
    bar ("Downloading bytes") and the count of finished files (unit "it")
    are not bytes of the model and are not counted. Every update checks
    for cancellation.

    An earlier version implemented only a hand-picked subset; on the
    unknown-total path snapshot_download called set_description_str and
    the download failed with AttributeError (found on a Mac against the
    real huggingface_hub client).
    """

    _tracker: ProgressTracker = None  # type: ignore[assignment]

    def __init__(self, *args, **kwargs):
        desc = kwargs.get("desc", "") or ""
        unit = kwargs.get("unit", "it")
        kwargs.pop("name", None)              # huggingface_hub's own keyword
        kwargs["disable"] = True              # never draw anything
        super().__init__(*args, **kwargs)
        self.desc, self.unit = desc, unit
        self.n = kwargs.get("initial", 0) or 0
        self.total = kwargs.get("total")
        self._counts_bytes = unit == "B" and desc != "Downloading bytes"

    def update(self, n=1):
        n = n or 0
        self.n += n
        if self._counts_bytes and n:
            self._tracker.add_bytes(int(n))
        else:
            self._tracker.check_cancelled()

    def update_transfer(self, n=1):
        self._tracker.check_cancelled()

    def set_transfer_postfix_str(self, *args, **kwargs):
        pass

    @property
    def format_dict(self):
        # A disabled tqdm has no timing state; a rate is not measured here.
        return {"n": self.n, "total": self.total, "elapsed": 0, "rate": None,
                "unit": getattr(self, "unit", "it"), "desc": getattr(self, "desc", "")}

    def set_description(self, desc=None, refresh=True):
        self.desc = desc or ""

    def set_description_str(self, desc=None, refresh=True):
        self.desc = desc or ""

    def set_postfix(self, *args, **kwargs):
        pass

    def set_postfix_str(self, s="", refresh=True):
        pass

    def refresh(self, *args, **kwargs):
        pass

    def reset(self, total=None):
        self.n = 0
        if total is not None:
            self.total = total

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def progress_bar_class(tracker: ProgressTracker) -> type:
    """A tqdm-compatible class bound to *tracker* (a class, not a partial:
    huggingface_hub inspects its attributes)."""
    return type("BoundProgressBar", (_ProgressBar,), {"_tracker": tracker})
