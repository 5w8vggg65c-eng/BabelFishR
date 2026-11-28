"""Export sessions and transmissions in readable and machine-readable forms.

A session bundle is self-contained on purpose: audio, metadata, original
transcript and translation travel together, with relative paths, so it stays
readable after being copied to another machine.
"""

from __future__ import annotations

import csv
import dataclasses
import errno
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
import zipfile
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .models import Session, Transmission, iso, utcnow
from .storage import Store

CSV_COLUMNS = [
    "id", "session_id", "started_at", "ended_at", "duration", "channel_name",
    "frequency_mhz", "profile_label", "source_language", "language_confidence",
    "transcript", "transcript_correction", "transcript_confidence",
    "target_language", "translation", "translation_correction", "state",
    "tags", "bookmarked", "reviewed", "notes", "audio_path",
    "transcription_engine", "translation_engine",
    # Appended, so existing columns keep their positions.
    "needs_review", "translation_status",
    # A recogniser's guess for noise is not put in the transcript column.
    "transcript_status", "withheld_guess",
]


class ExportCancelled(Exception):
    """Raised when an export's ``cancelled`` hook asks it to stop.

    Nothing is left behind: the operation's private staging is removed and
    nothing appears at the requested name.
    """


#: Shown in place of an operator folder (a removable volume, a home folder)
#: wherever one would otherwise travel inside a portable bundle.
LOCAL_FOLDER = "[local folder]"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(text: str, fallback: str) -> str:
    """One harmless filename component: no separators, no leading dots,
    nothing a filesystem treats specially."""
    name = _SAFE_NAME.sub("_", str(text or "")).strip("._ ")
    return name[:120] or fallback


def _plan_audio(transmissions: Sequence[Transmission], include_audio: bool
                ) -> List[Tuple[str, pathlib.Path, str]]:
    """(tx id, source, bundle-relative path) for each recording to copy.

    Bundle names are derived from the id but made safe and collision-free
    (case-insensitively, for macOS and Windows volumes), so an odd id can
    never escape ``audio/`` and two recordings never share a file.
    """
    if not include_audio:
        return []
    plan: List[Tuple[str, pathlib.Path, str]] = []
    used: set = set()
    seen_ids: set = set()
    for tx in transmissions:
        if not tx.audio_path or tx.id in seen_ids:
            continue
        source = pathlib.Path(tx.audio_path)
        if not source.is_file():
            continue
        seen_ids.add(tx.id)
        stem = _safe_component(tx.id, "clip")
        suffix = ("." + _safe_component(source.suffix[1:], "audio")
                  if source.suffix else "")
        name, n = f"{stem}{suffix}", 1
        while name.lower() in used:
            n += 1
            name = f"{stem}-{n}{suffix}"
        used.add(name.lower())
        plan.append((tx.id, source, f"audio/{name}"))
    return plan


def _operator_folders(store: Store, sessions: Iterable[Optional[Session]],
                      conversation_id: Optional[str],
                      transmissions: Sequence[Transmission]) -> List[str]:
    """Absolute local folders that must not travel inside a bundle: the
    Session's save paths, the recordings folder, where each recording sits,
    and the operator's home folder. Longest first, so the most specific
    folder is the one replaced."""
    roots: set = set()

    def add(value) -> None:
        text = str(value or "").strip()
        if not text:
            return
        path = pathlib.Path(text)
        if not path.is_absolute():
            return
        text = str(path)
        if len(text.rstrip("/\\")) >= 4:
            roots.add(text.rstrip("/\\"))

    for session in sessions:
        if session is not None:
            add(session.save_path)
            if not conversation_id:
                conversation_id = session.conversation_id
    if conversation_id:
        try:
            conversation = store.get_conversation(conversation_id)
        except Exception:  # noqa: BLE001 - redaction must not stop an export
            conversation = None
        if conversation is not None:
            add(getattr(conversation, "save_path", ""))
    add(getattr(store, "recordings_dir", ""))
    for tx in transmissions:
        for value in (tx.audio_path, tx.processed_audio_path):
            if value:
                add(pathlib.Path(value).parent)
    try:
        add(pathlib.Path.home())
    except Exception:  # noqa: BLE001 - no home folder: nothing to hide
        pass
    return sorted(roots, key=len, reverse=True)


def _redactor(roots: Sequence[str]):
    if not roots:
        return lambda text: text
    pattern = re.compile(
        "(?:" + "|".join(re.escape(r) for r in roots) + r")(?![A-Za-z0-9_.-])")
    return lambda text: pattern.sub(LOCAL_FOLDER, text)


