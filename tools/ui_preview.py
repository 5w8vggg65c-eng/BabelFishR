"""UI PREVIEW - SAMPLE DATA: open the real BabelFishR window on synthetic data.

    python tools/ui_preview.py                     # interactive window
    python tools/ui_preview.py --appearance light  # daylight variant
    python tools/ui_preview.py --screenshot DIR    # write PNGs and exit

What this is: the production MainWindow, InputPanel and timeline, opened on a
throwaway database holding a handful of synthetic messages with synthetic
audio, so the layout can be looked at without a radio, a model or the
operator's data.

What it is not: nothing is captured (monitoring is never started), nothing is
transcribed or translated (the engines are switched off in this preview's
configuration, and the window reports that honestly), nothing is
downloaded. The sample transcripts and translations are typed text stored
directly, labelled "sample data" as their engine. The header shows the
operating mode the preview application is really in.

Isolation: BABELFISHR_HOME is set to a temporary directory for the whole run,
so settings, the database, recordings and logs all live there and are deleted
at exit. HOME is left alone. The operator's Application Support folder is not
read or written.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import sys
import tempfile

TITLE = "UI PREVIEW — SAMPLE DATA"
ROOT = pathlib.Path(__file__).resolve().parents[1]

#: (source language, original, translation, duration s). Typed sample text:
#: not recognised speech, not machine translation.
SAMPLES = [
    ("es", "Equipo en posición, esperamos la señal.",
     "Team in position, waiting for the signal.", 2.6),
    ("fr", "Et c'est la dictée numéro un.",
     "And that's dictation number one.", 3.0),
    ("de", "Das Team ist in Position. Funkprobe in fünf Minuten.",
     "The team is in position. Radio check in five minutes.", 4.1),
]


def _seed(store, recordings: pathlib.Path):
    """A named thread with synthetic messages and synthetic recordings."""
    from babelfishr.audio.wavefile import write_wav
    from babelfishr.models import (ContentClass, ErrorInfo, ProcessingState,
                                   Session, Transmission, utcnow)
    from babelfishr.testing import speech_like, tone

    conversation = store.create_conversation(TITLE)
    session = Session(name=TITLE, conversation_id=conversation.id,
                      audio_device="(sample data - no device)", sample_rate=48_000,
                      target_language="en")
    store.save_session(session)
    recordings.mkdir(parents=True, exist_ok=True)
    start = utcnow() - _dt.timedelta(minutes=12)

    # Older first: the tone and the failed translation, then the three
    # translated messages, so the newest (top of the thread) are the ones
    # that show original and translation together.
    minute = {len(SAMPLES): 0, len(SAMPLES) + 1: 1}
    minute.update({i: i + 2 for i in range(len(SAMPLES))})

    def message(index, **fields):
        duration = fields.pop("duration")
        audio = fields.pop("audio")
        tx = Transmission(session_id=session.id,
                          started_at=start + _dt.timedelta(minutes=2 * minute[index]),
                          duration=duration, sample_rate=48_000,
                          audio_device=session.audio_device,
                          channel_name="SAMPLE", target_language="en",
                          **fields)
        tx.ended_at = tx.started_at + _dt.timedelta(seconds=duration)
        tx.audio_path = write_wav(str(recordings / f"sample_{index}.wav"),
                                  audio(duration), 48_000)
        store.save_transmission(tx)

    for index, (language, original, translated, duration) in enumerate(SAMPLES):
        message(index, duration=duration, audio=lambda d: speech_like(d, 48_000),
                content_class=ContentClass.SPEECH, source_language=language,
                transcript=original, translation=translated,
                transcription_engine="sample data", translation_engine="sample data",
                state=ProcessingState.COMPLETE)
    message(len(SAMPLES), duration=1.2,
            audio=lambda d: tone(d, 1000.0, 48_000),
            content_class=ContentClass.TONE, auto_processed=False,
            state=ProcessingState.SKIPPED,
            skip_reason="Sample: a tone burst, recorded but not sent to speech recognition.")
    message(len(SAMPLES) + 1, duration=2.2,
            audio=lambda d: speech_like(d, 48_000),
            content_class=ContentClass.SPEECH, source_language="it",
            transcript="Ripeto, nessun movimento al ponte.",
            transcription_engine="sample data", state=ProcessingState.FAILED,
            error=ErrorInfo(stage="translation",
                            message="sample data: no it->en language pack installed"))
    return conversation


def _control_states(window):
    """The window's own control styles side by side, normal and disabled.

    A swatch, not product state: copies of the header's primary button, the
    mode button and the message Play button, styled by the same stylesheet
    and palette as the window, so their contrast can be checked in both
    states without driving the application into them.
    """
    from PySide6 import QtWidgets

    swatch = QtWidgets.QWidget()
    swatch.setWindowTitle(TITLE + " — control states")
    swatch.setPalette(window.palette())
    swatch.setStyleSheet(window.styleSheet())
    grid = QtWidgets.QGridLayout(swatch)
    grid.setContentsMargins(16, 16, 16, 16)
    grid.setHorizontalSpacing(16)
    for column, heading in enumerate(("", "normal", "disabled")):
        if heading:
            label = QtWidgets.QLabel(heading.upper())
            label.setObjectName("sectionLabel")
            grid.addWidget(label, 0, column)
    rows = (("Primary (Start monitoring)", QtWidgets.QPushButton, "primaryButton",
             "Start monitoring", None),
            ("Operating mode", QtWidgets.QToolButton, "modeButton",
             window.mode_button.text(), ("mode", window.mode_button.property("mode"))),
            ("Play recording", QtWidgets.QToolButton, "playButton", "\u25b6 Play", None),
            ("Ordinary button", QtWidgets.QPushButton, "", "Calibrate", None))
    for row, (title, kind, name, text, prop) in enumerate(rows, start=1):
        grid.addWidget(QtWidgets.QLabel(title), row, 0)
        for column, enabled in ((1, True), (2, False)):
            control = kind()
            control.setText(text)
            if name:
                control.setObjectName(name)
            if prop:
                control.setProperty(*prop)
            control.setEnabled(enabled)
            grid.addWidget(control, row, column)
    swatch.resize(760, 230)
    swatch.show()
    for _ in range(10):
        QtWidgets.QApplication.processEvents()
    return swatch


def _dark_palette():
    """A dark *system* palette, as macOS dark appearance would hand Qt."""
    from PySide6 import QtGui

    p = QtGui.QPalette()
    for role, color in ((QtGui.QPalette.Window, "#1e1e1e"),
                        (QtGui.QPalette.WindowText, "#ffffff"),
                        (QtGui.QPalette.Base, "#2a2a2a"),
                        (QtGui.QPalette.Text, "#ffffff"),
                        (QtGui.QPalette.Button, "#2a2a2a"),
                        (QtGui.QPalette.ButtonText, "#ffffff")):
        p.setColor(role, QtGui.QColor(color))
    return p


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=TITLE)
    parser.add_argument("--appearance", choices=("dark", "light"), default="dark",
                        help="simulate the system appearance (default: dark)")
    parser.add_argument("--screenshot", metavar="DIR",
                        help="write main and narrow-window PNGs to DIR and exit")
    args = parser.parse_args(argv)

    probe_root = tempfile.TemporaryDirectory(prefix="bfr-ui-preview-")
    os.environ["BABELFISHR_HOME"] = probe_root.name      # HOME is left alone
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))

    from PySide6 import QtWidgets

    from babelfishr.app import BabelFishRApp
    from babelfishr.config import Config
    from babelfishr.modes import bootstrap_environment
    from babelfishr.storage import Store
    from babelfishr.ui.main_window import MainWindow
    from babelfishr.ui.theme import apply as apply_theme

    home = pathlib.Path(probe_root.name)
    config = Config()
    config.app_home = str(home)
    config.database = str(home / "preview.sqlite3")
    config.recording.directory = str(home / "Recordings")
    config.asr.engine = "none"            # nothing is transcribed in a preview
    config.translate.engine = "none"
    bootstrap_environment(config)

    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([TITLE])
    if args.appearance == "dark":
        qt_app.setPalette(_dark_palette())
    apply_theme(qt_app)

    store = Store(config.database, recordings_dir=config.recording.directory)
    conversation = _seed(store, home / "Recordings")
    app = BabelFishRApp(config=config, store=store)
    app.select_conversation(conversation.id)
    window = MainWindow(app)
    window.setWindowTitle(TITLE)
    # The identity row says this is sample data, as the concept did.
    window.title_note.setText("UI PREVIEW / SAMPLE DATA")
    window.show()
    # Select the French sample, so the waveform shows its own recording.
    for tx_id in window.timeline.order():
        if window.timeline.bubble(tx_id).tx.source_language == "fr":
            window.timeline.select(tx_id)
            break
    window.waveform.wait_until_loaded(10.0)
    status = 0
    if args.screenshot:
        out = pathlib.Path(args.screenshot)
        out.mkdir(parents=True, exist_ok=True)
        # Let the window's own readiness check finish (bounded), so the chip
        # shows its real result rather than "Checking".
        import time
        deadline = time.monotonic() + 15
        while "Checking" in window.ready_badge.text() and time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.05)
        shots = [(f"main-{args.appearance}", (1240, 1180)),
                 (f"narrow-{args.appearance}", (620, 900))]
        for name, size in shots:
            window.resize(*size)
            for _ in range(20):
                qt_app.processEvents()
            if name.startswith("narrow"):
                # Report the width the window really took: it will not go
                # below its layout's minimum, whatever was asked for.
                name = f"{name}-{window.width()}px"
            path = out / f"ui-preview-{name}.png"
            if not window.grab().save(str(path)):
                print(f"could not write {path}", file=sys.stderr)
                status = 1
            else:
                print(f"{path}  ({window.width()}x{window.height()})")
        path = out / f"ui-preview-controls-{args.appearance}.png"
        swatch = _control_states(window)
        if not swatch.grab().save(str(path)):
            status = 1
        else:
            print(path)
        swatch.close()
    else:
        status = qt_app.exec()
    window.close()
    app.close() if hasattr(app, "close") else None
    store.close()
    probe_root.cleanup()
    return status


if __name__ == "__main__":
    sys.exit(main())
