"""First-run GUI setup workflow and background workers.

The previous assistant told the operator to open Terminal and run a command,
which is not a setup workflow for a double-clicked application. These tests
cover the real one: choices, in-GUI preparation on a worker thread, progress,
cancellation, Field Check afterwards, and a remembered Record Only choice.

Preparation itself is driven with a stubbed job, because no model weights can
be downloaded in this environment. The GUI plumbing is genuinely exercised;
a real download is not.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.app import BabelFishRApp  # noqa: E402
from babelfishr.config import Config  # noqa: E402
from babelfishr.modes import OperatingMode  # noqa: E402
from babelfishr.storage import Store  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    config = Config.load()
    return BabelFishRApp(config=config, store=Store(":memory:"))


def _pump(qt_app, predicate, timeout: float = 5.0) -> bool:
    """Spin the event loop until *predicate* holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ---- workers ------------------------------------------------------------
def test_worker_reports_result_and_messages(qt_app):
    from babelfishr.ui.workers import run_in_background

    seen, done = [], []

    def job(value, report=None, token=None):
        report("halfway")
        return value * 2

    run_in_background(job, 21, on_message=seen.append, on_finished=done.append)
    assert _pump(qt_app, lambda: done)
    assert done == [42] and seen == ["halfway"]


def test_worker_survives_without_a_caller_reference(qt_app):
    """An unreferenced worker must still deliver its result."""
    import gc

    from babelfishr.ui.workers import run_in_background

    done = []
    run_in_background(lambda report=None, token=None: "ok",
                      on_finished=done.append)
    gc.collect()
    assert _pump(qt_app, lambda: done), "the result was lost to garbage collection"


def test_worker_surfaces_failures(qt_app):
    from babelfishr.ui.workers import run_in_background

    errors = []

    def job(report=None, token=None):
        raise ValueError("boom")

    run_in_background(job, on_failed=errors.append)
    assert _pump(qt_app, lambda: errors)
    assert "boom" in errors[0]


def test_worker_is_cancellable(qt_app):
    from babelfishr.ui.workers import run_in_background

    cancelled, finished = [], []

    def job(report=None, token=None):
        for _ in range(500):
            token.raise_if_cancelled()
            time.sleep(0.005)
        return "should not finish"

    worker = run_in_background(job, on_cancelled=lambda: cancelled.append(True),
                               on_finished=finished.append)
    time.sleep(0.05)
    worker.cancel()
    assert _pump(qt_app, lambda: cancelled or finished)
    assert cancelled and not finished


def test_active_workers_are_released(qt_app):
    from babelfishr.ui.workers import active_worker_count, run_in_background

    done = []
    run_in_background(lambda report=None, token=None: 1, on_finished=done.append)
    assert _pump(qt_app, lambda: done)
    assert _pump(qt_app, lambda: active_worker_count() == 0)


