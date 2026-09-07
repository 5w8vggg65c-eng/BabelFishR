"""Repairs from the first live-Mac bench test of the alpha 4 candidate.

Five things the operator saw or asked for, each driven here through the
control they would use - the mode menu, the Rename button and the tab bar,
the readiness chip, a bubble's header, the state badge fed by the real event
queue - rather than through the helper underneath. Where a Mac interaction
could not be made to fail here, the test says what it does prove.
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import time

import pytest

from babelfishr.app import BabelFishRApp, EngineSummary
from babelfishr.models import ProcessingState, Transmission
from babelfishr.pipeline import PipelineState
from babelfishr.providers.mock import (MockTranscriptionEngine,
                                       MockTranslationEngine)
from babelfishr.readiness import Check, CheckStatus, ReadinessReport
from babelfishr.storage import Store
from babelfishr.testing import build_fixture

SR = 48_000


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def wav(tmp_path) -> str:
    return build_fixture([{"gap": 1.0},
                          {"kind": "voice", "duration": 2.0, "level_dbfs": -14},
                          {"gap": 1.0}], sample_rate=SR).write(
        str(tmp_path / "voice.wav"))


def mock_app(config, store) -> BabelFishRApp:
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    return app


def pump(qt_app, rounds: int = 30, pause: float = 0.005) -> None:
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(pause)


def pump_until(qt_app, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def settle_readiness(qt_app, window) -> None:
    """Let the startup readiness check finish so it cannot race a test."""
    assert pump_until(qt_app, lambda: window._readiness is not None, 30.0), (
        "the startup readiness check never returned")


def clean_summary() -> EngineSummary:
    summary = EngineSummary()
    summary.transcription = "real-asr"
    summary.translation = "real-mt"
    return summary


# ---- 1. the warning banner follows the current selection ----------------


def test_leaving_record_only_removes_its_explanation(qt_app, config, store,
                                                     monkeypatch):
    """The reproduction: Record Only, then a processing mode, through the
    same _apply_mode the mode menu calls."""
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    pump(qt_app)

    assert window._apply_mode("record-only")
    assert "Record Only" in window.warning_banner.text(), (
        "a genuine Record Only selection must still be explained")
    assert window.warning_banner.isVisibleTo(window)

    assert window._apply_mode("field-offline")
    assert "Record Only" not in window.warning_banner.text(), (
        "the Record Only explanation outlived the Record Only selection")
    window.close()


def test_a_summary_with_no_warnings_clears_the_engine_warning(qt_app, config,
                                                              store,
                                                              monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    # Make sure nothing but the engines is on the banner first.
    window._clear_warning("audio-backend")
    window._clear_warning("audio-input")

    assert window._apply_mode("record-only")
    assert "Record Only" in window.warning_banner.text()

    monkeypatch.setattr(app, "select_engines",
                        lambda strict=False: clean_summary())
    window._report_engines()
    assert window.warning_banner.text() == ""
    assert not window.warning_banner.isVisibleTo(window), (
        "nothing is wrong, and the banner still shows")
    window.close()


def test_clearing_the_engine_warning_keeps_a_valid_audio_warning(qt_app,
                                                                 config,
                                                                 store,
                                                                 monkeypatch):
    """The banner also carries audio-input trouble. Fixing the stale engine
    text by hiding the whole banner would have hidden that too."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    window._clear_warning("audio-backend")

    assert window._apply_mode("record-only")
    # A device drops out, through the real event path.
    app.events.publish("audio-status", {"kind": "disconnected",
                                        "message": "gone"})
    window._drain_events()
    banner = window.warning_banner.text()
    assert "Record Only" in banner and "stopped responding" in banner

    # Leave Record Only for a mode with healthy engines.
    monkeypatch.setattr(app, "select_engines",
                        lambda strict=False: clean_summary())
    window._report_engines()
    banner = window.warning_banner.text()
    assert "Record Only" not in banner, "the obsolete warning stayed"
    assert "stopped responding" in banner, "a still-valid warning was hidden"
    assert window.warning_banner.isVisibleTo(window)

    # And the device coming back retracts only its own warning.
    app.events.publish("audio-status", {"kind": "reconnected",
                                        "message": "back"})
    window._drain_events()
    assert "stopped responding" not in window.warning_banner.text()
    window.close()


