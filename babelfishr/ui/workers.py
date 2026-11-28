"""Background workers.

Preparation downloads hundreds of megabytes, model loading takes seconds and a
transcription smoke test takes longer still.  None of that may run on the GUI
thread: a frozen window during a 500 MB download looks like a crash, and an
operator who force-quits mid-download is left with an incomplete model.

Every worker here reports progress through signals (model downloads also
emit real byte progress on ``signals.progress``) and is cancellable.
Cancellation is cooperative: a model download is abandoned between chunks,
but each file is written to a temporary name and only moved into place once
complete, so a partially downloaded model is detected as INCOMPLETE and
repaired rather than being mistaken for a good one.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any, Callable, List, Optional, Tuple

from PySide6 import QtCore

from ..download_progress import DownloadCancelled
from ..preparation import working_config

log = logging.getLogger(__name__)

__all__ = ["CancellationToken", "Cancelled", "FunctionWorker", "WorkerSignals",
           "active_worker_count", "analysis_job", "prepare_field_job",
           "readiness_job", "run_in_background", "working_config"]


class WorkerSignals(QtCore.QObject):
    """Signals a worker emits. Qt requires these on a QObject."""

    started = QtCore.Signal()
    message = QtCore.Signal(str)
    step = QtCore.Signal(str, int, int)      # label, index, total
    finished = QtCore.Signal(object)         # result payload
    failed = QtCore.Signal(str)
    cancelled = QtCore.Signal()
    #: A DownloadProgress snapshot (immutable, so safe across threads),
    #: emitted from the worker thread while a model downloads and verifies.
    progress = QtCore.Signal(object)


class CancellationToken:
    """Cooperative cancellation, checked at safe points between steps."""

    def __init__(self) -> None:
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise Cancelled()


class Cancelled(RuntimeError):
    """Raised inside a worker when the operator cancelled."""


class FunctionWorker(QtCore.QRunnable):
    """Runs a callable on a thread pool, reporting through signals.

    The callable receives ``report`` (a message callback), ``token`` (a
    :class:`CancellationToken`) and ``progress`` (a DownloadProgress
    callback that emits ``signals.progress``) as keyword arguments when it
    accepts them.
    """

    def __init__(self, function: Callable[..., Any], *args, **kwargs):
        super().__init__()
        self.signals = WorkerSignals()
        self.token = CancellationToken()
        self._function = function
        self._args = args
        self._kwargs = kwargs

    def cancel(self) -> None:
        self.token.cancel()

    def _emit(self, signal, *args) -> None:
        """Deliver a result, unless the receiver has already gone away.

        A window can be closed while a readiness check is still running. The
        C++ side of its signals object is destroyed, and emitting into it
        raises out of the pool thread - noisy, and one day fatal. The result
        is simply not wanted any more, so it is dropped.
        """
        try:
            signal.emit(*args)
        except RuntimeError as exc:  # pragma: no cover - teardown race
            log.debug("dropping a background result: %s", exc)

    def _emit_progress(self, snapshot) -> None:
        self._emit(self.signals.progress, snapshot)

    @QtCore.Slot()
    def run(self) -> None:  # pragma: no cover - exercised via the thread pool
        self._emit(self.signals.started)
        try:
            import inspect

            kwargs = dict(self._kwargs)
            parameters = inspect.signature(self._function).parameters
            if "report" in parameters:
                kwargs["report"] = self.signals.message.emit
            if "token" in parameters:
                kwargs["token"] = self.token
            if "progress" in parameters and "progress" not in kwargs:
                kwargs["progress"] = self._emit_progress
            result = self._function(*self._args, **kwargs)
        except (Cancelled, DownloadCancelled):
            self._emit(self.signals.cancelled)
            return
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the UI
            log.exception("background worker failed")
            self._emit(self.signals.failed, f"{type(exc).__name__}: {exc}")
            return
        if self.token.cancelled:
            self._emit(self.signals.cancelled)
            return
        self._emit(self.signals.finished, result)


#: Workers currently in flight. Without a strong reference the Python wrapper
#: (and with it the signals QObject) can be garbage-collected before the result
#: is delivered, so a completed job silently notifies nobody.
_ACTIVE: "set[FunctionWorker]" = set()


def run_in_background(function: Callable[..., Any], *args,
                      on_finished: Optional[Callable[[Any], None]] = None,
                      on_failed: Optional[Callable[[str], None]] = None,
                      on_message: Optional[Callable[[str], None]] = None,
                      on_cancelled: Optional[Callable[[], None]] = None,
                      on_progress: Optional[Callable[[Any], None]] = None,
                      pool: Optional[QtCore.QThreadPool] = None,
                      **kwargs) -> FunctionWorker:
    """Start *function* off the GUI thread and wire up its callbacks.

    *on_progress* receives each DownloadProgress snapshot on the GUI thread
    (a queued connection to ``signals.progress``)."""
    worker = FunctionWorker(function, *args, **kwargs)
    if on_finished is not None:
        worker.signals.finished.connect(on_finished)
    if on_failed is not None:
        worker.signals.failed.connect(on_failed)
    if on_message is not None:
        worker.signals.message.connect(on_message)
    if on_cancelled is not None:
        worker.signals.cancelled.connect(on_cancelled)
    if on_progress is not None:
        worker.signals.progress.connect(on_progress)

    _ACTIVE.add(worker)
    for signal in (worker.signals.finished, worker.signals.failed,
                   worker.signals.cancelled):
        signal.connect(lambda *_, w=worker: _ACTIVE.discard(w))

    (pool or QtCore.QThreadPool.globalInstance()).start(worker)
    return worker


def active_worker_count() -> int:
    """How many jobs are in flight. Used by tests and by shutdown."""
    return len(_ACTIVE)


# ---- the concrete jobs -------------------------------------------------
def _transfer_job(kind: str, describe: Callable[..., str]):
    """Register the whole job with the transfer owner (babelfishr.transfers)
    from its first line until it has returned or raised, with its token's
    cancel as the way to stop it. A switch to a network-restricted mode
    waits for exactly this to end. The wrapped signature is unchanged."""
    import functools
    import inspect

    def wrap(function):
        signature = inspect.signature(function)

        @functools.wraps(function)
        def run(*args, **kwargs):
            from ..transfers import registry

            bound = signature.bind_partial(*args, **kwargs)
            token = bound.arguments.get("token")
            if token is None:
                token = CancellationToken()
                kwargs["token"] = token
            with registry().begin(kind, describe(*args, **kwargs),
                                  cancel=token.cancel):
                return function(*args, **kwargs)
        return run
    return wrap


@_transfer_job("setup", lambda *a, **k: "field preparation")
def prepare_field_job(config, asr_model: str,
                      language_pairs: List[Tuple[str, str]],
                      report: Callable[[str], None] = lambda text: None,
                      token: Optional[CancellationToken] = None,
                      progress: Optional[Callable[[Any], None]] = None) -> dict:
    """Download and verify field assets. Runs entirely off the GUI thread.

    *progress* receives DownloadProgress snapshots for the speech model; the
    token's cancellation stops the download mid-file."""
    from ..preparation import prepare_field, working_config

    token = token or CancellationToken()
    token.raise_if_cancelled()

    def relay(text: str) -> None:
        token.raise_if_cancelled()
        report(text)

    # One config for both steps, so preparation and verification can never
    # disagree about which model they are talking about.
    working = working_config(config, asr_model)
    extra = {"progress": progress} if progress is not None else {}
    try:
        result = prepare_field(working, asr_model=asr_model,
                               language_pairs=language_pairs, report=relay,
                               cancelled=lambda: token.cancelled, **extra)
    except DownloadCancelled as exc:
        raise Cancelled() from exc
    token.raise_if_cancelled()

    report(f"Running Field Check on {working.asr.model!r} with downloads "
           f"disabled...")
    from ..modes import OperatingMode
    from ..readiness import field_check

    readiness = field_check(working, run_smoke_tests=True,
                            mode=OperatingMode.FIELD_OFFLINE)
    return {"preparation": result, "readiness": readiness,
            "asr_model": working.asr.model,
            "language_pairs": list(language_pairs or [])}


