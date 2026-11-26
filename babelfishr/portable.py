"""Portable transcripts: one small JSON file per transmission, on the drive.

For a monitoring run started with a Session save path (Session.save_path),
every transmission gets a sidecar at

    <chosen>/BabelFishR/transcripts/<recording layout>/<stamp>_<tx id>.json

- the recording's own relative path and file name with ``.json`` - holding
what was received and what the operator made of it, so the drive is readable
without BabelFishR's database. It is rewritten (atomically: a temporary file,
then ``os.replace``) whenever the transmission changes: processing results,
corrections, notes, approval, retries, tags, a history clear; and removed
when the message is permanently deleted.

All of that happens on one background writer thread, never on the capture
thread or the window's: the writer only learns *which* transmission changed
(from the event bus) and reads the current row itself. A write that fails is
never reported as saved: it is logged, kept in :attr:`failures`, flagged on
the row as ``signal_metadata["portable_transcript"] = {"state": "failed",
"path", "reason", "at"}`` and announced as a ``save-path`` event with state
``"transcript-failed"``. The next successful write clears the flag and
announces ``"transcript-saved"``.

Failed transcripts are not left waiting for the next edit: whenever their
save path is seen working again - the capture's "restored" report, a
successful preflight by readiness, choosing the path or starting a watch -
and once at launch, the recorded failures for that path are queued again
(bounded, each message at most once, on the writer thread). Every path
written or removed must be safely inside the owned folder: no link
anywhere from ``<chosen>/BabelFishR`` down, real path inside its real path.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import pathlib
import threading
from typing import Any, Callable, Dict, Optional

from . import savepath
from .models import Session, Transmission, iso, utcnow

log = logging.getLogger(__name__)

FORMAT = "babelfishr.portable-transcript.v1"

#: signal_metadata key set on a transmission whose portable transcript could
#: not be written. Removed again once a write succeeds.
FAILURE_KEY = "portable_transcript"

#: At most this many recorded failures are queued again per restoration.
REQUEUE_LIMIT = 2000


def sidecar_path(chosen: str, tx: Transmission, session: Session,
                 layout: str = "{date}/{session}") -> pathlib.Path:
    """Where *tx*'s portable transcript lives under *chosen*."""
    from .pipeline import Recorder

    wav = Recorder(str(savepath.recordings_dir(chosen)), layout=layout).path_for(tx, session)
    relative = wav.relative_to(savepath.recordings_dir(chosen))
    return savepath.transcripts_dir(chosen) / relative.with_suffix(".json")


def _audio_reference(path: Optional[str], owned: pathlib.Path) -> str:
    if not path:
        return ""
    try:
        return pathlib.Path(path).relative_to(owned).as_posix()
    except ValueError:
        return str(path)


def portable_record(tx: Transmission, session: Optional[Session], chosen: str,
                    session_name: str = "") -> Dict[str, Any]:
    """The portable transcript's content for one transmission."""
    owned = savepath.owned_dir(chosen)
    metadata = tx.signal_metadata or {}
    fallback = metadata.get("save_path_fallback") or {}
    return {
        "format": FORMAT,
        "id": tx.id,
        "session": session_name,
        "conversation_id": getattr(session, "conversation_id", "") or "",
        "run_id": tx.session_id,
        "started_at": iso(tx.started_at),
        "ended_at": iso(tx.ended_at),
        "duration": round(float(tx.duration or 0.0), 3),
        # Relative to the BabelFishR folder when the recording is inside it,
        # so the drive can be read on another machine; absolute otherwise
        # (a recording that fell back to the local Recordings folder).
        "audio_file": _audio_reference(tx.audio_path, owned),
        "audio_on_this_drive": bool(tx.audio_path) and _audio_reference(
            tx.audio_path, owned) != str(tx.audio_path),
        "audio_fallback": dict(fallback) if fallback else None,
        "source_language": tx.source_language,
        "language_confidence": tx.language_confidence,
        "target_language": tx.target_language,
        "transcript": "" if tx.guess_withheld else tx.transcript,
        "translation": tx.translation,
        "transcript_correction": tx.transcript_correction,
        "translation_correction": tx.translation_correction,
        "final_transcript": "" if tx.guess_withheld else tx.display_transcript,
        "final_translation": tx.display_translation,
        "guess_withheld": tx.guess_withheld,
        "notes": tx.notes,
        "reviewed": bool(tx.reviewed),
        "approved": bool(metadata.get("operator_approved")),
        "corrected_at": iso(tx.corrected_at),
        "state": getattr(tx.state, "value", str(tx.state)),
        "error": tx.error.message if tx.error else "",
        "engines": {
            "transcription": tx.transcription_engine,
            "transcription_version": tx.transcription_engine_version,
            "translation": tx.translation_engine,
            "translation_version": tx.translation_engine_version,
        },
        "channel_name": tx.channel_name,
        "frequency_mhz": tx.frequency_mhz,
        "tags": list(tx.tags or []),
        "bookmarked": bool(tx.bookmarked),
        "hidden": bool(tx.hidden),
        "updated_at": iso(utcnow()),
    }


