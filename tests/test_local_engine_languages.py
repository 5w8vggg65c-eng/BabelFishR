"""Language detection and source language under Local Engines.

Moved out of Session Options into the sidebar's Local Engines section:
language detection first, the source language directly below it. Detect
automatically keeps the source disabled; Specified enables it; the choice
is saved, comes back after a relaunch, starts monitoring with it, and is
locked while monitoring. The Translate-into control stays in Session
Options. There is no separate "English > English" summary line and no
explanatory no-profile line; Calibrate and Remember for profile stay.
Everything fits a narrow sidebar without a horizontal scroll bar.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from test_offline_indicator import Jobs


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=10):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


def _open(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(BabelFishRApp(config=config, store=store))
    window.show()
    pump(qt_app)
    return window


@pytest.fixture
def setup(qt_app, config, store, tmp_path, monkeypatch):
    config.app_home = str(tmp_path / "home")
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    jobs = Jobs(monkeypatch)
    window = _open(qt_app, config, store)
    yield window, jobs
    window.close()


def _sidebar_widgets(window):
    left = window.sidebar.widget()
    return [w for w in left.findChildren(QtWidgets.QWidget) if w.isVisibleTo(left)]


def test_they_sit_under_local_engines_source_directly_below_mode(qt_app, setup):
    window, _ = setup
    left = window.sidebar.widget()
    assert left.isAncestorOf(window.source_mode_box)
    assert left.isAncestorOf(window.source_language_box)
    assert not window.session_options_dialog.isAncestorOf(window.source_mode_box)
    assert window.session_options_dialog.isAncestorOf(window.target_language_box)
    y = lambda w: w.mapTo(left, w.rect().topLeft()).y()          # noqa: E731
    engines = [w for w in left.findChildren(QtWidgets.QLabel)
               if w.text() == "LOCAL ENGINES"][0]
    assert y(engines) < y(window.whisper_model_box) < y(window.source_mode_box) \
        < y(window.source_language_box)
    assert window.source_language_box.accessibleName() == "Source language"
    assert window.source_mode_box.accessibleName() == "Language detection"


def test_no_summary_or_no_profile_line_but_the_real_controls_remain(qt_app, setup):
    window, _ = setup
    window._refresh_context_lines()
    texts = [w.text() for w in _sidebar_widgets(window) if isinstance(w, QtWidgets.QLabel)]
    assert not any(" > " in t for t in texts), "a language summary line is shown"
    assert not any("No profile selected" in t for t in texts)
    assert not hasattr(window, "language_line")
    assert window.calibrate_button.isVisibleTo(window)
    check = window.input_panel.profile_check
    assert "radio profile" in check.toolTip() and "input" in check.toolTip()


def test_automatic_and_specified_save_and_survive_a_relaunch(qt_app, setup, config, store):
    window, jobs = setup
    assert window.source_mode_box.currentData() == "automatic"
    assert not window.source_language_box.isEnabled()
    calls = len(jobs.calls)
    window.source_mode_box.setCurrentIndex(window.source_mode_box.findData("specified"))
    assert window.source_language_box.isEnabled()
    window.source_language_box.setCurrentIndex(window.source_language_box.findData("es"))
    saved = Config.load(config.source_path).session
    assert (saved.source_language_mode, saved.source_language) == ("specified", "es")
    assert len(jobs.calls) > calls, "readiness was not rechecked for the new route"
    window.target_language_box.setCurrentIndex(window.target_language_box.findData("fr"))
    assert Config.load(config.source_path).translate.target_language == "fr"
    window.close()                                  # closes the app and its store

    from babelfishr.storage import Store

    store = Store(config.database, recordings_dir=config.recording.directory)
    reopened = _open(qt_app, Config.load(config.source_path), store)
    try:
        assert reopened.source_mode_box.currentData() == "specified"
        assert reopened.source_language_box.currentData() == "es"
        assert reopened.source_language_box.isEnabled()
        assert reopened.target_language_box.currentData() == "fr"
        reopened.source_mode_box.setCurrentIndex(0)                 # automatic again
        assert not reopened.source_language_box.isEnabled()
        assert Config.load(config.source_path).session.source_language_mode == "automatic"
    finally:
        reopened.close()
        store.close()


def test_monitoring_uses_the_choice_and_locks_it(qt_app, setup, wav_path):
    window, _ = setup
    window.source_mode_box.setCurrentIndex(window.source_mode_box.findData("specified"))
    window.source_language_box.setCurrentIndex(window.source_language_box.findData("de"))
    window._start_monitoring(replay_path=wav_path)
    try:
        assert window.app.session.source_language == "de"
        assert window.app.session.source_language_mode.value == "specified"
        assert not window.source_mode_box.isEnabled()
        assert not window.source_language_box.isEnabled()
    finally:
        window._stop_monitoring()
        window.app.wait_for_session_ends(10)
    assert window.source_mode_box.isEnabled() and window.source_language_box.isEnabled()


def test_a_narrow_or_collapsed_sidebar_never_scrolls_sideways(qt_app, setup):
    window, _ = setup
    window.resize(640, 600)
    pump(qt_app)
    window.sidebar.setFixedWidth(window.sidebar.minimumWidth())   # the narrowest
    pump(qt_app)
    left = window.sidebar.widget()
    assert left.width() <= window.sidebar.viewport().width()
    for box in (window.source_mode_box, window.source_language_box):
        assert box.geometry().right() <= left.width()
    window.sidebar_toggle.click()                       # collapse and restore
    pump(qt_app)
    window.sidebar_toggle.click()
    pump(qt_app)
    assert window.source_mode_box.isVisibleTo(window)


@pytest.fixture
def wav_path(tmp_path):
    from babelfishr.testing import build_fixture

    return build_fixture([{"gap": 0.5}, {"kind": "voice", "duration": 1.0, "level_dbfs": -14},
                          {"gap": 0.5}], sample_rate=16_000).write(str(tmp_path / "one.wav"))