def _redact_tree(value, redact):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_redact_tree(v, redact) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v, redact) for k, v in value.items()}
    return value


def _unique_sibling(path: pathlib.Path, attempt: int,
                    keep_suffix: bool) -> pathlib.Path:
    """``name``, then ``name (2)``, ``name (3)``... keeping a file's suffix
    (``clip (2).wav``); a folder's name is numbered whole."""
    if attempt <= 1:
        return path
    if keep_suffix and path.suffix:
        return path.with_name(f"{path.stem} ({attempt}){path.suffix}")
    return path.with_name(f"{path.name} ({attempt})")


def _publish(staged: pathlib.Path, final: pathlib.Path) -> pathlib.Path:
    """Move *staged* to *final*, or to the first free ``final (n)``.

    Never replaces anything: a file is published with a hard link (which
    fails if the name is taken) where the volume supports it; a folder with
    a rename after checking the name is free. Returns the name used.
    """
    for attempt in range(1, 10_000):
        candidate = _unique_sibling(final, attempt, not staged.is_dir())
        if os.path.lexists(candidate):
            continue
        if staged.is_dir():
            try:
                os.rename(staged, candidate)
            except FileExistsError:
                continue
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) \
                        and os.path.lexists(candidate):
                    continue
                raise
            return candidate
        try:
            os.link(staged, candidate)
        except FileExistsError:
            continue
        except OSError:
            # No hard links here (FAT/exFAT, some network shares): the
            # name was free a moment ago; a plain rename is the fallback.
            if os.path.lexists(candidate):
                continue
            os.rename(staged, candidate)
            return candidate
        try:
            os.unlink(staged)
        except OSError:
            pass  # removed with the staging folder
        return candidate
    raise FileExistsError(f"no free name next to {final}")


def _staging_dir(parent: pathlib.Path) -> pathlib.Path:
    """A uniquely named, hidden folder owned by this export alone, next to
    the final location (same volume, so publishing is a rename)."""
    parent.mkdir(parents=True, exist_ok=True)
    return pathlib.Path(tempfile.mkdtemp(prefix=".babelfishr-export-",
                                         suffix=".partial", dir=parent))


def to_json(transmissions: Sequence[Transmission],
            session: Optional[Session] = None, indent: int = 2) -> str:
    payload: Dict[str, object] = {
        "format": "babelfishr.transmissions.v1",
        "exported_at": iso(utcnow()),
        # Machine-readable: every stored value as it is, plus whether the
        # transcript is a withheld guess, so no reader takes it for traffic.
        "transmissions": [dict(t.to_dict(), guess_withheld=t.guess_withheld,
                               withheld_ending=t.withheld_ending,
                               static=t.is_static)
                          for t in transmissions],
    }
    if session is not None:
        payload["session"] = session.to_dict()
    return json.dumps(payload, indent=indent, ensure_ascii=False)


