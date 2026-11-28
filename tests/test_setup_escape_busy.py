"""Escape (or the window's close box) while Setup is preparing.

QDialog maps Escape straight to reject(), which bypassed the assistant's own
Cancel handling: the dialog disappeared while the download carried on, and
when that hidden run finished it still persisted the setup and switched the
mode to Offline Field Use behind the operator's back. Escape and close now
mean "Cancel": the run is asked to stop, the dialog stays open and says so,
and it can be dismissed once the run has settled. A result that arrives
after the dialog has been dismissed changes no mode.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402

from babelfishr.ui import setup_assistant  # noqa: E402
from babelfishr.app import BabelFishRApp  # noqa: E402
from babelfishr.config import Config  # noqa: E402
from babelfishr.preparation import ASR_STEP, PreparationResult  # noqa: E402
from babelfishr.readiness import Check, CheckStatus, ReadinessReport  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.app_home = str(tmp_path)
    cfg.database = str(tmp_path / "test.sqlite3")
    cfg.recording.directory = str(tmp_path / "recordings")
    cfg.asr.engine = "mock"
    cfg.translate.engine = "mock"
    cfg.source_path = str(tmp_path / "settings.toml")
    return cfg


@pytest.fixture
def app(config):
    application = BabelFishRApp(config=config)
    yield application
    application.close()


class HeldWorker:
    """Stands in for the background preparation: it runs until the test
    delivers a result, and records a cancel request."""

    def __init__(self, callbacks):
        self.callbacks = callbacks
        self.cancel_requests = 0

    def cancel(self):
        self.cancel_requests += 1


@pytest.fixture
def held(monkeypatch):
    workers = []

    def fake_run(job, *args, **callbacks):
        worker = HeldWorker(callbacks)
        workers.append(worker)
        return worker

    monkeypatch.setattr(setup_assistant, "run_in_background", fake_run)
    return workers


def full_success():
    result = PreparationResult()
    result.add(ASR_STEP, True, "tiny ok")
    result.add("Language pack es->en", True, "installed")
    ready = ReadinessReport()
    for name in ("Audio backend", "Recording directory writable",
                 "Local transcription smoke test", "Local translation smoke test"):
        ready.add(Check(name, CheckStatus.PASS, ""))
    return {"preparation": result, "readiness": ready, "asr_model": "tiny",
            "language_pairs": [("es", "en")]}


def busy_dialog(qt_app, app, held):
    dialog = setup_assistant.SetupAssistant(app)
    dialog.show()
    QTest.qWaitForWindowExposed(dialog) if hasattr(QTest, "qWaitForWindowExposed") else None
    dialog._start()
    assert held and dialog._worker is held[-1]
    return dialog


def test_escape_while_preparing_cancels_and_keeps_the_dialog_open(qt_app, app,
                                                                   held):
    dialog = busy_dialog(qt_app, app, held)
    QTest.keyClick(dialog, QtCore.Qt.Key_Escape)
    qt_app.processEvents()
    assert dialog.isVisible()
    assert held[-1].cancel_requests == 1
    assert "Cancelling" in dialog.log_view.toPlainText()
    # The run settles as cancelled; now Escape dismisses the dialog.
    held[-1].callbacks["on_cancelled"]()
    QTest.keyClick(dialog, QtCore.Qt.Key_Escape)
    qt_app.processEvents()
    assert not dialog.isVisible()
    assert dialog.result() == QtWidgets.QDialog.Rejected


def test_a_run_that_finishes_after_escape_changes_no_mode_hidden(qt_app, app,
                                                                 config, held):
    dialog = busy_dialog(qt_app, app, held)
    QTest.keyClick(dialog, QtCore.Qt.Key_Escape)
    qt_app.processEvents()
    # The job got past its last cancellation point and finished anyway:
    # the dialog is still open and shows it; the mode change happens in
    # view, not behind a dismissed window.
    held[-1].callbacks["on_finished"](full_success())
    assert dialog.isVisible()
    assert "Ready for offline field use" in dialog.status_label.text()


def test_the_close_box_while_preparing_also_means_cancel(qt_app, app, held):
    dialog = busy_dialog(qt_app, app, held)
    dialog.close()
    qt_app.processEvents()
    assert dialog.isVisible()
    assert held[-1].cancel_requests == 1
    held[-1].callbacks["on_cancelled"]()
    dialog.close()
    qt_app.processEvents()
    assert not dialog.isVisible()


def test_a_result_after_the_dialog_was_dismissed_changes_no_mode(qt_app, app,
                                                                 config, held):
    """Defensive: should a result still arrive for a dismissed dialog (its
    parent closed it), it must not switch the mode or persist setup."""
    dialog = busy_dialog(qt_app, app, held)
    before_mode, before_done = config.mode, config.setup.completed
    dialog.done(QtWidgets.QDialog.Rejected)        # dismissed by its owner
    held[-1].callbacks["on_finished"](full_success())
    assert config.mode == before_mode
    assert config.setup.completed == before_done
    assert held[-1].cancel_requests >= 1
