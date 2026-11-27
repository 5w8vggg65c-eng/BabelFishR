"""Qt object lifetimes: nothing of ours runs while the collector tears down.

Mac CI run #8 (ab9757b) died with a segfault while the main thread was
garbage-collecting during an unrelated test. Investigation found that
ab9757b's appearance follower - an event-filter object held by its window,
holding the window - formed a reference cycle; collecting it, Qt called
the follower's Python eventFilter on an object the collector had already
cleared (78 times for 80 dialogs on Linux). That follower is gone (the
windows use their own changeEvent); the Space and keyboard-menu filters
hold their window weakly; the readiness watchdog is one timer per window,
not one per check (35 checks used to leave 35 timers behind).
"""

from __future__ import annotations

import gc
import sys

import pytest
from PySide6 import QtCore, QtWidgets

from babelfishr.app import BabelFishRApp
from test_offline_indicator import Jobs
from test_offline_readiness import ready_report


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def unraisable(monkeypatch):
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", lambda info: seen.append(info))
    return seen


def test_styled_dialogs_are_collected_without_running_our_code(qt_app, config, store,
                                                                tmp_path, capfd, unraisable):
    from babelfishr.ui import theme
    from babelfishr.ui.calibration_dialog import CalibrationDialog
    from babelfishr.ui.readiness_dialog import ReadinessDialog
    from babelfishr.ui.setup_assistant import SetupAssistant

    config.app_home = str(tmp_path / "home")
    app = BabelFishRApp(config=config, store=store)
    assert not hasattr(theme, "_FollowAppearance")
    for _ in range(40):
        made = [SetupAssistant(app), ReadinessDialog(app, report=ready_report()),
                CalibrationDialog(lambda: None, seconds=1.0)]
        for dialog in made:
            dialog.show()
            qt_app.processEvents()
            # No helper object of ours watches the window's events.
            assert not [c for c in dialog.findChildren(QtCore.QObject)
                        if type(c).__module__.startswith("babelfishr")
                        and "eventFilter" in type(c).__dict__]
            dialog.close()
        del made, dialog
    gc.collect()
    qt_app.processEvents()
    gc.collect()
    err = capfd.readouterr().err
    assert "Error calling Python override" not in err, err[-2000:]
    assert not unraisable


def test_appearance_still_follows_the_system(qt_app, config, store, tmp_path):
    from PySide6 import QtGui

    from babelfishr.ui import theme
    from babelfishr.ui.setup_assistant import SetupAssistant

    config.app_home = str(tmp_path / "home")
    setup = SetupAssistant(BabelFishRApp(config=config, store=store))
    palette = qt_app.palette()
    original = QtGui.QPalette(palette)
    try:
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#f0f0f0"))
        qt_app.setPalette(palette)
        qt_app.processEvents()
        assert setup.styleSheet() == theme.stylesheet(setup)
        light = setup.styleSheet()
        palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#1e1e1e"))
        qt_app.setPalette(palette)
        qt_app.processEvents()
        assert setup.styleSheet() != light
    finally:
        qt_app.setPalette(original)
        setup.close()


def test_readiness_checks_do_not_accumulate_timers(qt_app, config, store, tmp_path,
                                                   monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    jobs = Jobs(monkeypatch)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        def timers():
            return len(window.findChildren(QtCore.QTimer, options=QtCore.Qt.FindDirectChildrenOnly))

        window._refresh_readiness()
        jobs.finish(-1, ready_report())
        before = timers()
        for _ in range(35):                         # the director's measurement
            window._refresh_readiness()
            jobs.finish(-1, ready_report())
        assert timers() == before, f"{timers() - before} timers left behind"
    finally:
        window.close()


def test_a_window_is_collected_without_its_filters_running(qt_app, config, store, tmp_path,
                                                           monkeypatch, capfd, unraisable):
    from babelfishr.ui.main_window import MainWindow

    from babelfishr.storage import Store

    config.app_home = str(tmp_path / "home")
    Jobs(monkeypatch)
    for _ in range(3):
        # Each window closes its application's store: a fresh one each time.
        window = MainWindow(BabelFishRApp(config=config, store=Store(
            config.database, recordings_dir=config.recording.directory)))
        window.show()
        window.session_tabs.setFocus()               # the Space filter watches it
        qt_app.processEvents()
        window.close()
        del window
        gc.collect()
        qt_app.processEvents()
    gc.collect()
    err = capfd.readouterr().err
    assert "Error calling Python override" not in err, err[-2000:]
    assert not unraisable