# ---- the assistant ------------------------------------------------------
def test_assistant_offers_model_and_language_choices(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    assistant = SetupAssistant(app)
    assert assistant.model_box.count() >= 4
    assert assistant.selected_model()
    assert assistant.language_checks, "no language pairs offered"
    assert assistant.language_pairs(), "no pairs selected by default"


def test_assistant_shows_a_download_estimate(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    assistant = SetupAssistant(app)
    assert "MB" in assistant.size_label.text()
    assert str(app.config.paths().models.parent) in assistant.size_label.text()


def test_assistant_keeps_the_terminal_command_as_a_fallback(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    assistant = SetupAssistant(app)
    text = assistant.advanced.text()
    assert "prepare-field" in text
    assert "--asr-model" in text
    assert "Advanced" in text


def test_assistant_excludes_the_target_language_from_pairs(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    app.config.translate.target_language = "es"
    assistant = SetupAssistant(app)
    assert all(source != "es" for source, _ in assistant.language_pairs())


def test_assistant_runs_preparation_off_the_gui_thread(qt_app, app, monkeypatch):
    """The GUI must stay responsive; preparation must not block it."""
    import threading

    import babelfishr.ui.setup_assistant as module

    gui_thread = threading.current_thread().ident
    ran_on = {}

    def fake_job(config, model, pairs, report=None, token=None):
        ran_on["thread"] = threading.current_thread().ident
        report("downloading...")
        time.sleep(0.05)
        from babelfishr.preparation import PreparationResult
        from babelfishr.readiness import ReadinessReport

        result = PreparationResult()
        result.add("Local ASR model", True, "prepared")
        return {"preparation": result, "readiness": ReadinessReport()}

    monkeypatch.setattr(module, "prepare_field_job", fake_job)
    assistant = module.SetupAssistant(app)
    assistant._start()
    assert _pump(qt_app, lambda: assistant._worker is None)
    assert ran_on["thread"] != gui_thread, "preparation ran on the GUI thread"
    assert "downloading..." in assistant.log_view.toPlainText()


def test_assistant_reports_failure_without_losing_recording(qt_app, app,
                                                            monkeypatch):
    import babelfishr.ui.setup_assistant as module

    def failing(config, model, pairs, report=None, token=None):
        raise RuntimeError("network unreachable")

    monkeypatch.setattr(module, "prepare_field_job", failing)
    assistant = module.SetupAssistant(app)
    assistant._start()
    assert _pump(qt_app, lambda: "ERROR" in assistant.log_view.toPlainText())
    assert "failed" in assistant.status_label.text().lower()
    assert "Record only" in assistant.log_view.toPlainText()
    assert assistant.prepare_button.isEnabled(), "must allow another attempt"


def test_assistant_cancellation_is_offered_and_explained(qt_app, app,
                                                         monkeypatch):
    import babelfishr.ui.setup_assistant as module

    def slow(config, model, pairs, report=None, token=None):
        for _ in range(500):
            token.raise_if_cancelled()
            time.sleep(0.005)
        return {}

    monkeypatch.setattr(module, "prepare_field_job", slow)
    assistant = module.SetupAssistant(app)
    assistant._start()
    time.sleep(0.05)
    assistant._cancel()
    assert _pump(qt_app, lambda: "cancelled" in assistant.status_label.text().lower())
    assert "incomplete" in assistant.log_view.toPlainText()


def test_readiness_is_only_claimed_when_assets_really_load(qt_app, app,
                                                           monkeypatch):
    """Field readiness must never be asserted on a failed Field Check."""
    import babelfishr.ui.setup_assistant as module
    from babelfishr.preparation import PreparationResult
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    def job(config, model, pairs, report=None, token=None):
        result = PreparationResult()
        result.add("Local ASR model", True, "downloaded")
        report_obj = ReadinessReport()
        report_obj.add(Check("Audio backend", CheckStatus.PASS))
        report_obj.add(Check("Recording directory writable", CheckStatus.PASS))
        report_obj.add(Check("Local transcription smoke test", CheckStatus.FAIL))
        return {"preparation": result, "readiness": report_obj}

    monkeypatch.setattr(module, "prepare_field_job", job)
    assistant = module.SetupAssistant(app)
    assistant._start()
    assert _pump(qt_app, lambda: assistant.readiness is not None)
    assert not assistant.readiness.field_ready
    assert "Partly ready" in assistant.status_label.text()
    # A failed smoke test must not be recorded as a completed setup.
    assert app.config.operating_mode() is not OperatingMode.FIELD_OFFLINE


def test_successful_preparation_records_setup_and_mode(qt_app, app, monkeypatch):
    import babelfishr.ui.setup_assistant as module
    from babelfishr.preparation import PreparationResult
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    def job(config, model, pairs, report=None, token=None):
        result = PreparationResult()
        result.add("Local ASR model", True, "ready")
        ready = ReadinessReport()
        ready.add(Check("Audio backend", CheckStatus.PASS))
        ready.add(Check("Recording directory writable", CheckStatus.PASS))
        ready.add(Check("Local transcription smoke test", CheckStatus.PASS))
        ready.add(Check("Local translation smoke test", CheckStatus.PASS))
        return {"preparation": result, "readiness": ready}

    monkeypatch.setattr(module, "prepare_field_job", job)
    assistant = module.SetupAssistant(app)
    assistant._start()
    assert _pump(qt_app, lambda: assistant.readiness is not None)
    assert assistant.readiness.field_ready
    assert app.config.operating_mode() is OperatingMode.FIELD_OFFLINE

    reloaded = Config.load()
    assert reloaded.setup.completed
    assert not reloaded.needs_first_run_setup


def test_record_only_choice_is_saved(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    assistant = SetupAssistant(app)
    assistant._record_only()
    assert app.config.operating_mode() is OperatingMode.RECORD_ONLY

    reloaded = Config.load()
    assert reloaded.operating_mode() is OperatingMode.RECORD_ONLY
    assert reloaded.setup.record_only_acknowledged
    assert not reloaded.needs_first_run_setup


# ---- first-run triggering ----------------------------------------------
def test_first_run_is_detected_from_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    assert Config.load().needs_first_run_setup
    Config.load().record_setup(asr_model="tiny")
    assert not Config.load().needs_first_run_setup


def test_gui_entry_point_checks_for_first_run():
    """The assistant must appear by itself, not wait to be found in a menu."""
    source = (pathlib.Path(__file__).resolve().parent.parent / "babelfishr"
              / "ui" / "__init__.py").read_text(encoding="utf-8")
    assert "needs_first_run_setup" in source
    assert "SetupAssistant" in source


# ---- readiness dialog off the GUI thread -------------------------------
def test_readiness_dialog_checks_on_a_worker(qt_app, app):
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    dialog = ReadinessDialog(app)
    assert _pump(qt_app, lambda: dialog.tree.topLevelItemCount() > 0)
    assert dialog.headline.text()
    assert dialog.recheck_button.isEnabled()


def test_main_window_readiness_badge_updates_asynchronously(qt_app, app):
    from babelfishr.ui.main_window import MainWindow

    window = MainWindow(app)
    assert _pump(qt_app, lambda: window.ready_badge.text().strip() != "")
    window.close()
