"""The one owner of every network transfer in this process.

A mode that forbids the network is only honest if nothing is still on the
network when it is shown. Before this module, switching to Offline Field Use
changed the badge at once while a model download already in flight carried on
in its worker thread - the desktop said "Offline" and was downloading.

So every model, language-pack, catalogue, repair and setup transfer registers
here from the moment it starts until its worker has *really* finished: the
function returned or raised, whether it completed, failed, was cancelled, or
stalled and only gave up later. A switch to a restricted mode then:

1. closes the gate (``restrict``), so no new transfer can begin;
2. asks every active transfer to cancel;
3. waits for the active set to drain (``wait_settled``) - and only then may
   the new mode be shown and saved. If it does not drain in a bounded time
   the switch is declined and the gate opened again.

Progress reporting is untouched; this module only knows who is running.
"""

from __future__ import annotations

import dataclasses
import functools
import itertools
import logging
import threading
import time
from typing import Callable, Dict, List, Optional

from .modes import OfflineViolation

log = logging.getLogger(__name__)

__all__ = ["Restriction", "TransferHandle", "TransferInfo", "TransferRegistry",
           "registered", "registry", "transfer_blocked_message"]


def transfer_blocked_message(reason: str, description: str) -> str:
    return (f"{description} was not started: {reason or 'a network-restricted mode was requested'}. "
            f"Nothing is downloaded while that change is pending or in force.")


@dataclasses.dataclass(frozen=True)
class TransferInfo:
    id: int
    kind: str
    description: str
    started: float
    cancel_requested: bool
    cancellable: bool


class TransferHandle:
    """One registered transfer. A context manager: leaving it - normally or
    by an exception - is what settles the transfer."""

    def __init__(self, owner: "TransferRegistry", ident: Optional[int],
                 kind: str, description: str,
                 cancel: Optional[Callable[[], None]]):
        self._owner = owner
        self.id = ident
        self.kind = kind
        self.description = description
        self._cancel = cancel
        self.cancel_requested = False
        self.started = time.monotonic()
        self._ended = False

    @property
    def nested(self) -> bool:
        """A begin() inside a transfer this thread already holds: the outer
        registration owns it, so this one adds no entry."""
        return self.id is None

    def request_cancel(self) -> None:
        self.cancel_requested = True
        if self._cancel is not None:
            try:
                self._cancel()
            except Exception:  # noqa: BLE001 - a bad callback must not stop the switch
                log.exception("cancelling %s failed", self.description)

    def end(self) -> None:
        if not self._ended:
            self._ended = True
            self._owner._end(self)

    def __enter__(self) -> "TransferHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.end()


class Restriction:
    """A held "restriction requested" gate. While any is held, begin() refuses.
    ``release()`` is idempotent; also a context manager."""

    def __init__(self, owner: "TransferRegistry", reason: str):
        self._owner = owner
        self.reason = reason
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            self._owner._release(self)

    def __enter__(self) -> "Restriction":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class TransferRegistry:
    """Thread-safe register of transfers in flight, and the gate in front."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.RLock())
        self._active: Dict[int, TransferHandle] = {}
        self._restrictions: List[Restriction] = []
        self._ids = itertools.count(1)
        self._local = threading.local()
        self._listeners: List[Callable[[], None]] = []

    # -- transfers ---------------------------------------------------------
    def begin(self, kind: str, description: str,
              cancel: Optional[Callable[[], None]] = None) -> TransferHandle:
        """Register a transfer before it touches the network.

        Raises OfflineViolation while a network-restricted mode has been
        requested (or is being entered): such a transfer must not start.
        """
        with self._cond:
            if self._restrictions:
                raise OfflineViolation(transfer_blocked_message(
                    self._restrictions[-1].reason, description))
            depth = getattr(self._local, "depth", 0)
            if depth:
                handle = TransferHandle(self, None, kind, description, cancel)
            else:
                handle = TransferHandle(self, next(self._ids), kind,
                                        description, cancel)
                self._active[handle.id] = handle
            self._local.depth = depth + 1
            return handle

    def _end(self, handle: TransferHandle) -> None:
        with self._cond:
            self._local.depth = max(0, getattr(self._local, "depth", 1) - 1)
            if handle.id is not None:
                self._active.pop(handle.id, None)
            self._cond.notify_all()
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001
                log.debug("transfer listener failed", exc_info=True)

    def active(self) -> List[TransferInfo]:
        with self._cond:
            return [TransferInfo(h.id, h.kind, h.description, h.started,
                                 h.cancel_requested, h._cancel is not None)
                    for h in self._active.values()]

    @property
    def busy(self) -> bool:
        with self._cond:
            return bool(self._active)

    def describe_active(self) -> str:
        items = self.active()
        return ", ".join(item.description for item in items)

    # -- the gate ------------------------------------------------------------
    def restrict(self, reason: str, cancel_active: bool = True) -> Restriction:
        """Close the gate and (by default) ask every active transfer to stop."""
        restriction = Restriction(self, reason)
        with self._cond:
            self._restrictions.append(restriction)
            handles = list(self._active.values()) if cancel_active else []
        for handle in handles:
            handle.request_cancel()
        return restriction

    def _release(self, restriction: Restriction) -> None:
        with self._cond:
            try:
                self._restrictions.remove(restriction)
            except ValueError:
                pass
            self._cond.notify_all()

    @property
    def restricted(self) -> bool:
        with self._cond:
            return bool(self._restrictions)

    # -- settlement ------------------------------------------------------------
    def wait_settled(self, timeout: Optional[float] = None) -> bool:
        """Block until no transfer is active, or *timeout* seconds pass.
        Returns True when settled."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._active, timeout)

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Called (from the transfer's own thread) each time one settles."""
        with self._cond:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        with self._cond:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass


_REGISTRY = TransferRegistry()


def registry() -> TransferRegistry:
    """The process-wide owner every entry point registers with."""
    return _REGISTRY


def registered(kind: str, describe: Callable[..., str]):
    """Decorator: run the function as a registered transfer (keeps the
    signature, so callers and stand-ins are unaffected)."""
    def wrap(function):
        @functools.wraps(function)
        def run(*args, **kwargs):
            try:
                description = describe(*args, **kwargs)
            except Exception:  # noqa: BLE001
                description = kind
            with registry().begin(kind, description):
                return function(*args, **kwargs)
        return run
    return wrap