def test_a_recovered_device_does_not_hide_an_engine_warning(qt_app, config,
                                                            store):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    window._clear_warning("audio-backend")
    assert window._apply_mode("record-only")
    app.events.publish("audio-status", {"kind": "disconnected",
                                        "message": "gone"})
    app.events.publish("audio-status", {"kind": "connected",
                                        "message": "back"})
    window._drain_events()
    assert "Record Only" in window.warning_banner.text()
    assert "stopped responding" not in window.warning_banner.text()
    window.close()


def test_offline_enforcement_still_warns_in_field_offline(qt_app, config,
                                                          store):
    """Preserved: FIELD OFFLINE with only placeholder engines is not quietly
    accepted. The warning changes to the right one; it does not vanish."""
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    pump(qt_app)
    assert window._apply_mode("record-only")
    assert window._apply_mode("field-offline")
    banner = window.warning_banner.text()
    assert "Record Only" not in banner
    assert "unavailable" in banner or "MOCK" in banner, (
        "Field Offline on placeholder engines produced no warning at all")
    window.close()


# ---- 2. renaming a Session through the actual controls ------------------


def _answer_dialog(monkeypatch, text, accepted=True, calls=None):
    from PySide6 import QtWidgets

    def fake(*args, **kwargs):
        if calls is not None:
            calls.append((args, kwargs))
        return text, accepted

    monkeypatch.setattr(QtWidgets.QInputDialog, "getText", staticmethod(fake))


def _tab_index_for(window, conversation_id) -> int:
    for index in range(window.session_tabs.count()):
        if window.session_tabs.tabData(index) == conversation_id:
            return index
    raise AssertionError(f"no tab for {conversation_id}")


def test_the_rename_button_renames_the_selected_session(qt_app, config, store,
                                                        monkeypatch):
    """A real mouse click on the real button, then the dialog, then the
    tab, then the database - reopened, to prove it was saved."""
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("Alpha")
    app.select_conversation(created.id)
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    index = _tab_index_for(window, created.id)
    window.session_tabs.setCurrentIndex(index)
    pump(qt_app)

    calls = []
    _answer_dialog(monkeypatch, "Bravo Net", True, calls)
    QTest.mouseClick(window.rename_session_button, QtCore.Qt.LeftButton)
    pump(qt_app)

    assert calls, "clicking Rename did not open the name dialog"
    assert calls[0][0][4] == "Alpha", "the dialog did not start from the current name"
    index = _tab_index_for(window, created.id)
    assert window.session_tabs.tabText(index) == "Bravo Net"
    assert window.session_tabs.tabData(index) == created.id, (
        "renaming changed which Session the tab points at")
    assert "Renamed" in window.status.currentMessage()

    reopened = Store(config.database, recordings_dir=config.recording.directory)
    assert reopened.get_conversation(created.id).name == "Bravo Net"
    reopened.close()
    window.close()


def test_double_clicking_a_tab_renames_it(qt_app, config, store, monkeypatch):
    from PySide6 import QtCore
    from PySide6.QtTest import QTest

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("Charlie")
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    index = _tab_index_for(window, created.id)

    _answer_dialog(monkeypatch, "Delta", True)
    QTest.mouseDClick(window.session_tabs, QtCore.Qt.LeftButton,
                      QtCore.Qt.NoModifier,
                      window.session_tabs.tabRect(index).center())
    pump(qt_app)

    index = _tab_index_for(window, created.id)
    assert window.session_tabs.tabText(index) == "Delta"
    assert store.get_conversation(created.id).name == "Delta"
    window.close()


def test_the_tab_context_menu_offers_rename(qt_app, config, store,
                                            monkeypatch):
    """A third way in, on the tab itself. The menu is exercised by
    triggering its action rather than by a blocking exec."""
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("Echo")
    window = MainWindow(app)
    window.show()
    pump(qt_app)
    index = _tab_index_for(window, created.id)

    # The click position resolves to this tab, and the menu built for it
    # offers Rename. (QMenu.exec itself blocks for a user, so the menu is
    # driven by triggering its action, as a click on the item would.)
    position = window.session_tabs.tabRect(index).center()
    assert window.session_tabs.tabAt(position) == index
    menu = window._build_session_tab_menu(index)
    assert isinstance(menu, QtWidgets.QMenu)
    labels = [a.text() for a in menu.actions()]
    rename = [a for a in menu.actions() if a.text().startswith("Rename")]
    assert rename, labels
    _answer_dialog(monkeypatch, "Foxtrot", True)
    rename[0].trigger()
    pump(qt_app)

    assert store.get_conversation(created.id).name == "Foxtrot"
    assert window.session_tabs.tabText(_tab_index_for(window, created.id)) == "Foxtrot"
    window.close()


