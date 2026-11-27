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


class _ProgressBar:
    """The subset of tqdm huggingface_hub uses, forwarding real byte counts.

    Deliberately not a tqdm subclass: huggingface_hub then passes only plain
    tqdm keyword arguments and never draws anything. ``update_transfer`` is
    defined so both the plain HTTP path and the Xet path report written
    bytes through ``update()`` exactly once (network-transfer bytes, which
    can differ because of deduplication, are ignored).
    """

    _tracker: ProgressTracker = None  # type: ignore[assignment]

    def __init__(self, *args, **kwargs):
        self.total = kwargs.get("total")
        self.n = kwargs.get("initial", 0) or 0
        self.desc = kwargs.get("desc", "") or ""
        # snapshot_download makes a separate network-transfer bar; counting
        # it as well would double every byte.
        self._ignored = self.desc == "Downloading bytes"

    def update(self, n=1):
        if n is None:
            return
        self.n += n
        if not self._ignored:
            self._tracker.add_bytes(int(n))
        else:
            self._tracker.check_cancelled()

    def update_transfer(self, n=1):
        self._tracker.check_cancelled()

    def set_postfix_str(self, *args, **kwargs):
        pass

    def set_transfer_postfix_str(self, *args, **kwargs):
        pass

    def set_description(self, *args, **kwargs):
        pass

    def refresh(self, *args, **kwargs):
        pass

    def reset(self, total=None):
        self.n = 0
        self.total = total

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(())


def progress_bar_class(tracker: ProgressTracker) -> type:
    """A tqdm-compatible class bound to *tracker* (a class, not a partial:
    huggingface_hub inspects its attributes)."""
    return type("BoundProgressBar", (_ProgressBar,), {"_tracker": tracker})
