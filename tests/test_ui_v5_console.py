"""The v5 field console: real states in the band, selection driving the
recording waveform and Selected Audio, filters and search through the real
store queries, the in-window navigation bar, theme and font.

Synthetic data only: WAVs written to tmp_path, mock engines."""

from __future__ import annotations

import datetime as _dt
import os

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtGui, QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def system_palette(qt_app):
    original = QtGui.QPalette(QtWidgets.QApplication.palette())

    def set_system(dark: bool):
        palette = QtGui.QPalette()
        palette.setColor(QtGui.QPalette.Window,
                         QtGui.QColor("#1e1e1e" if dark else "#efefef"))
        palette.setColor(QtGui.QPalette.WindowText,
                         QtGui.QColor("#ffffff" if dark else "#000000"))
        QtWidgets.QApplication.setPalette(palette)

    yield set_system
    QtWidgets.QApplication.setPalette(original)


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    color = QtGui.QColor(hex_color)
    return (0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def pump(qt_app, rounds: int = 5):
    for _ in range(rounds):
        qt_app.processEvents()


def _seed(store, tmp_path):
    """Three messages in a named Session: two with real synthetic WAVs (one
    bookmarked, one needing review), one whose recording is missing."""
    from babelfishr.audio.wavefile import write_wav
    from babelfishr.models import ProcessingState, Session, Transmission, utcnow
    from babelfishr.testing import speech_like

    conversation = store.create_conversation("Console")
    session = Session(name="Console", conversation_id=conversation.id,
                      sample_rate=48_000)
    store.save_session(session)
    start = utcnow() - _dt.timedelta(minutes=10)
    made = []
    for index, (text, kwargs) in enumerate((
            ("Bonjour, ceci est un test audio.", {"bookmarked": True}),
            ("Pouvez-vous répéter le dernier mot ?", {"transcript_confidence": 0.3}),
            ("Sans enregistrement.", {"missing": True}))):
        missing = kwargs.pop("missing", False)
        tx = Transmission(session_id=session.id,
                          started_at=start + _dt.timedelta(minutes=index),
                          duration=1.5, sample_rate=48_000, source_language="fr",
                          transcript=text, translation=f"[en] {text}",
                          state=ProcessingState.COMPLETE, **kwargs)
        tx.ended_at = tx.started_at + _dt.timedelta(seconds=1.5)
        path = tmp_path / f"clip_{index}.wav"
        if not missing:
            write_wav(str(path), speech_like(1.5, 48_000), 48_000)
        tx.audio_path = str(path)
        store.save_transmission(tx)
        made.append(tx)
    return conversation, made


@pytest.fixture
def console(qt_app, config, store, tmp_path):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    conversation, made = _seed(store, tmp_path)
    app = BabelFishRApp(config=config, store=store)
    app.select_conversation(conversation.id)
    window = MainWindow(app)
    window._reload_timeline()
    window.resize(1200, 900)
    window.show()
    pump(qt_app)
    yield window, app, made
    window.timeline.playback.stop()
    window.waveform.clear()
    window.close()
    pump(qt_app)


# ---- real states, never sample text ------------------------------------------

def test_the_band_shows_the_real_mode_source_and_state(console):
    from babelfishr.modes import OperatingMode

    window, app, _ = console
    assert window.title_note.text() == "", "no sample/preview label in the app"
    assert app.mode.label in window.mode_button.text()
    assert window.mode_button.accessibleName() == "Operating mode"
    window._refresh_context_lines()
    status = window.input_panel.status_label.text()
    expected = status.split(":", 1)[1].strip() if ":" in status else status
    assert window.source_label.text() == f"SOURCE: {expected or 'none selected'}"
    assert "Idle" in window.state_badge.text()
    assert window.state_badge.font().capitalization() == QtGui.QFont.AllUppercase
    # Changing the real mode changes the band - nothing hard-coded.
    window._apply_mode(OperatingMode.RECORD_ONLY.value)
    assert OperatingMode.RECORD_ONLY.label in window.mode_button.text()


def test_navigation_is_the_real_menu_bar_under_the_band(console):
    window, _, _ = console
    bar = window.menuBar()
    assert bar is window._nav_bar
    titles = [a.text().replace("&", "") for a in bar.actions()]
    assert titles == ["File", "View", "Receiver", "Tools", "Help"]
    band_bottom = window.status_band.mapTo(window, QtCore.QPoint(0, 0)).y() \
        + window.status_band.height()
    assert bar.mapTo(window, QtCore.QPoint(0, 0)).y() >= band_bottom - 1


def test_engine_lines_and_counts_come_from_the_application(console):
    window, app, made = console
    summary = app.select_engines()
    assert window.speech_engine_line.text() == f"Speech: {summary.transcription}"
    assert window.translation_engine_line.text() == f"Translation: {summary.translation}"
    window._refresh_counts()
    order = window.timeline.order()
    review = sum(1 for i in order if window.timeline.bubble(i).tx.needs_review)
    assert window.counts_label.text().startswith(f"{len(order)} messages")
    assert f"{review} need" in window.counts_label.text()
    assert "Original audio retained" in window.counts_label.text()


# ---- selection drives the waveform and Selected Audio ------------------------

def test_selecting_a_message_shows_its_own_recording(qt_app, console):
    window, _, made = console
    assert window.waveform.state in ("empty", "none")
    assert not window.selected_play_button.isEnabled()

    window.timeline.select(made[0].id)
    assert window.waveform.wait_until_loaded(10.0)
    assert window.waveform.state == "ready"
    assert window.waveform.current_tx_id == made[0].id
    assert window.waveform.envelope.frames == int(1.5 * 48_000)
    assert window.timeline.bubble(made[0].id).property("selected") is True
    assert window.selected_play_button.isEnabled()
    start = made[0].started_at.astimezone().strftime("%H:%M:%S")
    assert window.selected_range.text().startswith(start)
    assert window.selected_bookmark_button.isChecked()     # it is bookmarked

    # Another row: the first is no longer selected, the waveform follows.
    window.timeline.select(made[1].id)
    assert window.waveform.wait_until_loaded(10.0)
    assert window.waveform.current_tx_id == made[1].id
    assert window.timeline.bubble(made[0].id).property("selected") is False
    assert "Check uncertain wording" in window.selected_hint.text()


def test_a_missing_recording_is_said_never_drawn(qt_app, console):
    window, _, made = console
    window.timeline.select(made[2].id)
    pump(qt_app)
    assert window.waveform.state == "missing"
    assert window.waveform.envelope is None


def test_leaving_the_view_clears_the_selection_and_the_waveform(qt_app, console):
    window, _, made = console
    window.timeline.select(made[0].id)
    window.waveform.wait_until_loaded(10.0)
    window._reload_timeline()
    pump(qt_app)
    assert window.timeline.selected_id is None
    assert window.waveform.state in ("empty", "none")
    assert not window.selected_play_button.isEnabled()


def test_play_original_uses_the_shared_playback_controller(qt_app, console,
                                                         monkeypatch):
    window, _, made = console
    calls = []
    monkeypatch.setattr(window.timeline.playback, "toggle",
                        lambda tx_id, path: calls.append((tx_id, path)))
    window.timeline.select(made[1].id)
    window.selected_play_button.click()
    assert calls == [(made[1].id, made[1].audio_path)]


def test_keyboard_moves_the_selection(qt_app, console):
    window, _, made = console
    order = window.timeline.order()
    for expected in (order[0], order[1]):
        QtWidgets.QApplication.sendEvent(window.timeline, QtGui.QKeyEvent(
            QtCore.QEvent.KeyPress, QtCore.Qt.Key_Down, QtCore.Qt.NoModifier))
        assert window.timeline.selected_id == expected


# ---- filters and search: the real store queries -------------------------------

def test_filters_and_search_run_the_real_views(qt_app, console):
    window, _, made = console
    window.filter_bookmarked.click()
    pump(qt_app)
    assert window._view_kind == "bookmarked"
    assert window.timeline.order() == [made[0].id]
    assert window.filter_bookmarked.isChecked()

    window.filter_review.click()
    pump(qt_app)
    assert window._view_kind == "review"
    assert made[1].id in window.timeline.order()
    assert made[0].id not in window.timeline.order()

    window.search_field.setText("répéter")
    window.search_field.returnPressed.emit()
    pump(qt_app)
    assert window._view_kind == "search"
    assert window.timeline.order() == [made[1].id]
    assert not any(b.isChecked() for b in
                   (window.filter_all, window.filter_review, window.filter_bookmarked))

    window.filter_all.click()
    pump(qt_app)
    assert window._view_kind == "thread"
    assert window.search_field.text() == ""
    assert len(window.timeline.order()) == 3


def test_the_view_menu_paths_keep_the_filter_buttons_true(qt_app, console):
    window, _, _ = console
    window.review_action.trigger()
    assert window.filter_review.isChecked()
    window.show_all_action.trigger()
    assert window.filter_all.isChecked()


# ---- theme, font, resizing ------------------------------------------------------

def test_the_font_is_the_platform_monospace_at_normal_weight(qt_app):
    from babelfishr.ui import theme

    font = theme.monospace_font(13)
    assert QtGui.QFontInfo(font).fixedPitch()
    assert font.weight() == QtGui.QFont.Normal


def test_console_and_daylight_contrast():
    from babelfishr.ui.theme import CONSOLE, DAYLIGHT, STATUS_DARK, STATUS_LIGHT

    for t, status in ((CONSOLE, STATUS_DARK), (DAYLIGHT, STATUS_LIGHT)):
        assert _contrast(t["text"], t["window"]) >= 7.0
        assert _contrast(t["text_muted"], t["window"]) >= 4.5
        assert _contrast(t["on_band"], t["band"]) >= 4.5
        assert _contrast(t["on_accent"], t["accent"]) >= 7.0
        for tone in ("working", "error"):
            assert _contrast(status[tone], t["window"]) >= 4.5, tone


def test_the_window_follows_the_system_appearance(qt_app, config, store,
                                                  system_palette):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui import theme
    from babelfishr.ui.main_window import MainWindow

    system_palette(dark=True)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert window.palette().color(QtGui.QPalette.Window).name().lower() == \
        theme.CONSOLE["window"].lower()
    system_palette(dark=False)
    window._apply_theme()
    assert window.palette().color(QtGui.QPalette.Window).name().lower() == \
        theme.DAYLIGHT["window"].lower()
    window.close()


def test_the_window_resizes_down_to_its_minimum_and_back(qt_app, console):
    window, _, made = console
    window.timeline.select(made[0].id)
    window.waveform.wait_until_loaded(10.0)
    for width in (window.minimumSizeHint().width(), 1600):
        window.resize(width, 820)
        pump(qt_app)
        assert not window.grab().isNull()
    assert window.minimumSizeHint().width() <= 1000