def test_cancel_and_blank_leave_the_name_unchanged(qt_app, config, store,
                                                   monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("Golf")
    window = MainWindow(app)
    pump(qt_app)
    index = _tab_index_for(window, created.id)

    _answer_dialog(monkeypatch, "Hotel", False)          # cancelled
    window._rename_session_tab(index)
    assert store.get_conversation(created.id).name == "Golf"
    assert "unchanged" in window.status.currentMessage()

    _answer_dialog(monkeypatch, "   ", True)             # blank
    window._rename_session_tab(index)
    assert store.get_conversation(created.id).name == "Golf"
    assert window.session_tabs.tabText(_tab_index_for(window, created.id)) == "Golf"
    assert "unchanged" in window.status.currentMessage()
    window.close()


def test_a_failed_save_is_reported_not_presented_as_success(qt_app, config,
                                                            store,
                                                            monkeypatch):
    from PySide6 import QtWidgets

    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("India")
    window = MainWindow(app)
    pump(qt_app)
    index = _tab_index_for(window, created.id)

    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: warnings.append(a)))

    def refuse(conversation_id, name):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app, "rename_conversation", refuse)
    _answer_dialog(monkeypatch, "Juliet", True)
    window._rename_session_tab(index)
    assert warnings, "the failed rename produced no visible outcome"
    assert "not renamed" in warnings[0][2]
    assert store.get_conversation(created.id).name == "India"
    assert window.session_tabs.tabText(_tab_index_for(window, created.id)) == "India"
    window.close()


