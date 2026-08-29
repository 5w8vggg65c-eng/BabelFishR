"""Headless checks on the Qt front-end."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.models import ProcessingState, Transmission  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_timeline_renders_a_bubble(qt_app):
    from babelfishr.ui.timeline import TimelineView

    timeline = TimelineView()
    tx = Transmission(transcript="hola", translation="hello", source_language="es",
                      duration=2.0, channel_name="GMRS 16", frequency_mhz=462.575,
                      state=ProcessingState.COMPLETE)
    bubble = timeline.add(tx)
    assert timeline.count() == 1
    assert "GMRS 16" in bubble.header.text()
    assert "hola" in bubble.original_label.text()
    assert "hello" in bubble.translated_label.text()


def test_bubble_separates_original_from_translation(qt_app):
    from babelfishr.ui.timeline import TimelineView

    timeline = TimelineView()
    bubble = timeline.add(Transmission(transcript="hola", translation="hello",
                                       source_language="es", target_language="en"))
    assert bubble.original_label.text() != bubble.translated_label.text()
    assert "es" in bubble.original_label.text()
    assert "en" in bubble.translated_label.text()


def test_failed_bubble_offers_retry_and_reassures(qt_app):
    from babelfishr.ui.timeline import TimelineView

    tx = Transmission(transcript="kept")
    tx.fail("translation", "api down")
    bubble = TimelineView().add(tx)
    assert bubble.retry_button.isVisibleTo(bubble)
    assert "api down" in bubble.error_label.text()
    assert "safe" in bubble.error_label.text().lower()


def test_provisional_text_is_marked_as_such(qt_app):
    from babelfishr.ui.timeline import TimelineView

    bubble = TimelineView().add(Transmission())
    bubble.set_provisional("partial words")
    assert "<i>" in bubble.provisional.text()
    assert "partial words" in bubble.provisional.text()


def test_meter_widget_paints(qt_app):
    from babelfishr.ui.widgets import LevelMeterWidget

    meter = LevelMeterWidget()
    meter.resize(200, 22)
    meter.set_reading(0.6, 0.8, True, 5)
    assert not meter.grab().isNull()


def test_main_window_states_and_banners(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert window.state_badge.text() == "Idle"
    # Local-only engines must say so plainly.
    assert "computer" in window.privacy_banner.text()
    # A mock engine must be called out, not quietly accepted.
    assert window.warning_banner.isVisibleTo(window)
    assert "MOCK" in window.warning_banner.text()
    window.close()


def test_main_window_warns_when_no_profile_is_selected(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert "cannot determine" in window.channel_label.text()
    window.close()


def test_replay_populates_the_timeline(qt_app, config, store, fixture_wav,
                                       expected_transmissions):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="ui-replay")
    app.run_replay()
    window._drain_events()
    qt_app.processEvents()
    assert window.timeline.count() == expected_transmissions
    app.stop_session()
    window.close()
