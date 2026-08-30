"""Regressions for the defects a real Mac found in alpha 3.

Every one of these is behaviour, driven through the real objects: the
classifier, the pipeline, the store, the app and - where it is the thing that
was wrong - the Qt widgets themselves. Searching the source for a string would
not have caught any of these, because in each case the source read perfectly
well and the product still did the wrong thing.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import re

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.detect import ContentClass as DetectorClass
from babelfishr.detect import DetectorSettings, detect_in_array
from babelfishr.models import (ContentClass, ProcessingState, Session,
                               SourceLanguageMode, Transmission)
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: A fixture whose bursts the classifier calls DIGITAL_SUSPECTED. On the
#: operator's Mac, ordinary speech through the built-in microphone landed in
#: exactly this class - which is the whole reason it must still be transcribed.
DIGITAL_SPEC = [
    {"gap": 1.5},
    {"kind": "digital", "duration": 2.5, "level_dbfs": -14},
    {"gap": 1.5},
]


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def digital_wav(tmp_path) -> str:
    return build_fixture(DIGITAL_SPEC, sample_rate=SR).write(
        str(tmp_path / "digital.wav"))


def _mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


# ---- A. classification must not veto speech recognition -----------------


def test_a_digital_suspected_transmission_is_saved_and_reaches_asr(
        config, store, digital_wav):
    """1. Captured, saved, and automatically transcribed - not skipped."""
    app = _mock_app(config, store)
    engine = app.transcription
    app.start_session(replay_path=digital_wav, name="digital-asr")
    app.run_replay()

    digital = [t for t in app.transmissions()
               if t.content_class is ContentClass.DIGITAL_SUSPECTED]
    assert digital, "the fixture did not produce a digital-suspected event"
    for tx in digital:
        assert pathlib.Path(tx.audio_path).exists(), "capture-first broken"
        assert tx.state is not ProcessingState.SKIPPED
        assert tx.transcript, "a digital-suspected recording got no transcript"
    assert engine.calls >= len(digital)
    app.stop_session()


def test_reverting_the_routing_restores_the_alpha_3_failure(
        config, store, digital_wav, monkeypatch):
    """2. The guard is not vacuous: put the veto back and the defect returns."""
    from babelfishr import detect as detect_module

    original = detect_module.DetectedTransmission.should_auto_transcribe

    def alpha3(self, settings):
        """Exactly the old lookup, including the digital entry."""
        if self.duration < 0.25:
            return False
        return {
            DetectorClass.SPEECH: settings.auto_process_speech,
            DetectorClass.UNKNOWN: settings.auto_process_unknown,
            DetectorClass.NOISE: settings.auto_process_noise,
            DetectorClass.TONE: settings.auto_process_tone,
            DetectorClass.DIGITAL_SUSPECTED: False,
        }.get(self.content_class or DetectorClass.UNKNOWN, True)

    monkeypatch.setattr(detect_module.DetectedTransmission,
                        "should_auto_transcribe", alpha3)
    app = _mock_app(config, store)
    app.start_session(replay_path=digital_wav, name="regression")
    app.run_replay()
    digital = [t for t in app.transmissions()
               if t.content_class is ContentClass.DIGITAL_SUSPECTED]
    assert digital
    assert all(t.state is ProcessingState.SKIPPED for t in digital), (
        "the old routing no longer reproduces the failure, so the test that "
        "checks the fix proves nothing")
    assert all(not t.transcript for t in digital)
    app.stop_session()
    monkeypatch.setattr(detect_module.DetectedTransmission,
                        "should_auto_transcribe", original)


@pytest.mark.parametrize("content,expected", [
    (DetectorClass.SPEECH, True),
    (DetectorClass.UNKNOWN, True),
    (DetectorClass.DIGITAL_SUSPECTED, True),
    (DetectorClass.NOISE, True),
    (DetectorClass.TONE, False),
])
def test_only_a_steady_tone_is_routed_away_by_default(content, expected):
    """Ordinary and digital-suspected events both reach ASR."""
    from babelfishr.detect import DetectedTransmission
    import numpy as np

    detected = DetectedTransmission(
        started_at=dt.datetime.now(dt.timezone.utc), duration=2.0,
        audio=np.zeros(SR), sample_rate=SR, start_offset=0.0, peak_dbfs=-14.0,
        rms_dbfs=-20.0, noise_floor_dbfs=-60.0, active_ratio=0.9,
        confidence=0.8, content_class=content)
    assert detected.should_auto_transcribe(DetectorSettings()) is expected


def test_no_setting_can_reinstate_the_digital_veto():
    """Including one persisted by alpha 3 in settings.toml."""
    from babelfishr.detect import DetectedTransmission
    import numpy as np

    detected = DetectedTransmission(
        started_at=dt.datetime.now(dt.timezone.utc), duration=2.0,
        audio=np.zeros(SR), sample_rate=SR, start_offset=0.0, peak_dbfs=-14.0,
        rms_dbfs=-20.0, noise_floor_dbfs=-60.0, active_ratio=0.9,
        confidence=0.8, content_class=DetectorClass.DIGITAL_SUSPECTED)
    for settings in (DetectorSettings(), DetectorSettings(auto_process_noise=False),
                     DetectorSettings(auto_process_speech=False)):
        assert detected.should_auto_transcribe(settings)

    # And an alpha 3 settings file loads without reintroducing the field.
    loaded = Config.from_dict({"detector": {"auto_process_digital": False}})
    assert not hasattr(loaded.detector, "auto_process_digital")
    assert not hasattr(loaded.detector.to_settings(), "auto_process_digital")


# ---- B. saved recordings are processable without monitoring -------------


def _one_skipped(app, wav, name):
    """Record now, process later - the operator's actual sequence.

    Captured in Record Only, so the WAV and the row exist with no transcript,
    exactly like a transmission the classifier routed away in alpha 3.
    """
    from babelfishr.modes import OperatingMode

    app.set_mode(OperatingMode.RECORD_ONLY, persist=False)
    app.select_engines()
    assert app.transcription is None
    app.start_session(replay_path=wav, name=name)
    app.run_replay()
    app.stop_session()
    app.set_mode(OperatingMode.ONLINE_SETUP, persist=False)
    saved = [t for t in app.store.recent_transmissions()
             if t.audio_path and not t.transcript]
    assert saved, "nothing was left for the operator to transcribe later"
    return saved[0]


def test_a_saved_recording_transcribes_after_stop_session(config, store,
                                                          digital_wav):
    """3. No live session, no microphone - the WAV is already on disk."""
    app = BabelFishRApp(config=config, store=store)
    target = _one_skipped(app, digital_wav, "later")
    assert app.session is None and app.pipeline is None

    assert app.processing_problem(target.id) == "", app.processing_problem(target.id)
    assert app.transcribe_anyway(target.id)
    assert app.standalone_pipeline is not None
    assert app.session is None, "a fake live session was created"
    assert app.standalone_pipeline.wait_until_idle(timeout=30.0)

    done = store.get_transmission(target.id)
    assert done.transcript, "the saved recording was never transcribed"
    app.close()


def test_a_saved_recording_transcribes_after_a_relaunch(config, store,
                                                        digital_wav):
    """4. Close the app and the store, reopen both, process the recording."""
    app = BabelFishRApp(config=config, store=store)
    target = _one_skipped(app, digital_wav, "relaunch")
    app.close()                      # closes the store too

    reopened_store = Store(config.database,
                           recordings_dir=config.recording.directory)
    relaunched = BabelFishRApp(config=config, store=reopened_store)
    assert relaunched.session is None and relaunched.pipeline is None
    assert relaunched.processing_problem(target.id) == ""
    assert relaunched.transcribe_anyway(target.id)
    assert relaunched.standalone_pipeline.wait_until_idle(timeout=30.0)
    assert reopened_store.get_transmission(target.id).transcript
    relaunched.close()


def test_translation_follows_when_the_source_language_differs(config, store,
                                                              digital_wav):
    """5. The saved transmission carries its own languages and target."""
    config.translate.target_language = "en"
    app = BabelFishRApp(config=config, store=store)
    target = _one_skipped(app, digital_wav, "translate")

    saved = store.get_transmission(target.id)
    saved.source_language = "es"
    saved.source_language_mode = SourceLanguageMode.SPECIFIED
    saved.target_language = "en"
    store.save_transmission(saved)

    assert app.transcribe_anyway(saved.id)
    assert app.standalone_pipeline.wait_until_idle(timeout=30.0)
    done = store.get_transmission(saved.id)
    assert done.transcript
    assert done.translation, "a different source language produced no translation"
    assert done.target_language == "en"
    app.close()


def test_field_offline_saved_processing_builds_no_cloud_or_mock_engine(
        config, store, digital_wav):
    """6. Offline stays offline, and a placeholder is not a transcription."""
    from babelfishr.modes import OperatingMode
    from babelfishr.providers import is_placeholder

    app = BabelFishRApp(config=config, store=store)
    target = _one_skipped(app, digital_wav, "offline")

    app.set_mode(OperatingMode.FIELD_OFFLINE, persist=False)
    app.transcribe_anyway(target.id)

    built = [e for e in (app.transcription, app.translation) if e is not None]
    for engine in built:
        assert not engine.privacy.is_cloud, f"{engine.name} sends data offsite"
        assert not is_placeholder(engine), (
            f"{engine.name} is the mock engine; Field Offline must not "
            f"silently produce placeholder text")
    if not built:
        # Nothing local is installed in this environment, which is the honest
        # outcome - and the operator must be told precisely that, not fobbed
        # off with a mock transcript.
        problem = app.processing_problem(target.id)
        assert "Field Offline" in problem or "transcription engine" in problem
    app.close()


def test_the_failure_message_never_blames_monitoring(config, store):
    """The alpha 3 dialog said this needed a running session. It does not."""
    app = BabelFishRApp(config=config, store=store)
    session = Session()
    store.save_session(session)
    tx = Transmission(session_id=session.id,
                      started_at=dt.datetime.now(dt.timezone.utc),
                      duration=2.0, audio_path="/nonexistent/gone.wav",
                      state=ProcessingState.SKIPPED)
    store.save_transmission(tx)

    problem = app.processing_problem(tx.id)
    assert problem
    assert "gone.wav" in problem, "the operator is not told which file"
    for phrase in ("running session", "start monitoring", "needs a running"):
        assert phrase not in problem.lower()
    app.close()


# ---- C. the message thread persists -------------------------------------


def test_history_selects_the_newest_rows_and_shows_them_chronologically(
        config, store):
    """9. ASC + LIMIT returns the oldest rows; that was the query defect."""
    session = Session()
    store.save_session(session)
    base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    for minute in range(12):
        store.save_transmission(Transmission(
            session_id=session.id, started_at=base + dt.timedelta(minutes=minute),
            duration=1.0, transcript=f"message {minute}",
            state=ProcessingState.COMPLETE))

    recent = store.recent_transmissions(limit=5)
    minutes = [t.started_at.minute for t in recent]
    assert minutes == [7, 8, 9, 10, 11], "not the newest five, in time order"
    # The unbounded-order query really does return the oldest, so this test
    # is measuring a difference that exists.
    assert [t.started_at.minute for t in store.list_transmissions(limit=5)] == \
        [0, 1, 2, 3, 4]
    assert store.recent_transmissions(limit=0) == []


def test_a_second_monitoring_session_keeps_the_first_sessions_bubbles(
        qt_app, config, store, digital_wav):
    """7. Starting monitoring again must append, never wipe the thread."""
    from babelfishr.ui.main_window import MainWindow

    app = _mock_app(config, store)
    app.start_session(replay_path=digital_wav, name="first")
    app.run_replay()
    app.stop_session()
    first = [t.id for t in store.recent_transmissions()]
    assert first

    window = MainWindow(app)
    qt_app.processEvents()
    assert window.timeline.count() == len(first), "the thread did not load"

    # Exactly what _start_monitoring does to the timeline, without opening a
    # device: if it clears, these bubbles disappear.
    window._start_monitoring(replay_path=digital_wav)
    qt_app.processEvents()
    assert window.timeline.count() >= len(first), (
        "starting monitoring again removed earlier transmissions from view")
    for tx_id in first:
        assert tx_id in window.timeline._bubbles
    app.stop_session()
    window.hide()


def test_reopening_the_window_restores_the_thread(qt_app, config, store,
                                                  digital_wav):
    """8. Relaunching must rebuild the thread from the database."""
    from babelfishr.ui.main_window import MainWindow

    app = _mock_app(config, store)
    app.start_session(replay_path=digital_wav, name="persisted")
    app.run_replay()
    app.stop_session()
    saved = [t.id for t in store.recent_transmissions()]
    assert saved

    first = MainWindow(app)
    qt_app.processEvents()
    assert first.timeline.count() == len(saved)
    first.hide()
    app.close()                      # quitting closes the store too

    # Relaunch: a new store, a new app and a new window over the same database.
    reopened_store = Store(config.database,
                           recordings_dir=config.recording.directory)
    relaunched = BabelFishRApp(config=config, store=reopened_store)
    relaunched.transcription = MockTranscriptionEngine()
    relaunched.translation = MockTranslationEngine()
    window = MainWindow(relaunched)
    qt_app.processEvents()

    assert window.timeline.count() == len(saved), (
        "the thread was not restored from the database after a relaunch")
    for tx_id in saved:
        assert tx_id in window.timeline._bubbles
    window.hide()
    relaunched.close()


def test_the_menu_offers_a_route_back_to_the_whole_thread(qt_app, config,
                                                          store):
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(_mock_app(config, store))
    qt_app.processEvents()
    labels = [action.text() for menu in window.menuBar().actions()
              if menu.menu() for action in menu.menu().actions()]
    assert "Show all transmissions" in labels
    assert "Show current session" not in labels
    window.hide()


# ---- D. the bubble is a text message ------------------------------------


def _bubble(**kwargs):
    from babelfishr.ui.timeline import TransmissionBubble, _Player

    defaults = dict(session_id="s", started_at=dt.datetime.now(dt.timezone.utc),
                    duration=2.0, state=ProcessingState.COMPLETE,
                    target_language="en")
    defaults.update(kwargs)
    return TransmissionBubble(Transmission(**defaults), _Player())


def test_the_default_bubble_has_no_waveform_and_no_play_button(qt_app):
    """10. Neither exists on the widget at all - not merely hidden."""
    from babelfishr.ui.widgets import WaveformWidget

    bubble = _bubble(transcript="all units, stand by")
    assert not hasattr(bubble, "waveform")
    assert not hasattr(bubble, "play_button")
    assert not bubble.findChildren(WaveformWidget)
    visible = [b.text() for b in bubble.findChildren(QtWidgetsToolButton())
               if not b.isHidden()]
    assert not any("Play" in text for text in visible), visible
    # Playback did not disappear; it moved into the ellipsis menu.
    menu_labels = [a.text() for a in bubble.menu_button.menu().actions()]
    assert "Play original recording" in menu_labels
    assert "Play decoded audio" in menu_labels
    assert "Export audio..." in menu_labels
    assert any("Analyze as digital" in text for text in menu_labels)
    assert any("Edit transcript" in text for text in menu_labels)
    assert any("tags" in text.lower() for text in menu_labels)
    assert any("note" in text.lower() for text in menu_labels)
    assert any("Bookmark" in text for text in menu_labels)


def QtWidgetsToolButton():
    from PySide6 import QtWidgets

    return QtWidgets.QToolButton


def test_the_transcript_is_the_main_text_and_translation_is_conditional(qt_app):
    """11. One bubble, transcript first, second line only when it differs."""
    foreign = _bubble(transcript="hola mundo", translation="hello world",
                      source_language="es", target_language="en")
    assert foreign.original_label.text() == "hola mundo"
    assert not foreign.original_label.isHidden()
    assert not foreign.translated_label.isHidden()
    assert "hello world" in foreign.translated_label.text()

    native = _bubble(transcript="hello world", source_language="en",
                     target_language="en")
    assert native.original_label.text() == "hello world"
    assert native.translated_label.isHidden(), (
        "an 'already in English' row appeared under an English transcript")

    same_dialect = _bubble(transcript="hello", translation="hello",
                           source_language="en-GB", target_language="en")
    assert same_dialect.translated_label.isHidden()


def test_a_bubble_being_processed_says_so_in_place(qt_app):
    working = _bubble(state=ProcessingState.TRANSCRIBING)
    assert "Transcribing" in working.original_label.text()
    assert not working.original_label.isHidden()


def test_a_digital_chip_neither_suppresses_nor_replaces_the_transcript(qt_app):
    """12. Advisory metadata stays a chip beside the words."""
    from PySide6 import QtWidgets

    bubble = _bubble(transcript="units responding",
                     content_class=ContentClass.DIGITAL_SUSPECTED,
                     source_language="en", target_language="en")
    assert bubble.original_label.text() == "units responding"
    assert not bubble.original_label.isHidden()
    chips = [c.text() for c in bubble.findChildren(QtWidgets.QLabel)
             if c.objectName() == "chip"]
    assert "possibly digital" in chips
    assert not any("possibly digital" in c.text()
                   for c in [bubble.original_label])


# ---- E. readiness ---------------------------------------------------------


def test_a_skipped_smoke_test_is_not_reported_as_record_only():
    """13. "Not tested" and "not available" are different answers."""
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    report = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local ASR model present", "Translation packages installed"):
        report.add(Check(name, CheckStatus.PASS, ""))
    for name in ("Local transcription smoke test",
                 "Local translation smoke test"):
        report.add(Check(name, CheckStatus.SKIP, "smoke tests disabled"))

    assert report.can_record
    assert not report.field_ready          # honest: it has not been proven
    assert report.field_ready_unknown, (
        "a prepared installation whose smoke tests were skipped is being "
        "reported as Record Only")
    assert report.transcription_unverified and report.translation_unverified

    # A genuinely missing model is still an unambiguous Record Only.
    missing = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable"):
        missing.add(Check(name, CheckStatus.PASS, ""))
    missing.add(Check("Local ASR model present", CheckStatus.FAIL, "no model"))
    missing.add(Check("Local transcription smoke test", CheckStatus.SKIP, ""))
    assert missing.can_record
    assert not missing.field_ready
    assert not missing.field_ready_unknown


def test_the_badge_says_checking_rather_than_record_only(qt_app, config, store):
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(_mock_app(config, store))
    qt_app.processEvents()

    report = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local ASR model present", "Translation packages installed"):
        report.add(Check(name, CheckStatus.PASS, ""))
    for name in ("Local transcription smoke test",
                 "Local translation smoke test"):
        report.add(Check(name, CheckStatus.SKIP, "smoke tests disabled"))
    window._render_readiness(report)
    assert "Record only" not in window.ready_badge.text()
    assert "Checking" in window.ready_badge.text()
    window.hide()


# ---- F. first-run setup refreshes the window ----------------------------


def test_the_automatic_first_run_setup_refreshes_the_main_window(qt_app,
                                                                 config, store,
                                                                 monkeypatch):
    """14. Both entry points go through the same refresh, no restart."""
    import babelfishr.ui as ui_package
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(_mock_app(config, store))
    qt_app.processEvents()

    calls = []
    monkeypatch.setattr(MainWindow, "refresh_after_setup",
                        lambda self: calls.append("refreshed"))

    class _FakeAssistant:
        def __init__(self, app, parent):
            pass

        def exec(self):
            return 1

    # The manually opened path.
    monkeypatch.setattr("babelfishr.ui.setup_assistant.SetupAssistant",
                        _FakeAssistant)
    window._show_assistant()
    assert calls == ["refreshed"]

    # And the automatic first-run path, taken from run() itself.
    source = pathlib.Path(ui_package.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _first_run_setup"):]
    assert "refresh_after_setup" in body.split("QtCore.QTimer")[0], (
        "the automatic first-run assistant does not refresh the window")
    window.hide()


def test_the_shared_refresh_covers_mode_engines_devices_and_readiness(
        qt_app, config, store, monkeypatch):
    """One method, and it really does re-read all of it."""
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(_mock_app(config, store))
    qt_app.processEvents()

    called = []
    for name in ("_refresh_devices", "_report_engines", "_refresh_mode_badge",
                 "_refresh_readiness", "_reload_timeline"):
        monkeypatch.setattr(MainWindow, name,
                            lambda self, *a, _n=name, **k: called.append(_n))
    window.refresh_after_setup()
    assert set(called) == {"_refresh_devices", "_report_engines",
                           "_refresh_mode_badge", "_refresh_readiness",
                           "_reload_timeline"}
    window.hide()


# ---- G. the release notes ------------------------------------------------


def test_generated_release_notes_contain_a_literal_delete(tmp_path):
    """15. Run the workflow's own heredoc and read what it wrote.

    Alpha 3's notes said "type ." because the heredoc was unquoted and the
    backticked DELETE was executed as a command. This runs the real block
    from the workflow, with the variables it expects, and fails if the word
    is missing or if the shell tried to execute anything.
    """
    import subprocess

    workflow = (ROOT / ".github" / "workflows" / "macos-release.yml").read_text(
        encoding="utf-8")
    start = workflow.index("cat > release-notes.md <<NOTES")
    end = workflow.index("\n          NOTES\n", start) + len("\n          NOTES\n")
    block = "\n".join(line[10:] if line.startswith(" " * 10) else line.strip()
                      for line in workflow[start:end].splitlines())

    script = tmp_path / "notes.sh"
    script.write_text("set -euo pipefail\n"
                      'TAG="v0.0.0-test"\n'
                      'SHA256="deadbeef"\n'
                      'GATEKEEPER="gatekeeper text"\n'
                      + block, encoding="utf-8")
    completed = subprocess.run(["bash", str(script)], cwd=tmp_path,
                               capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert "command not found" not in (completed.stderr + completed.stdout)

    notes = (tmp_path / "release-notes.md").read_text(encoding="utf-8")
    assert "`DELETE`" in notes, (
        "the literal word DELETE is missing from the generated notes")
    assert "type ." not in notes
    assert "v0.0.0-test" in notes and "deadbeef" in notes, (
        "escaping the backticks broke variable expansion")
    assert "```" in notes, "the checksum code fence was lost"