def test_renaming_keeps_identity_messages_and_the_pinned_capture(
        qt_app, config, store, wav, monkeypatch):
    """The Session being recorded into is renamed mid-run. Same id, same
    destination, same messages; only the label changes."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    created = app.create_conversation("Kilo")
    app.select_conversation(created.id)
    window = MainWindow(app)
    pump(qt_app)

    app.start_session(replay_path=wav, name="run-1")
    assert app.capture_conversation_id == created.id
    app.run_replay()
    before = {t.id for t in app.recent_transmissions()}
    assert before, "nothing was captured, so ownership cannot be checked"

    # Look at General while the run is pinned to Kilo, so the label speaks.
    general = store.default_conversation()
    app.select_conversation(general.id)
    window._refresh_session_tabs()
    assert "Kilo" in window.capture_tab_label.text()

    _answer_dialog(monkeypatch, "Lima", True)
    window._rename_session_tab(_tab_index_for(window, created.id))

    assert app.capture_conversation_id == created.id, "the pin moved"
    assert "Lima" in window.capture_tab_label.text(), (
        "the 'Recording into' notice still shows the old name")
    assert store.get_conversation(created.id).name == "Lima"
    assert set(store.session_ids_for_conversation(created.id)) == {
        app.session.id}
    after = {t.id for t in store.conversation_transmissions(created.id,
                                                             limit=1000)}
    assert before <= after, "renaming lost messages"
    app.stop_session()
    window.close()


def test_the_default_general_session_can_be_renamed_without_duplication(
        qt_app, config, store, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    general = store.default_conversation()
    _answer_dialog(monkeypatch, "Base", True)
    window._rename_session_tab(_tab_index_for(window, general.id))
    names = [c.name for c in store.list_conversations()]
    assert names.count("Base") == 1
    assert "General" not in names, "renaming General created a second General"
    assert store.default_conversation().id == general.id
    window.close()


# ---- 3. "Ready", and only when ready ------------------------------------


def _ready_report() -> ReadinessReport:
    report = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local transcription smoke test",
                 "Local translation smoke test"):
        report.add(Check(name, CheckStatus.PASS, ""))
    return report


def _partly_ready_report() -> ReadinessReport:
    report = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable"):
        report.add(Check(name, CheckStatus.PASS, ""))
    report.add(Check("Local transcription smoke test", CheckStatus.FAIL,
                     "no model"))
    report.add(Check("Local translation smoke test", CheckStatus.FAIL,
                     "no pack"))
    return report


def _untested_report() -> ReadinessReport:
    report = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local ASR model present", "Installed translation paths"):
        report.add(Check(name, CheckStatus.PASS, ""))
    report.add(Check("Local transcription smoke test", CheckStatus.SKIP, ""))
    report.add(Check("Local translation smoke test", CheckStatus.SKIP, ""))
    return report


def test_a_verified_ready_report_reads_ready(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    settle_readiness(qt_app, window)
    window._render_readiness(_ready_report())
    text = window.ready_badge.text()
    assert "Ready" in text and "Field ready" not in text
    assert text.split()[-1] == "Ready", f"unexpected qualifier in {text!r}"
    assert window.ready_badge.accessibleDescription().startswith("Ready"), (
        "the accessible text does not say what the chip says")
    window.close()


def test_a_capability_limitation_is_not_called_record_only(qt_app, config,
                                                           store):
    """Design choice: the chip describes what is installed, so it must not
    borrow the name of an operating mode the operator did not select."""
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    settle_readiness(qt_app, window)
    window._render_readiness(_partly_ready_report())
    text = window.ready_badge.text()
    assert "Partly ready" in text
    assert "record only" not in text.lower()
    assert "not the operating mode" in window.ready_badge.accessibleDescription()
    window.close()


def test_readiness_wording_does_not_alter_readiness_decisions(qt_app, config,
                                                              store):
    """Each report lands in the branch its own properties dictate, and
    rendering changes none of those properties."""
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(mock_app(config, store))
    settle_readiness(qt_app, window)

    cases = [
        (_ready_report(), "Ready", dict(field_ready=True)),
        (_untested_report(), "Checking", dict(field_ready=False,
                                              field_ready_unknown=True)),
        (_partly_ready_report(), "Partly ready", dict(field_ready=False,
                                                      can_record=True)),
    ]
    broken = ReadinessReport()
    broken.add(Check("Audio backend", CheckStatus.FAIL, "none"))
    cases.append((broken, "Not ready", dict(can_record=False)))

    for report, expected, facts in cases:
        before = report.to_dict()
        window._render_readiness(report)
        assert expected in window.ready_badge.text(), (
            report.to_dict(), window.ready_badge.text())
        for key, value in facts.items():
            assert getattr(report, key) is value
        assert report.to_dict() == before, "rendering changed the report"

    # A skipped smoke test is never "Ready".
    window._render_readiness(_untested_report())
    assert window.ready_badge.text().split()[-1] != "Ready"
    window.close()


# ---- 4. each bubble shows its own local date and time --------------------


def _with_tz(name):
    """Run the local-time conversion under a chosen zone, then put it back."""
    import contextlib

    @contextlib.contextmanager
    def manager():
        if not hasattr(time, "tzset"):
            pytest.skip("time.tzset is not available on this platform")
        previous = os.environ.get("TZ")
        os.environ["TZ"] = name
        time.tzset()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    return manager()


HISTORICAL = dt.datetime(2025, 3, 15, 23, 30, 0, tzinfo=dt.timezone.utc)


def _historical_transmission(**overrides) -> Transmission:
    fields = dict(id="tx_hist", session_id="s", duration=3.0,
                  started_at=HISTORICAL, transcript="original words",
                  state=ProcessingState.COMPLETE, source_language="en",
                  target_language="en")
    fields.update(overrides)
    return Transmission(**fields)


def test_the_bubble_shows_the_local_date_and_time_of_the_transmission(qt_app):
    from babelfishr.ui.timeline import TimelineView

    with _with_tz("UTC"):
        view = TimelineView()
        header = view.add(_historical_transmission()).header.text()
    assert "2025-03-15 23:30:00" in header, header
    # Not today's date, whatever today is.
    assert dt.datetime.now().strftime("%Y-%m-%d") not in header or \
        dt.datetime.now().strftime("%Y-%m-%d") == "2025-03-15"


def test_the_date_crosses_the_local_boundary_with_the_timezone(qt_app):
    """23:30 UTC on the 15th is 12:30 on the 16th in Auckland. The date must
    follow the computer's zone, not the stored UTC value."""
    from babelfishr.ui.timeline import TimelineView

    with _with_tz("Pacific/Auckland"):
        view = TimelineView()
        header = view.add(_historical_transmission()).header.text()
    assert "2025-03-16 12:30:00" in header, header
    assert "2025-03-15" not in header


def test_updating_the_transcript_does_not_change_the_transmission_date(qt_app):
    from babelfishr.ui.timeline import TimelineView

    with _with_tz("UTC"):
        view = TimelineView()
        bubble = view.add(_historical_transmission())
        first = bubble.header.text()
        # Reprocessed today, with new words. The date stays the recording's.
        bubble.update_from(_historical_transmission(
            transcript="corrected words after reprocessing"))
        second = bubble.header.text()
    assert "2025-03-15 23:30:00" in first
    assert "2025-03-15 23:30:00" in second
    assert first.split("  ·")[0] == second.split("  ·")[0]
    assert bubble.tx.transcript == "corrected words after reprocessing", (
        "the update itself did not land, so the date check proves nothing")