def to_csv(transmissions: Sequence[Transmission]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for tx in transmissions:
        row = tx.to_dict()
        unresolved = (tx.signal_metadata or {}).get("translation_unresolved")
        row["translation_status"] = ("not attempted: language unresolved"
                                     if unresolved else "")
        row["tags"] = ",".join(tx.tags)
        if tx.withheld_ending:
            row["transcript"] = tx.shown_transcript
            row["translation"] = tx.shown_translation
            row["withheld_guess"] = tx.withheld_ending
            row["transcript_status"] = "unsupported sign-off withheld from the end"
        if tx.is_static:
            row["transcript_status"] = "static"
        if tx.guess_withheld:
            row["withheld_guess"] = tx.transcript
            row["transcript"] = ""
            row["transcript_status"] = ("static: engine guess withheld" if tx.is_static
                                        else "audio unintelligible: engine guess withheld")
            if not tx.translation_correction:
                row["translation"] = ""
        writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    return buffer.getvalue()


def to_markdown(transmissions: Sequence[Transmission],
                session: Optional[Session] = None) -> str:
    """A log a human can read - the format most operators actually want."""
    lines: List[str] = []
    if session is not None:
        lines += [
            f"# BabelFishR session {session.name or session.id}",
            "",
            f"- **Started:** {iso(session.started_at)}",
            f"- **Ended:** {iso(session.ended_at) or 'open'}",
            f"- **Input device:** {session.audio_device or 'unknown'}",
            f"- **Radio profile:** {session.profile_label or 'none'}",
            f"- **Target language:** {session.target_language}",
            f"- **Transcription:** {session.transcription_engine or 'none'}",
            f"- **Translation:** {session.translation_engine or 'none'}",
            "",
            "> Frequency and channel are operator-supplied metadata, not measured "
            "by BabelFishR.",
            "",
            "---",
            "",
        ]
    lines.append(f"## {len(transmissions)} received transmission(s)")
    lines.append("")
    for tx in transmissions:
        header = f"### {iso(tx.started_at)}  ({tx.duration:.1f} s)"
        lines.append(header)
        meta: List[str] = []
        if tx.channel_name:
            meta.append(tx.channel_name)
        if tx.frequency_mhz is not None:
            meta.append(f"{tx.frequency_mhz:.4f} MHz")
        if tx.source_language:
            confidence = (f" ({tx.language_confidence:.0%})"
                          if tx.language_confidence is not None else "")
            meta.append(f"language: {tx.source_language}{confidence}")
        if tx.tags:
            meta.append("tags: " + ", ".join(tx.tags))
        if tx.bookmarked:
            meta.append("bookmarked")
        if meta:
            lines.append("*" + " | ".join(meta) + "*")
        lines.append("")
        if tx.guess_withheld or tx.is_static:
            # Not traffic, and the guess is not repeated here: the raw engine
            # text is in Edit and in the JSON (forensic) export.
            lines.append(f"**Original:** *{tx.presented_transcript}*")
        elif tx.shown_transcript:
            lines.append(f"**Original ({tx.source_language or 'unknown'}):** "
                         f"{tx.shown_transcript}")
        if tx.withheld_ending:
            lines.append("*A stock sign-off the recording does not support was "
                         "withheld from the end.*")
        if tx.shown_translation:
            lines.append(f"**Translated ({tx.target_language}):** "
                         f"{tx.shown_translation}")
        if tx.state.value == "failed" and tx.error:
            lines.append(f"**Error ({tx.error.stage}):** {tx.error.message}")
        unresolved = (tx.signal_metadata or {}).get("translation_unresolved") or {}
        if unresolved:
            # A COMPLETE row with no translation: say why, as the Error line
            # did when this case was recorded as a failure.
            lines.append("**Translation not attempted (language unresolved):** "
                         + (unresolved.get("message") or
                            f"automatic detection guessed "
                            f"'{unresolved.get('guessed_language') or '?'}' and no "
                            f"language pack is installed for it."))
        if tx.needs_review:
            lines.append("**Review:** needs review")
        if tx.notes:
            lines.append(f"**Notes:** {tx.notes}")
        if tx.audio_path:
            # A bundle names its own relative file (audio/<id>.wav); a loose
            # export names the recording without the folder it sits in.
            audio = pathlib.PurePath(tx.audio_path)
            shown = audio.name if audio.is_absolute() else audio.as_posix()
            lines.append(f"**Audio:** `{shown}`")
        lines.append("")
    return "\n".join(lines)


def export_session(store: Store, session_id: str, destination: str,
                   include_audio: bool = True, archive: bool = False,
                   conversation_id: Optional[str] = None,
                   cancelled: Optional[Callable[[], bool]] = None) -> str:
    """Write a self-contained bundle. Returns the path actually written.

    ``conversation_id`` exports a whole named Session - every monitoring run
    filed under it - rather than one run. That is what the operator means by
    "this Session": they started and stopped monitoring several times and
    expect one bundle, not the last fragment of it. Chronological either way,
    because an export is a record.

    The bundle is built in a private staging folder next to *destination*
    and published by renaming it into place, so a folder or ZIP appears
    complete or not at all. Nothing that already exists is packaged,
    changed or removed: when *destination* (or ``<destination>.zip``) is
    taken, the bundle is published as ``<name> (2)``, ``<name> (3)``... and
    that is the path returned. On any failure, or when ``cancelled()``
    returns true (raising :class:`ExportCancelled`), the staging folder is
    removed and nothing appears.
    """
    session = store.get_session(session_id)
    if session is None:
        raise KeyError(f"no such session: {session_id}")
    sessions: List[Optional[Session]] = [session]
    if conversation_id:
        session_ids = store.session_ids_for_conversation(conversation_id)
        transmissions = []
        for other in session_ids:
            transmissions += store.list_transmissions(session_id=other,
                                                      limit=100_000)
            if other != session_id:
                sessions.append(store.get_session(other))
        transmissions.sort(key=lambda t: (t.started_at, t.id))
    else:
        transmissions = store.list_transmissions(session_id=session_id,
                                                 limit=100_000)

    requested = pathlib.Path(destination)
    if archive:
        final = (requested if requested.suffix.lower() == ".zip"
                 else requested.with_name(requested.name + ".zip"))
    else:
        final = requested
    final = final.absolute()

    def check() -> None:
        if cancelled is not None and cancelled():
            raise ExportCancelled("export cancelled")

    check()
    plan = _plan_audio(transmissions, include_audio)
    redact = _redactor(_operator_folders(store, sessions, conversation_id,
                                         transmissions))
    staging = _staging_dir(final.parent)
    try:
        root = staging / "bundle"
        root.mkdir()
        written: List[pathlib.Path] = []
        copied: Dict[str, str] = {}
        if include_audio:
            (root / "audio").mkdir()
        for tx_id, source, relative in plan:
            check()
            target = root / relative
            shutil.copy2(source, target)
            written.append(target)
            copied[tx_id] = relative
        check()

        # One mapping for every file: included audio has its bundle path,
        # excluded or missing audio has none.
        portable = [dataclasses.replace(tx, audio_path=copied.get(tx.id) or "",
                                        processed_audio_path="")
                    for tx in transmissions]
        manifest = json.loads(to_json(transmissions, session))
        manifest["format"] = "babelfishr.session-bundle.v1"
        manifest["audio_files"] = copied
        for entry in manifest["transmissions"]:
            # Rewrite to bundle-relative paths so the export stays portable.
            entry["audio_path"] = copied.get(entry["id"])
            entry["processed_audio_path"] = None
        if isinstance(manifest.get("session"), dict):
            # Where this machine saved the run is not part of the record.
            manifest["session"]["save_path"] = ""
        manifest = _redact_tree(manifest, redact)

        texts = {
            "session.json": json.dumps(manifest, indent=2, ensure_ascii=False),
            "transcript.md": redact(to_markdown(portable, session)),
            "transmissions.csv": redact(to_csv(portable)),
            "README.txt": redact(_bundle_readme(session, len(transmissions))),
        }
        for name, text in texts.items():
            target = root / name
            target.write_text(text, encoding="utf-8")
            written.append(target)
        check()

        if archive:
            staged = staging / final.name
            with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as zf:
                for item in sorted(written):
                    check()
                    zf.write(item, item.relative_to(root).as_posix())
        else:
            staged = root
        check()
        published = _publish(staged, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return str(published)


def _bundle_readme(session: Session, count: int) -> str:
    return (
        "BabelFishR session bundle\n"
        "=========================\n\n"
        f"Session:      {session.name or session.id}\n"
        f"Started:      {iso(session.started_at)}\n"
        f"Transmissions:{count}\n\n"
        "Contents\n"
        "  session.json      full metadata, transcripts and translations\n"
        "  transcript.md     human-readable log\n"
        "  transmissions.csv spreadsheet-friendly table\n"
        "  audio/            original recordings, named by transmission id\n\n"
        "Original transcripts and translations are stored in separate fields;\n"
        "the audio here is the unmodified capture.\n\n"
        "Channel and frequency values were supplied by the operator (via a radio\n"
        "profile). BabelFishR receives audio only and does not measure them.\n"
    )


def export_transmission_audio(tx: Transmission, destination: str,
                              overwrite: bool = False) -> str:
    """Copy one clip's original audio out for sharing. Returns the path
    actually written.

    The copy is made beside the target under a private temporary name and
    then moved into place, so a failed copy never leaves a partial file at
    the target. An existing file is replaced only with ``overwrite=True``
    (the operator confirmed that file in a save dialog); otherwise the clip
    is written as ``name (2).wav`` and so on. A folder destination gets a
    file named by the transmission id.
    """
    if not tx.audio_path:
        raise ValueError(f"transmission {tx.id} has no audio on disk")
    source = pathlib.Path(tx.audio_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target = pathlib.Path(destination)
    if target.is_dir():
        suffix = ("." + _safe_component(source.suffix[1:], "audio")
                  if source.suffix else "")
        target = target / f"{_safe_component(tx.id, 'clip')}{suffix}"
        overwrite = False
    target = target.absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".babelfishr-clip-",
                                         suffix=".partial", dir=target.parent)
    os.close(handle)
    temp = pathlib.Path(temp_name)
    try:
        shutil.copy2(source, temp)
        if overwrite:
            os.replace(temp, target)
            return str(target)
        return str(_publish(temp, target))
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