@_transfer_job("model", lambda config, model_name, *a, **k:
               f"the Whisper model {model_name}")
def install_model_job(config, model_name: str,
                      report: Callable[[str], None] = lambda text: None,
                      token: Optional[CancellationToken] = None,
                      progress: Optional[Callable[[Any], None]] = None):
    """One Whisper model, downloaded or repaired and verified, off the GUI
    thread. *progress* receives DownloadProgress snapshots. Cancellation is
    honoured during the transfer (mid-file) and between steps; a partial
    model is reported as incomplete."""
    from ..preparation import install_asr_model

    token = token or CancellationToken()
    token.raise_if_cancelled()

    def relay(text: str) -> None:
        token.raise_if_cancelled()
        report(text)

    extra = {"progress": progress} if progress is not None else {}
    try:
        result = install_asr_model(config, model_name, report=relay,
                                   cancelled=lambda: token.cancelled, **extra)
    except DownloadCancelled as exc:
        raise Cancelled() from exc
    token.raise_if_cancelled()
    return result


def verify_model_job(config, model_name: str,
                     report: Callable[[str], None] = lambda text: None,
                     token: Optional[CancellationToken] = None,
                     progress: Optional[Callable[[Any], None]] = None):
    """Verify checksums of one installed Whisper model, off the GUI thread.

    Deliberately NOT a registered transfer (no _transfer_job): it reads
    local files only, so a network-restricted mode neither refuses it nor
    waits for it."""
    from ..preparation import verify_asr_model

    token = token or CancellationToken()
    token.raise_if_cancelled()
    extra = {"progress": progress} if progress is not None else {}
    try:
        result = verify_asr_model(config, model_name, report=report,
                                  cancelled=lambda: token.cancelled, **extra)
    except DownloadCancelled as exc:
        raise Cancelled() from exc
    token.raise_if_cancelled()
    return result


def readiness_job(app, run_smoke_tests: bool = True,
                  report: Callable[[str], None] = lambda text: None,
                  microphone_permission: Optional[str] = None):
    """Field Check off the GUI thread; smoke tests load models and take time.
    *microphone_permission* was read on the GUI thread before starting."""
    report("Checking audio, storage and engines...")
    if microphone_permission is None:
        return app.readiness(run_smoke_tests=run_smoke_tests)
    return app.readiness(run_smoke_tests=run_smoke_tests,
                         microphone_permission=microphone_permission)