def test_the_stored_timestamp_and_the_order_are_untouched(qt_app):
    from babelfishr.ui.timeline import TimelineView

    older = _historical_transmission(id="tx_old")
    newer = _historical_transmission(
        id="tx_new", started_at=HISTORICAL + dt.timedelta(days=40))
    with _with_tz("UTC"):
        view = TimelineView()
        view.set_transmissions([older, newer])
    assert view.order() == ["tx_new", "tx_old"], "newest is not first"
    assert older.started_at == HISTORICAL, "rendering mutated started_at"
    assert older.started_at.tzinfo is not None


# ---- 5. the badge says Listening only while something is listening ------


def test_processing_a_saved_recording_does_not_end_in_listening(
        qt_app, config, store, wav):
    """The reproduction: no monitoring, Transcribe anyway, drain."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    window._stop_monitoring()
    pump(qt_app, 40)
    tx = app.recent_transmissions()[0]
    assert app.capture is None and app.session is None

    assert app.transcribe_anyway(tx.id)
    assert app.standalone_pipeline.wait_until_idle(30.0)
    pump(qt_app, 60)

    assert "Listening" not in window.state_badge.text(), (
        f"no microphone is open, but the badge says {window.state_badge.text()!r}")
    assert "Idle" in window.state_badge.text()
    assert window.start_button.text() == "Start monitoring"
    # And the work itself still arrived where it belongs.
    updated = store.get_transmission(tx.id)
    assert updated.state is ProcessingState.COMPLETE
    bubble = window.timeline._bubbles[tx.id]
    assert bubble.tx.state is ProcessingState.COMPLETE, (
        "the transcript update never reached the bubble")
    window.close()


def test_events_finishing_after_stop_do_not_revive_listening(qt_app, config,
                                                             store, wav):
    """Stop, then the timer drains what the run left behind."""
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    window._stop_monitoring()
    assert "Idle" in window.state_badge.text()
    assert app.events._queue.qsize() > 0, (
        "nothing was left in the queue, so this proves nothing")
    pump(qt_app, 60)
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    assert window.start_button.text() == "Start monitoring"
    assert window.timeline.count() >= 1, "the run's bubbles were discarded"
    window.close()


def test_stale_capture_states_from_an_earlier_run_are_ignored_but_updates_land(
        qt_app, config, store, wav):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    window._stop_monitoring()
    pump(qt_app, 40)
    tx = app.recent_transmissions()[0]

    # What a slow worker from the finished run might still emit. Asserted
    # after each one, so no later event can paper over a wrong display.
    for stale in (PipelineState.LISTENING, PipelineState.RECEIVING,
                  PipelineState.TRANSCRIBING, PipelineState.TRANSLATING):
        app.events.publish("state", stale)
        window._drain_events()
        assert "Idle" in window.state_badge.text(), (
            f"stale {stale} was displayed: {window.state_badge.text()!r}")
    tx.transcript = "late words"
    app.events.publish("updated", tx)
    window._drain_events()
    assert window.timeline._bubbles[tx.id].tx.transcript == "late words", (
        "suppressing stale states also suppressed the message update")
    assert "Idle" in window.state_badge.text()
    window.close()


def test_live_capture_states_still_display_while_monitoring(qt_app, config,
                                                            store, wav):
    """The gate must not flatten a genuine run to Idle."""
    from babelfishr.ui.main_window import MainWindow

    from babelfishr.audio.source import CallbackAudioSource

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(source=CallbackAudioSource(SR), name="live")
    app.begin_capture()                      # a real capture thread, listening
    assert pump_until(qt_app, lambda: app.capture.state == PipelineState.LISTENING)
    window._drain_events()
    assert "Listening" in window.state_badge.text()
    # The detector opening: the service sets its state and publishes it.
    app.capture._set_state(PipelineState.RECEIVING)
    window._drain_events()
    assert "Receiving" in window.state_badge.text()
    # Processing finished mid-run: back to what the capture is doing.
    window._set_state(PipelineState.COMPLETE)
    assert "Receiving" in window.state_badge.text()
    app.capture._set_state(PipelineState.LISTENING)
    window._drain_events()
    window._set_state(PipelineState.COMPLETE)
    assert "Listening" in window.state_badge.text()
    app.stop_session()
    window._drain_events()
    window._set_state(PipelineState.COMPLETE)
    assert "Idle" in window.state_badge.text()
    window.close()


def test_the_pipeline_reports_finished_not_listening(config, store, wav):
    """At the source: the processing pipeline has no microphone to speak
    for, so it says COMPLETE and leaves the capture question to the window."""
    app = mock_app(config, store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    tx = app.recent_transmissions()[0]

    states = []
    app.events.subscribe(
        lambda e: states.append(e.payload) if e.kind == "state" else None)
    assert app.transcribe_anyway(tx.id)
    assert app.standalone_pipeline.wait_until_idle(30.0)
    assert PipelineState.COMPLETE in states
    assert PipelineState.LISTENING not in states, (
        "a pipeline with no capture claimed to be listening")
    assert PipelineState.TRANSCRIBING in states


# ---- 6. every completion path recovers; only a *current* capture counts --


class _EmptyEngine(MockTranscriptionEngine):
    """An engine that hears nothing: the no-speech result."""

    id = "test-empty"
    name = "Test empty transcription"

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        from babelfishr.providers.base import TranscriptionResult

        self.calls += 1
        return TranscriptionResult(engine=self.id, engine_version="1",
                                   no_speech=True)


class _FailingEngine(MockTranscriptionEngine):
    id = "test-failing"
    name = "Test failing transcription"

    def __init__(self):
        super().__init__(fail=True, fail_message="asr blew up")


class _BlockingEngine(MockTranscriptionEngine):
    """Holds transcription until released, so processing is observably
    in flight."""

    id = "test-blocking"
    name = "Test blocking transcription"

    def __init__(self):
        import threading

        super().__init__()
        self.release = threading.Event()
        self.started = threading.Event()

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        self.started.set()
        assert self.release.wait(30.0), "the test never released the engine"
        return super().transcribe(audio, sample_rate, language=language,
                                  vocabulary=vocabulary)


class _BlockingEmptyEngine(_BlockingEngine):
    """Blocks, then hears nothing - the empty path while it is observable."""

    id = "test-blocking-empty"
    name = "Test blocking empty transcription"

    def transcribe(self, audio, sample_rate, *, language=None, vocabulary=None):
        from babelfishr.providers.base import TranscriptionResult

        self.started.set()
        assert self.release.wait(30.0), "the test never released the engine"
        self.calls += 1
        return TranscriptionResult(engine=self.id, engine_version="1",
                                   no_speech=True)


@pytest.fixture
def test_engines(monkeypatch):
    """Register the test engines with the production factory.

    Selecting them goes through config.asr.engine and
    build_transcription_engine like any real engine, so the engine the
    pipeline ends up holding is the factory's choice - not a reference the
    test handed it. Each construction is recorded so identity can be checked.
    """
    import babelfishr.providers as providers

    created = []
    real = providers._transcription_factories

    def factories(config=None, mode=None):
        table = real(config, mode)
        for cls in (_EmptyEngine, _FailingEngine, _BlockingEngine,
                    _BlockingEmptyEngine):
            def make(cls=cls):
                engine = cls()
                created.append(engine)
                return engine
            table[cls.id] = make
        return table

    monkeypatch.setattr(providers, "_transcription_factories", factories)
    return created


def _captured_then_stopped(qt_app, config, store, wav):
    """A window, one captured transmission, monitoring stopped, badge Idle."""
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)   # engines from the factory
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    window._stop_monitoring()
    pump(qt_app, 40)
    assert "Idle" in window.state_badge.text()
    assert app.standalone_pipeline is None, "a standalone processor already exists"
    return app, window, app.recent_transmissions()[0]


def _reprocess_with(app, window, qt_app, tx, engine_id, test_engines):
    """Switch the configured engine, reprocess, and prove that engine ran."""
    app.config.asr.engine = engine_id
    before = len(test_engines)
    assert app.transcribe_anyway(tx.id)
    assert app.standalone_pipeline.wait_until_idle(30.0)
    pump(qt_app, 60)
    used = app.standalone_pipeline.transcription
    assert used.id == engine_id, f"the pipeline holds {used.id!r}, not {engine_id!r}"
    assert used in test_engines[before:], (
        "the pipeline's engine is not one the factory built for this test")
    assert app.standalone_pipeline.pending == 0
    return used


def test_an_empty_transcription_returns_the_display_to_idle(
        qt_app, config, store, wav, test_engines):
    app, window, tx = _captured_then_stopped(qt_app, config, store, wav)
    used = _reprocess_with(app, window, qt_app, tx, "test-empty", test_engines)
    assert used.calls == 1, "the empty engine was never actually asked"

    after = store.get_transmission(tx.id)
    assert after.state is ProcessingState.COMPLETE, "an empty result is complete"
    assert after.transcript == ""
    assert after.error is None
    assert "Transcribing" not in window.state_badge.text(), (
        f"nothing is left to transcribe, badge says {window.state_badge.text()!r}")
    assert "Idle" in window.state_badge.text()
    assert window.timeline._bubbles[tx.id].tx.state is ProcessingState.COMPLETE
    window.close()


def test_a_transcription_error_returns_the_display_to_idle(
        qt_app, config, store, wav, test_engines):
    app, window, tx = _captured_then_stopped(qt_app, config, store, wav)
    used = _reprocess_with(app, window, qt_app, tx, "test-failing", test_engines)
    assert used.calls == 1

    after = store.get_transmission(tx.id)
    assert after.state is ProcessingState.FAILED, "the failure must be kept"
    assert after.error is not None and "asr blew up" in after.error.message
    assert pathlib.Path(after.audio_path).exists(), "the recording must survive"
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    assert window.timeline._bubbles[tx.id].tx.state is ProcessingState.FAILED
    window.close()


def test_a_missing_recording_returns_the_display_to_idle(
        qt_app, config, store, wav, test_engines):
    app, window, tx = _captured_then_stopped(qt_app, config, store, wav)
    pathlib.Path(tx.audio_path).unlink()
    used = _reprocess_with(app, window, qt_app, tx, "test-empty", test_engines)
    assert used.calls == 0, "the engine ran with no audio to give it"

    after = store.get_transmission(tx.id)
    assert after.state is ProcessingState.FAILED
    assert after.error is not None and "missing" in after.error.message
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    window.close()


def test_processing_activity_shows_only_while_work_is_in_flight(
        qt_app, config, store, wav, test_engines):
    """Transcribing is shown with no microphone open - while it is true."""
    app, window, tx = _captured_then_stopped(qt_app, config, store, wav)
    app.config.asr.engine = "test-blocking"
    assert app.transcribe_anyway(tx.id)
    engine = app.standalone_pipeline.transcription
    assert engine.id == "test-blocking" and engine in test_engines
    assert engine.started.wait(10.0), "processing never began"
    assert app.standalone_pipeline.pending == 1
    pump(qt_app, 20)
    assert "Transcribing" in window.state_badge.text(), window.state_badge.text()

    engine.release.set()
    assert app.standalone_pipeline.wait_until_idle(30.0)
    pump(qt_app, 60)
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    assert store.get_transmission(tx.id).state is ProcessingState.COMPLETE

    # And a Transcribing that arrives with nothing in flight describes
    # nothing current.
    app.events.publish("state", PipelineState.TRANSCRIBING)
    window._drain_events()
    assert "Idle" in window.state_badge.text()
    window.close()


def test_an_empty_result_after_a_visible_transcribing_returns_to_idle(
        qt_app, config, store, wav, test_engines):
    """The empty path with the display genuinely showing Transcribing first.

    With a fast engine the work finishes before the window drains, and the
    in-flight check alone would make the badge right even if the pipeline
    never said "finished". Holding the engine open makes the window commit to
    Transcribing, so only a real completion signal can bring it back.
    """
    app, window, tx = _captured_then_stopped(qt_app, config, store, wav)
    app.config.asr.engine = "test-blocking-empty"
    assert app.transcribe_anyway(tx.id)
    engine = app.standalone_pipeline.transcription
    assert engine.id == "test-blocking-empty" and engine in test_engines
    assert engine.started.wait(10.0)
    pump(qt_app, 20)
    assert "Transcribing" in window.state_badge.text()

    engine.release.set()
    assert app.standalone_pipeline.wait_until_idle(30.0)
    assert engine.calls == 1
    # Drain exactly what the pipeline published; no synthetic events.
    window._drain_events()
    assert "Idle" in window.state_badge.text(), window.state_badge.text()
    after = store.get_transmission(tx.id)
    assert after.state is ProcessingState.COMPLETE and after.transcript == ""
    window.close()


def test_the_pipeline_says_finished_on_every_path_out(config, store, wav,
                                                      test_engines):
    """At the source: COMPLETE follows the last update whether the
    transmission ended complete, empty or failed."""
    app = BabelFishRApp(config=config, store=store)
    app.start_session(replay_path=wav, name="run")
    app.run_replay()
    app.stop_session()
    tx = app.recent_transmissions()[0]

    for engine_id, expected in (("test-empty", ProcessingState.COMPLETE),
                                ("test-failing", ProcessingState.FAILED)):
        app.config.asr.engine = engine_id
        if app.standalone_pipeline is not None:
            app.standalone_pipeline.stop(wait=True)
            app.standalone_pipeline = None
        seen = []
        app.events.subscribe(lambda e, seen=seen: seen.append(e))
        assert app.transcribe_anyway(tx.id)
        assert app.standalone_pipeline.wait_until_idle(30.0)
        states = [e.payload for e in seen if e.kind == "state"]
        assert states and states[-1] == PipelineState.COMPLETE, (engine_id, states)
        assert PipelineState.LISTENING not in states
        updates = [e.payload for e in seen if e.kind == "updated"]
        assert updates[-1].state is expected
        assert store.get_transmission(tx.id).state is expected
    app.close()


def test_a_created_but_unstarted_capture_does_not_make_listening_true(
        qt_app, config, store):
    from babelfishr.audio.source import CallbackAudioSource
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(source=CallbackAudioSource(SR), name="unstarted")
    assert app.capture is not None and app.capture.state == PipelineState.IDLE
    window._drain_events()
    for stale in (PipelineState.LISTENING, PipelineState.RECEIVING):
        app.events.publish("state", stale)
        window._drain_events()
        assert "Idle" in window.state_badge.text(), (
            f"an unstarted capture displayed {window.state_badge.text()!r}")
    app.stop_session()
    window.close()


def test_an_obsolete_receiving_does_not_overwrite_a_listening_run(
        qt_app, config, store):
    """A new capture is genuinely running and listening; a stale Receiving
    from earlier must not be believed over it."""
    from babelfishr.audio.source import CallbackAudioSource
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    window = MainWindow(app)
    pump(qt_app)
    app.start_session(source=CallbackAudioSource(SR), name="live")
    app.begin_capture()
    assert pump_until(qt_app, lambda: app.capture.state == PipelineState.LISTENING)
    assert app.capture._running
    window._drain_events()
    assert "Listening" in window.state_badge.text()

    app.events.publish("state", PipelineState.RECEIVING)
    window._drain_events()
    assert "Listening" in window.state_badge.text(), (
        f"a stale Receiving was displayed: {window.state_badge.text()!r}")
    app.events.publish("state", PipelineState.TRANSCRIBING)
    window._drain_events()
    assert "Listening" in window.state_badge.text()

    # The run's own transitions still show.
    app.capture._set_state(PipelineState.RECEIVING)
    window._drain_events()
    assert "Receiving" in window.state_badge.text()
    app.stop_session()
    window.close()


def test_late_updates_reach_their_own_session_only(qt_app, config, store, wav):
    from babelfishr.ui.main_window import MainWindow

    app = mock_app(config, store)
    other = app.create_conversation("Other")
    window = MainWindow(app)
    pump(qt_app)
    general = store.default_conversation()
    app.select_conversation(general.id)
    window._refresh_session_tabs()

    # Traffic filed under Other while General is on screen.
    app.select_conversation(other.id)
    app.start_session(replay_path=wav, name="other-run")
    app.run_replay()
    app.stop_session()
    app.select_conversation(general.id)
    window._reload_timeline()
    window._drain_events()
    theirs = app.recent_transmissions(conversation_id=other.id)
    assert theirs
    stale = theirs[0]
    stale.transcript = "late words for Other"
    app.events.publish("state", PipelineState.LISTENING)
    app.events.publish("updated", stale)
    window._drain_events()
    assert "Idle" in window.state_badge.text()
    assert stale.id not in window.timeline._bubbles, (
        "Other's late update was drawn into General")

    # Now looking at Other: the same late update lands in its own thread.
    app.select_conversation(other.id)
    window._reload_timeline()
    assert stale.id in window.timeline._bubbles
    app.events.publish("state", PipelineState.RECEIVING)
    app.events.publish("updated", stale)
    window._drain_events()
    assert window.timeline._bubbles[stale.id].tx.transcript == "late words for Other"
    assert "Idle" in window.state_badge.text()
    window.close()
