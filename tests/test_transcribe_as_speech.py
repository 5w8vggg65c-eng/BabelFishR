"""One per-message way to say "this is speech": Transcribe as speech...

The operator picks Automatic or a language for this recording, in one
dialog; one job runs (no cascaded retries), the message is translated, the
raw result and earlier corrections are kept, and other recordings keep the
Session's own setting. Retry processing repeats the saved settings and is
offered only where something did not finish. Scripted engines only.
"""

from __future__ import annotations

import os
import time

import pytest

from babelfishr.models import ProcessingState
from test_live_audio_fixes import PairTranslation, ScriptedTranscription, result, run_one


class Recording(ScriptedTranscription):
    def __init__(self, scripted):
        super().__init__(scripted)
        self.asked = []

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.asked.append(language)
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)


def _wait_done(app, tx_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tx = app.store.get_transmission(tx_id)
        if tx.state in (ProcessingState.COMPLETE, ProcessingState.FAILED) and \
                (tx.signal_metadata or {}).get("speech_confirmed"):
            return tx
        time.sleep(0.02)
    return app.store.get_transmission(tx_id)


def _held(config, store, tmp_path):
    asr = Recording(result("Gracias por ver.", ratio=0.0, nsp=0.8, confidence=0.4,
                           language="es"))
    mt = PairTranslation(pairs=(("es", "en"),))
    app, tx = run_one(config, store, tmp_path, asr, mt, target_language="en")
    assert tx.guess_withheld and tx.translation == ""
    # Keep these scripted engines for the processor made after Stop.
    app.select_engines = lambda *a, **k: None
    return app, tx, asr, mt


@pytest.mark.parametrize("language", [None, "es"], ids=["automatic", "spanish"])
def test_one_action_one_job_and_the_language_is_this_recordings_alone(
        config, store, tmp_path, language):
    app, tx, asr, mt = _held(config, store, tmp_path)
    app.correct(tx.id, transcript="gracias")             # an earlier correction
    asked_before = len(asr.asked)
    assert app.transcribe_as_speech(tx.id, language)
    done = _wait_done(app, tx.id)
    app.stop_session()
    assert asr.asked[asked_before:] == [language], "not exactly one job"
    confirmed = done.signal_metadata["speech_confirmed"]
    assert confirmed["source_language"] == language
    assert confirmed["automatic"] is (language is None)
    assert done.state is ProcessingState.COMPLETE
    assert done.translation, "known speech is translated"
    assert not done.guess_withheld
    assert done.signal_metadata["superseded_corrections"][-1]["transcript"] == "gracias"
    assert done.audio_path
    assert app.config.session.source_language_mode == "automatic", \
        "the Session's own setting changed"


def test_a_skipped_recording_uses_the_same_action(config, store, tmp_path):
    app, tx, asr, mt = _held(config, store, tmp_path)
    row = store.get_transmission(tx.id)
    row.state, row.skip_reason = ProcessingState.SKIPPED, "Classified as noise."
    store.save_transmission(row)
    assert app.transcribe_as_speech(tx.id, None)
    done = _wait_done(app, tx.id)
    app.stop_session()
    assert done.state is ProcessingState.COMPLETE and done.skip_reason == ""


def test_the_old_retry_api_still_works(config, store, tmp_path):
    app, tx, asr, mt = _held(config, store, tmp_path)
    assert app.retry(tx.id, source_language="es")
    done = _wait_done(app, tx.id)
    app.stop_session()
    assert done.signal_metadata["speech_confirmed"]["source_language"] == "es"


# --- the card --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_the_card_offers_one_speech_action_and_retry_only_on_failure(qt_app, tmp_path):
    from babelfishr.models import Transmission
    from babelfishr.ui.timeline import TimelineView

    wav = tmp_path / "r.wav"
    wav.write_bytes(b"RIFF synthetic")
    view = TimelineView()
    fine = view.add(Transmission(id="fine", transcript="copy", audio_path=str(wav),
                                 state=ProcessingState.COMPLETE))
    labels = [a.text() for a in fine.menu_button.menu().actions() if a.isVisible()]
    assert labels.count("Transcribe as speech…") == 1
    for gone in ("Transcribe anyway", "Retry as speech in..."):
        assert gone not in labels
    assert "Retry processing" not in labels, "Retry offered where nothing failed"
    failed = Transmission(id="failed", transcript="", audio_path=str(wav))
    failed.fail("transcription", "engine crashed")
    bubble = view.add(failed)
    visible = [a.text() for a in bubble.menu_button.menu().actions() if a.isVisible()]
    assert "Retry processing" in visible and "Transcribe as speech…" in visible


def test_the_dialog_offers_automatic_first_in_one_step(qt_app, config, store, tmp_path,
                                                       monkeypatch):
    from PySide6 import QtWidgets

    from babelfishr.app import BabelFishRApp
    from babelfishr.models import Session, Transmission
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    store.save_session(Session(id="r", conversation_id=app.conversation_id))
    store.save_transmission(Transmission(id="m", session_id="r", transcript="x",
                                         state=ProcessingState.COMPLETE))
    window = MainWindow(app)
    asked, seen = {}, []

    def get_item(parent, title, label, items, current, editable):
        asked.update(items=list(items), current=current)
        return items[0], True

    monkeypatch.setattr(QtWidgets.QInputDialog, "getItem", staticmethod(get_item))
    monkeypatch.setattr(app, "transcribe_as_speech",
                        lambda tx_id, source_language=None:
                        seen.append((tx_id, source_language)) or True)
    try:
        window._on_retry_as_speech("m")
        assert asked["items"][0].startswith("Automatic") and asked["current"] == 0
        assert seen == [("m", None)]
    finally:
        window.close()
        app.close()
