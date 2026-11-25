"""A fresh profile starts with Set up BabelFishR, in Online / Setup.

Eric's requirement: on first launch the setup window is the very first
application window - the console does not flash up first - nothing is
downloaded or monitored by itself, and the mode is Online / Setup unless the
operator explicitly chooses another (Record only). Completing setup on first
launch keeps Online / Setup. A returning profile keeps its saved mode
(Field Offline included) and completed setup, and goes straight to the
console. Tools > Setup assistant stays available.

Runs the application's own startup path (babelfishr.ui.launch) with an
event filter recording which top-level window is shown first. Isolated
temporary data only; the assistant's modal loop is stood in for, so no
preparation (and no network) ever starts.
"""

from __future__ import annotations

import os
import types

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def profile(tmp_path):
    """A brand-new profile in a temporary folder."""
    cfg = Config()
    cfg.database = str(tmp_path / "db.sqlite3")
    cfg.recording.directory = str(tmp_path / "rec")
    cfg.app_home = str(tmp_path / "home")
    cfg.asr.engine = cfg.translate.engine = "mock"
    cfg.source_path = str(tmp_path / "settings.toml")
    return cfg


class ShowOrder:
    """Records each top-level window as it is first shown."""

    def __init__(self, qt_app):
        from PySide6 import QtCore

        class Filter(QtCore.QObject):
            def __init__(self, seen):
                super().__init__()
                self.seen = seen

            def eventFilter(self, obj, event):  # noqa: N802
                if (event.type() == QtCore.QEvent.Show and hasattr(obj, "isWindow")
                        and obj.isWindow() and type(obj).__name__ in (
                            "MainWindow", "SetupAssistant")):
                    self.seen.append(type(obj).__name__)
                return False

        self.seen = []
        self.filter = Filter(self.seen)
        qt_app.installEventFilter(self.filter)
        self.qt_app = qt_app

    def close(self):
        self.qt_app.removeEventFilter(self.filter)


def _start(qt_app, cfg, monkeypatch, operator):
    """Launch through babelfishr.ui.launch; *operator* acts in the assistant."""
    from PySide6 import QtWidgets

    from babelfishr.ui import launch
    from babelfishr.ui.setup_assistant import SetupAssistant

    seen_in_assistant = {}
    before = {id(w) for w in QtWidgets.QApplication.topLevelWidgets()}

    def exec_(self):
        self.show()                                   # what exec() does first
        qt_app.processEvents()
        seen_in_assistant["console_exists"] = any(
            type(w).__name__ == "MainWindow" and id(w) not in before
            for w in QtWidgets.QApplication.topLevelWidgets())
        seen_in_assistant["first_run"] = self.first_run
        seen_in_assistant["parent"] = self.parent()
        seen_in_assistant["worker"] = self._worker
        result = operator(self)
        self.hide()
        return result

    monkeypatch.setattr(SetupAssistant, "exec", exec_)
    order = ShowOrder(qt_app)
    app = BabelFishRApp(config=cfg)
    try:
        window = launch(qt_app, app)
        qt_app.processEvents()
    finally:
        order.close()
    return app, window, order.seen, seen_in_assistant


def _close(app, window):
    window.close()
    app.close()


def cancel(assistant):
    assistant._cancel()
    return 0


def record_only(assistant):
    assistant.record_only_button.click()
    return 1


def prepared(assistant):
    """Preparation finished and verified (the worker's result, stood in)."""
    result = types.SimpleNamespace(ok=True, summary=lambda: "prepared")
    readiness = types.SimpleNamespace(field_ready=True, can_record=True,
                                      summary=lambda: "ready")
    assistant._finished({"preparation": result, "readiness": readiness,
                         "asr_model": "small", "language_pairs": [("es", "en")]})
    return 1


def test_a_fresh_profile_is_online_setup_and_needs_setup(profile):
    assert profile.mode == "online-setup"
    assert profile.needs_first_run_setup


@pytest.mark.parametrize("operator", [cancel, record_only, prepared],
                         ids=["cancel", "record-only", "prepared"])
def test_setup_is_the_first_window_and_the_console_never_flashes(qt_app, profile,
                                                                monkeypatch, operator):
    app, window, seen, inside = _start(qt_app, profile, monkeypatch, operator)
    try:
        assert seen[0] == "SetupAssistant", seen
        assert seen.count("MainWindow") == 1 and seen.index("MainWindow") > 0
        assert inside["console_exists"] is False, "the console was built first"
        assert inside["first_run"] is True and inside["parent"] is None
        assert inside["worker"] is None, "preparation started by itself"
        assert window.isVisible()
        assert app.capture is None and app.session is None, "monitoring started by itself"
    finally:
        _close(app, window)


def test_cancelling_stays_online_setup_and_asks_again_next_time(qt_app, profile,
                                                                monkeypatch):
    app, window, _, _ = _start(qt_app, profile, monkeypatch, cancel)
    try:
        assert app.config.mode == "online-setup"
        assert app.config.needs_first_run_setup
    finally:
        _close(app, window)


def test_record_only_is_honoured(qt_app, profile, monkeypatch):
    app, window, _, _ = _start(qt_app, profile, monkeypatch, record_only)
    try:
        assert app.config.mode == "record-only"
        assert app.config.setup.completed and app.config.setup.record_only_acknowledged
        assert Config.load(profile.source_path).mode == "record-only"
    finally:
        _close(app, window)


def test_completing_setup_on_first_launch_keeps_online_setup(qt_app, profile,
                                                             monkeypatch):
    app, window, _, _ = _start(qt_app, profile, monkeypatch, prepared)
    try:
        assert app.config.setup.completed
        assert app.config.mode == "online-setup", "switched mode without being asked"
        saved = Config.load(profile.source_path)
        assert saved.mode == "online-setup" and saved.setup.completed
    finally:
        _close(app, window)


@pytest.mark.parametrize("mode", ["field-offline", "online-setup", "record-only"])
def test_a_returning_profile_keeps_its_mode_and_goes_straight_to_the_console(
        qt_app, profile, monkeypatch, mode):
    import babelfishr.ui as ui

    profile.mode = mode
    profile.setup.completed = True
    profile.save()
    monkeypatch.setattr(ui, "_has_usable_assets", lambda config: True)
    asked = []
    app, window, seen, _ = _start(qt_app, Config.load(profile.source_path), monkeypatch,
                                  lambda a: asked.append(a) or 0)
    try:
        assert asked == [] and seen == ["MainWindow"]
        assert app.config.mode == mode and app.config.setup.completed
    finally:
        _close(app, window)


def test_the_setup_assistant_stays_in_the_tools_menu(qt_app, profile, monkeypatch):
    app, window, _, _ = _start(qt_app, profile, monkeypatch, cancel)
    try:
        bar = window.menuBar()
        tools = next(a.menu() for a in bar.actions() if a.text().replace("&", "") == "Tools")
        labels = [a.text().replace("&", "") for a in tools.actions()]
        assert any("Setup assistant" in label or "Set up" in label for label in labels), labels
    finally:
        _close(app, window)


def test_the_assistant_opened_later_still_switches_to_field_offline_when_ready(
        qt_app, profile):
    """Tools > Setup assistant (not the first launch) keeps its behaviour."""
    from babelfishr.ui.setup_assistant import SetupAssistant

    profile.setup.completed = True
    app = BabelFishRApp(config=profile)
    try:
        assistant = SetupAssistant(app)
        assert assistant.first_run is False
        prepared(assistant)
        assert app.config.mode == "field-offline"
    finally:
        app.close()
