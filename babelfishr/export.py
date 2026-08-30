"""Export sessions and transmissions in readable and machine-readable forms.

A session bundle is self-contained on purpose: audio, metadata, original
transcript and translation travel together, with relative paths, so it stays
readable after being copied to another machine.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import shutil
import zipfile
from typing import Dict, Iterable, List, Optional, Sequence

from .models import Session, Transmission, iso, utcnow
from .storage import Store

CSV_COLUMNS = [
    "id", "session_id", "started_at", "ended_at", "duration", "channel_name",
    "frequency_mhz", "profile_label", "source_language", "language_confidence",
    "transcript", "transcript_correction", "transcript_confidence",
    "target_language", "translation", "translation_correction", "state",
    "tags", "bookmarked", "reviewed", "notes", "audio_path",
    "transcription_engine", "translation_engine",
]


def to_json(transmissions: Sequence[Transmission],
            session: Optional[Session] = None, indent: int = 2) -> str:
    payload: Dict[str, object] = {
        "format": "babelfishr.transmissions.v1",
        "exported_at": iso(utcnow()),
        "transmissions": [t.to_dict() for t in transmissions],
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
        row["tags"] = ",".join(tx.tags)
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
        if tx.display_transcript:
            lines.append(f"**Original ({tx.source_language or 'unknown'}):** "
                         f"{tx.display_transcript}")
        if tx.display_translation:
            lines.append(f"**Translated ({tx.target_language}):** "
                         f"{tx.display_translation}")
        if tx.state.value == "failed" and tx.error:
            lines.append(f"**Error ({tx.error.stage}):** {tx.error.message}")
        if tx.notes:
            lines.append(f"**Notes:** {tx.notes}")
        if tx.audio_path:
            lines.append(f"**Audio:** `{pathlib.Path(tx.audio_path).name}`")
        lines.append("")
    return "\n".join(lines)


def export_session(store: Store, session_id: str, destination: str,
                   include_audio: bool = True, archive: bool = False,
                   conversation_id: Optional[str] = None) -> str:
    """Write a self-contained bundle. Returns the path written.

    ``conversation_id`` exports a whole named Session - every monitoring run
    filed under it - rather than one run. That is what the operator means by
    "this Session": they started and stopped monitoring several times and
    expect one bundle, not the last fragment of it. Chronological either way,
    because an export is a record.
    """
    session = store.get_session(session_id)
    if session is None:
        raise KeyError(f"no such session: {session_id}")
    if conversation_id:
        session_ids = store.session_ids_for_conversation(conversation_id)
        transmissions = []
        for other in session_ids:
            transmissions += store.list_transmissions(session_id=other,
                                                      limit=100_000)
        transmissions.sort(key=lambda t: (t.started_at, t.id))
    else:
        transmissions = store.list_transmissions(session_id=session_id,
                                                 limit=100_000)

    root = pathlib.Path(destination)
    root.mkdir(parents=True, exist_ok=True)
    audio_dir = root / "audio"

    copied: Dict[str, str] = {}
    if include_audio:
        audio_dir.mkdir(exist_ok=True)
        for tx in transmissions:
            if not tx.audio_path:
                continue
            source = pathlib.Path(tx.audio_path)
            if not source.exists():
                continue
            target = audio_dir / f"{tx.id}{source.suffix}"
            shutil.copy2(source, target)
            copied[tx.id] = f"audio/{target.name}"

    manifest = json.loads(to_json(transmissions, session))
    manifest["format"] = "babelfishr.session-bundle.v1"
    manifest["audio_files"] = copied
    for entry in manifest["transmissions"]:
        # Rewrite to bundle-relative paths so the export stays portable.
        entry["audio_path"] = copied.get(entry["id"])
        entry["processed_audio_path"] = None

    (root / "session.json").write_text(json.dumps(manifest, indent=2,
                                                  ensure_ascii=False),
                                       encoding="utf-8")
    (root / "transcript.md").write_text(to_markdown(transmissions, session),
                                        encoding="utf-8")
    (root / "transmissions.csv").write_text(to_csv(transmissions), encoding="utf-8")
    (root / "README.txt").write_text(_bundle_readme(session, len(transmissions)),
                                     encoding="utf-8")

    if not archive:
        return str(root)

    zip_path = root.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in sorted(root.rglob("*")):
            if item.is_file():
                zf.write(item, item.relative_to(root))
    shutil.rmtree(root)
    return str(zip_path)


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


def export_transmission_audio(tx: Transmission, destination: str) -> str:
    """Copy one clip's original audio out for sharing."""
    if not tx.audio_path:
        raise ValueError(f"transmission {tx.id} has no audio on disk")
    source = pathlib.Path(tx.audio_path)
    if not source.exists():
        raise FileNotFoundError(source)
    target = pathlib.Path(destination)
    if target.is_dir():
        target = target / f"{tx.id}{source.suffix}"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return str(target)