class PortableTranscripts:
    """The background writer for portable transcripts (see module docs)."""

    def __init__(self, store, events, layout: Callable[[], str] = lambda: "{date}/{session}"):
        self.store = store
        self.events = events
        self._layout = layout
        self._cond = threading.Condition()
        self._queue: "collections.deque" = collections.deque()
        self._queued: set = set()
        self._busy = False
        self._thread: Optional[threading.Thread] = None
        self._stopped = False
        #: Runs (session ids) that were started with a save path. Events for
        #: any other transmission are ignored without touching the store.
        self._runs: set = set()
        #: Transmission id -> {"path", "reason", "at"} for writes that failed.
        self.failures: Dict[str, Dict[str, str]] = {}
        try:
            self._runs = set(store.custom_save_runs())
        except Exception:  # noqa: BLE001 - a missing column is not fatal here
            log.debug("could not read runs with a save path", exc_info=True)
        events.subscribe(self._on_event)

    # -- intake (any thread; never blocks on I/O) --------------------------
    def track_run(self, session_id: str) -> None:
        self._runs.add(session_id)

    def _on_event(self, event) -> None:
        if event.kind == "save-path":
            payload = event.payload or {}
            if payload.get("state") == "restored" and payload.get("chosen"):
                self.requeue_failed(payload["chosen"])
            return
        if event.kind not in ("transmission", "updated"):
            return
        payload = event.payload
        tx_id = getattr(payload, "id", None)
        if tx_id and getattr(payload, "session_id", None) in self._runs:
            self.request(tx_id)

    def request(self, tx_id: str) -> None:
        """Write (or rewrite) *tx_id*'s portable transcript soon."""
        self._put(("write", tx_id))

    def requeue_failed(self, chosen: Optional[str] = None) -> None:
        """Write again, soon, every portable transcript recorded as not
        saved - for runs started with *chosen*, or all of them. Done on the
        writer thread: it reads the recorded failures (bounded by
        :data:`REQUEUE_LIMIT`), checks each save path once, and queues each
        message at most once. Unavailable paths are left for next time."""
        if not chosen and not self._runs and not self.failures:
            return                     # no run ever used a save path
        self._put(("requeue", chosen or ""))

    def remove(self, path: pathlib.Path, chosen: str) -> None:
        """Delete one portable transcript file soon (a deleted message)."""
        self._put(("remove", (str(path), chosen)))

    def _put(self, item) -> None:
        with self._cond:
            if self._stopped or item in self._queued:
                return
            self._queue.append(item)
            self._queued.add(item)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True,
                                                name="babelfishr-portable")
                self._thread.start()
            self._cond.notify_all()

    # -- state ---------------------------------------------------------------
    @property
    def idle(self) -> bool:
        with self._cond:
            return not self._queue and not self._busy

    def wait_idle(self, timeout: Optional[float] = 10.0) -> bool:
        """Block until every requested write has been attempted."""
        with self._cond:
            return self._cond.wait_for(lambda: not self._queue and not self._busy,
                                       timeout=timeout)

    def stop(self, timeout: Optional[float] = 0.0) -> bool:
        """Finish what was requested, then take nothing more. True once the
        writer has nothing left to do (it ends by itself afterwards)."""
        if not self.wait_idle(timeout=timeout):
            return False
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        return True

    # -- the writer thread -----------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                if not self._queue:
                    if not self._cond.wait_for(
                            lambda: self._queue or self._stopped, timeout=5.0):
                        self._thread = None      # idle: a new request restarts it
                        return
                    if not self._queue:
                        return                   # stopped
                item = self._queue.popleft()
                self._queued.discard(item)
                self._busy = True
            try:
                kind, value = item
                if kind == "write":
                    self._write(value)
                elif kind == "requeue":
                    self._requeue(value)
                else:
                    self._remove(*value)
            except Exception:  # noqa: BLE001 - one bad row must not stop the writer
                log.exception("portable transcript work failed for %r", item)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def _requeue(self, chosen: str) -> None:
        pending = list(self.store.failed_portable_transcripts(
            chosen or None, limit=REQUEUE_LIMIT))
        known = {tx_id for tx_id, _ in pending}
        for tx_id, failure in list(self.failures.items()):
            path = failure.get("chosen", "")
            if tx_id not in known and path and (not chosen or path == chosen):
                pending.append((tx_id, path))
        usable: Dict[str, bool] = {}
        for tx_id, path in pending[:REQUEUE_LIMIT]:
            if path not in usable:
                usable[path] = not savepath.preflight(path)
            if usable[path]:
                self.request(tx_id)

    def _write(self, tx_id: str) -> None:
        tx = self.store.get_transmission(tx_id)
        if tx is None:
            return
        session = self.store.get_session(tx.session_id)
        chosen = getattr(session, "save_path", "") if session else ""
        if not chosen:
            return
        conversation = (self.store.get_conversation(session.conversation_id)
                        if session.conversation_id else None)
        path = sidecar_path(chosen, tx, session, self._layout())
        record = portable_record(tx, session, chosen,
                                 conversation.name if conversation else "")
        try:
            owned = savepath.ensure_owned_dir(chosen)
            savepath.make_within(owned, path.parent)   # refuses links
            if not savepath.contained(owned, path):
                raise savepath.SavePathProblem(
                    savepath.subdir_is_link(savepath.TRANSCRIPTS_DIRNAME), str(path))
            savepath.write_json_atomic(
                path, json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            self._failed(tx, path, savepath.reason_for(exc), chosen)
            return
        if tx_id in self.failures or FAILURE_KEY in (tx.signal_metadata or {}):
            self.failures.pop(tx_id, None)
            self.store.set_transmission_flag(tx_id, FAILURE_KEY, None)
            self.events.publish("save-path", {
                "state": "transcript-saved", "transmission": tx_id,
                "path": str(path), "reason": "",
                "message": f"The portable transcript was saved to {path}."})

    def _failed(self, tx: Transmission, path: pathlib.Path, reason: str,
                chosen: str) -> None:
        log.error("portable transcript %s was not saved: %s", path, reason)
        failure = {"state": "failed", "path": str(path), "reason": reason,
                   "chosen": chosen, "at": iso(utcnow())}
        first = tx.id not in self.failures
        self.failures[tx.id] = failure
        try:
            self.store.set_transmission_flag(tx.id, FAILURE_KEY, failure)
        except Exception:  # noqa: BLE001 - the event below still reports it
            log.debug("could not flag %s", tx.id, exc_info=True)
        if first:
            self.events.publish("save-path", {
                "state": "transcript-failed", "transmission": tx.id,
                "path": str(path), "reason": reason,
                "message": f"The portable transcript could not be saved to "
                           f"{path} ({reason}). It is kept in BabelFishR "
                           f"and will be written again on the next change."})

    def _remove(self, path: str, chosen: str) -> None:
        target = pathlib.Path(path)
        owned = savepath.owned_dir(chosen)
        try:
            target.relative_to(savepath.transcripts_dir(chosen))
        except ValueError:
            return                     # only ever inside the transcripts area
        # No link anywhere from the owned folder down, and the real path
        # inside the owned folder's real path - as storage's ownership rules.
        if not savepath.contained(owned, target):
            log.warning("not removing %s: it is not safely inside %s", path, owned)
            return
        try:
            if target.is_symlink() or not target.is_file():
                return
            os.unlink(target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.error("could not remove portable transcript %s: %s", path, exc)
            self.events.publish("save-path", {
                "state": "transcript-remove-failed", "path": path,
                "reason": savepath.reason_for(exc),
                "message": f"The portable transcript {path} of a deleted "
                           f"message could not be removed."})
