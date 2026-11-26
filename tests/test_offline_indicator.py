"""The readiness mark beside the operating mode (Offline Field Use).

Selecting Offline Field Use shows "Checking..." at once, then a red cross
naming every required check that is not a pass, or a green tick only when
all of them passed for the current configuration. The check runs off the
GUI thread; a result for an earlier request is ignored, a failed check is
a cross, and any change (mode, model, input) puts it back to "Checking..."
so a tick is never stale. The mark opens the full report by mouse or
keyboard. Other modes hide it. Opening the app in Offline Field Use makes
no network connection.

The worker is stood in for so results can be delivered in any order; one
test runs the real worker. Temporary data only.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from PySide6 import QtCore, QtWidgets
from PySide6.QtTest import QTest

from babelfishr.app import BabelFishRApp
from babelfishr.modes import OperatingMode
from test_offline_readiness import FAIL, SKIP, ready_report

CHECKING = "Checking…"


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=10):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


class Jobs:
    """Stands in for run_in_background: holds each readiness request."""

    def __init__(self, monkeypatch):
        import babelfishr.ui.workers as workers

        self.calls = []
        self._real = workers.run_in_background
        monkeypatch.setattr(workers, "run_in_background", self._start)

    def _start(self, function, *args, on_finished=None, on_failed=None, **kwargs):
        if getattr(function, "__name__", "") != "readiness_job":
            return self._real(function, *args, on_finished=on_finished,
                              on_failed=on_failed, **kwargs)
        self.calls.append({"args": args, "kwargs": kwargs, "finish": on_finished,
                           "fail": on_failed})
        return object()

    def finish(self, index, report):
        self.calls[index]["finish"](report)

    def fail(self, index, message):
        self.calls[index]["fail"](message)


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    jobs = Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.show()
    pump(qt_app)
    win.jobs = jobs
    yield win
    win.close()


def _select_offline(window):
    window.mode_actions[OperatingMode.FIELD_OFFLINE.value].trigger()


def _state(window):
    mark = window.offline_status
    return (mark.isVisibleTo(window), mark.property("state"), mark.text())


def test_selecting_offline_field_use_checks_then_ticks(qt_app, window):
    assert not window.offline_status.isVisibleTo(window), "shown in Online / Setup"
    before = len(window.jobs.calls)
    _select_offline(window)
    assert window.app.mode is OperatingMode.FIELD_OFFLINE
    assert _state(window) == (True, "checking", CHECKING)
    assert window.offline_status.accessibleName().endswith("checking")
    # It sits immediately to the right of the mode control.
    row = window.mode_button.parentWidget().layout()
    assert row.indexOf(window.offline_status) == row.indexOf(window.mode_button) + 1
    call = window.jobs.calls[before]
    assert call["args"][1] is True, "Offline Field Use must run the smoke tests"
    assert "microphone_permission" in call["kwargs"]
    window.jobs.finish(before, ready_report())
    assert _state(window) == (True, "ok", "✓")
    assert window.offline_status.accessibleName() == "Offline Field Use readiness: ready"


def test_a_failure_is_a_red_cross_that_names_each_problem(qt_app, window):
    _select_offline(window)
    report = ready_report(**{"Local ASR model present": (FAIL, "no model", {}),
                             "Local translation smoke test": (SKIP, "smoke tests disabled", {})})
    window.jobs.finish(-1, report)
    visible, state, text = _state(window)
    assert (visible, state, text) == (True, "fail", "✕")
    assert window.offline_status.accessibleName() == "Offline Field Use readiness: not ready"
    tip = window.offline_status.toolTip()
    assert "Local ASR model present: failed (no model)" in tip
    assert "Local translation smoke test: not verified" in tip


def test_a_check_that_cannot_finish_is_a_cross_and_reselecting_recovers(qt_app, window):
    _select_offline(window)
    window.jobs.fail(-1, "the model loader crashed")
    assert _state(window)[1] == "fail"
    assert "could not finish" in window.offline_status.toolTip()
    window._apply_mode(OperatingMode.ONLINE_SETUP.value)
    assert not window.offline_status.isVisibleTo(window)
    _select_offline(window)
    assert _state(window)[1] == "checking"
    window.jobs.finish(-1, ready_report())
    assert _state(window)[1] == "ok"


def test_results_out_of_order_never_show_an_earlier_answer(qt_app, window):
    _select_offline(window)
    first = len(window.jobs.calls) - 1
    window._refresh_readiness()                     # a settings change meanwhile
    second = len(window.jobs.calls) - 1
    window.jobs.finish(first, ready_report())       # the older, green answer arrives late
    assert _state(window)[1] == "checking", "showed the superseded answer"
    window.jobs.finish(second, ready_report(**{"Audio backend": (FAIL, "none", {})}))
    assert _state(window)[1] == "fail"
    window.jobs.finish(first, ready_report())       # and again, after the current one
    assert _state(window)[1] == "fail"


def test_a_tick_never_outlives_a_change(qt_app, window):
    _select_offline(window)
    window.jobs.finish(-1, ready_report())
    assert _state(window)[1] == "ok"
    window._on_input_selection()                    # the input changed
    assert _state(window)[1] == "checking"
    window.jobs.finish(-1, ready_report())
    window._refresh_readiness(run_smoke_tests=False)  # what a model change does
    assert _state(window)[1] == "checking"
    assert window.jobs.calls[-1]["args"][1] is True, "a tick needs the smoke tests"
    window.jobs.finish(-1, ready_report())
    window._apply_mode(OperatingMode.RECORD_ONLY.value)
    assert not window.offline_status.isVisibleTo(window)
    _select_offline(window)
    assert _state(window)[1] == "checking", "an old tick came back with the mode"
    # An answer computed for another mode is not a tick for this one.
    window.jobs.finish(-1, ready_report(mode=OperatingMode.ONLINE_SETUP))
    assert _state(window)[1] == "fail"


def test_the_mark_opens_the_full_report_by_mouse_and_keyboard(qt_app, window,
                                                             monkeypatch):
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    opened = []
    monkeypatch.setattr(ReadinessDialog, "exec", lambda self: opened.append(self) or 0)
    _select_offline(window)
    window.jobs.finish(-1, ready_report(**{"Audio backend": (FAIL, "none", {})}))
    QTest.mouseClick(window.offline_status, QtCore.Qt.LeftButton)
    assert len(opened) == 1
    window.offline_status.setFocus()
    pump(qt_app)
    QTest.keyClick(window.offline_status, QtCore.Qt.Key_Space)
    pump(qt_app)
    assert len(opened) == 2
    assert _state(window)[1] == "checking", "closing the report did not recheck"


def test_the_check_runs_off_the_gui_thread(qt_app, config, store, tmp_path,
                                           monkeypatch):
    """The real worker: the window stays responsive while a slow check runs."""
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.FIELD_OFFLINE.value
    release, threads = threading.Event(), []

    def slow(self, run_smoke_tests=True, microphone_permission=None):
        threads.append(threading.current_thread() is threading.main_thread())
        release.wait(5)
        return ready_report()

    monkeypatch.setattr(BabelFishRApp, "readiness", slow)
    started = time.monotonic()
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        window.show()
        pump(qt_app)
        assert time.monotonic() - started < 4, "the check blocked the interface"
        assert _state(window)[1] == "checking"
        release.set()
        deadline = time.monotonic() + 10
        while _state(window)[1] == "checking" and time.monotonic() < deadline:
            pump(qt_app)
        assert _state(window)[1] == "ok" and threads and not any(threads)
    finally:
        release.set()
        window.close()


def test_opening_in_offline_field_use_connects_to_nothing(qt_app, config, store,
                                                          tmp_path, monkeypatch):
    """The real readiness check (no models installed): it finishes with a
    cross, and no socket ever tried to connect."""
    from babelfishr.ui.main_window import MainWindow

    attempts = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            attempts.append(address)
            raise OSError("network disabled for this test")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    monkeypatch.setattr(socket, "create_connection",
                        lambda *a, **k: attempts.append(a) or (_ for _ in ()).throw(
                            OSError("network disabled for this test")))
    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.FIELD_OFFLINE.value
    window = MainWindow(BabelFishRApp(config=config, store=store))
    try:
        window.show()
        deadline = time.monotonic() + 60
        while _state(window)[1] == "checking" and time.monotonic() < deadline:
            pump(qt_app)
        assert _state(window)[1] == "fail"
        assert attempts == [], f"network connections attempted: {attempts}"
    finally:
        window.close()


# --- the report it opens says the same as the mark ---------------------------------

def _open_dialog(window, monkeypatch):
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    opened = []
    monkeypatch.setattr(ReadinessDialog, "exec", lambda self: opened.append(self) or 0)
    window.offline_status.click()
    return opened[-1]


def test_a_green_mark_opens_the_same_green_report_without_a_new_check(qt_app, window,
                                                                       monkeypatch):
    _select_offline(window)
    window.jobs.finish(-1, ready_report())
    assert _state(window)[1] == "ok"
    before = len(window.jobs.calls)
    dialog = _open_dialog(window, monkeypatch)
    assert dialog.headline.text() == "✓ Ready for Offline Field Use"
    assert len(window.jobs.calls) == before + 1, \
        "the dialog started its own check (the +1 is the recheck after closing)"


def test_a_red_mark_opens_the_same_reasons(qt_app, window, monkeypatch):
    _select_offline(window)
    report = ready_report(**{"Local ASR model present": (FAIL, "no model", {})})
    window.jobs.finish(-1, report)
    tip = window.offline_status.toolTip()
    dialog = _open_dialog(window, monkeypatch)
    assert dialog.headline.text() == "✕ Not ready for Offline Field Use"
    assert "Local ASR model present: failed (no model)" in dialog.subtitle.text()
    assert "Local ASR model present: failed (no model)" in tip


def test_while_checking_the_dialog_checks_with_the_same_inputs(qt_app, window,
                                                               monkeypatch):
    _select_offline(window)                      # still Checking...
    before = len(window.jobs.calls)
    dialog = _open_dialog(window, monkeypatch)
    started = window.jobs.calls[before]
    assert "microphone_permission" in started["kwargs"], \
        "the dialog's check did not read macOS's microphone decision"
    started["finish"](ready_report())
    assert dialog.headline.text() == "✓ Ready for Offline Field Use"
    dialog.recheck_button.click()                # Re-check: the same inputs again
    assert "microphone_permission" in window.jobs.calls[-1]["kwargs"]
    assert window.jobs.calls[-1]["args"][1] is True


def test_a_late_result_after_the_app_closed_reads_nothing(qt_app, config, store, tmp_path,
                                                          monkeypatch):
    """The full suite crashed (segfault) when a readiness result reached a
    window whose application was closing its store on another thread: the
    Save path row read the shared connection unlocked. Now a late result is
    ignored once closing has begun, and a read after close raises instead
    of crashing."""
    import sqlite3

    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.FIELD_OFFLINE.value
    jobs = Jobs(monkeypatch)
    app = BabelFishRApp(config=config, store=store)
    win = MainWindow(app)
    pending = len(jobs.calls) - 1
    app.close()                                    # the store closes
    assert app.closing
    jobs.finish(pending, ready_report())           # a late result arrives
    assert win.offline_status.property("state") != "ok"
    win._refresh_save_path_row()                   # and a stray redraw
    with pytest.raises(sqlite3.ProgrammingError):
        store.get_conversation("anything")         # locked read: raises, no crash
