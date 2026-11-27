"""No readiness indicator stays on "Checking".

Reproduced from the field test: in Online / Setup (and Record Only) a
model, input or language change runs a quick check without the smoke
tests; when it finished with everything installed but untested, the
sidebar badge said "... Checking" - and stayed so, since nothing else was
coming. A finished check now always settles to its result. A check that
fails, is cancelled or stalls (no answer within its limit) says it did not
finish and can be started again by a click; a late answer from an older
check never replaces the current one - in the window and in the Field
Readiness dialog. Checks are stood in for; nothing here uses the network.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.modes import OperatingMode
from babelfishr.readiness import Check, CheckStatus, ReadinessReport
from test_offline_indicator import Jobs
from test_offline_readiness import FAIL, ready_report


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, seconds=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)


def untested_report(mode=OperatingMode.ONLINE_SETUP):
    report = ReadinessReport(mode=mode)
    for name, status in [("Audio backend", "pass"), ("Recording directory writable", "pass"),
                         ("Local ASR model present", "pass"),
                         ("Local transcription smoke test", "skip"),
                         ("Installed translation paths", "pass"),
                         ("Local translation smoke test", "skip")]:
        report.add(Check(name, CheckStatus(status),
                         "smoke tests disabled" if status == "skip" else ""))
    return report


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    jobs = Jobs(monkeypatch)
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.jobs = jobs
    yield win
    win.close()


def test_a_finished_quick_check_settles_instead_of_checking(qt_app, window):
    window._refresh_readiness(run_smoke_tests=False)        # a model/input/language change
    assert "Checking" in window.ready_badge.text()          # while it runs
    window.jobs.finish(-1, untested_report())
    assert "Checking" not in window.ready_badge.text()
    assert window.ready_badge.text() == "◑ Installed, not tested"
    assert "not test-run" in window.ready_badge.accessibleDescription()


@pytest.mark.parametrize("how", ["failed", "cancelled", "stalled"])
def test_an_unfinished_check_says_so_and_can_be_run_again(qt_app, window, monkeypatch, how):
    monkeypatch.setattr(type(window), "READINESS_QUICK_LIMIT_S", 0.05)
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)   # runs the smoke tests
    monkeypatch.setattr(type(window), "READINESS_LIMIT_S", 0.05)
    window._refresh_readiness()
    call = window.jobs.calls[-1]
    if how == "failed":
        call["fail"]("the model loader crashed")
    elif how == "cancelled":
        call["kwargs"]["on_cancelled"]()
    else:
        pump(qt_app, 0.3)                                    # nothing ever reports
    assert window.ready_badge.text() == "⚠ Check did not finish"
    assert window.offline_status.property("state") == "fail"
    assert {"failed": "could not finish", "cancelled": "cancelled",
            "stalled": "stalled"}[how] in window.offline_status.toolTip()
    # Another attempt: a new check, which settles to its own result.
    window._refresh_readiness()
    assert window.offline_status.property("state") == "checking"
    window.jobs.finish(-1, ready_report())
    assert window.offline_status.property("state") == "ok"


def test_a_late_answer_after_a_stall_still_counts_for_the_current_check(qt_app, window,
                                                                        monkeypatch):
    monkeypatch.setattr(type(window), "READINESS_LIMIT_S", 0.05)
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    pump(qt_app, 0.3)
    assert window.offline_status.property("state") == "fail"
    window.jobs.finish(-1, ready_report())                  # it did finish, late
    assert window.offline_status.property("state") == "ok"


def test_an_older_check_never_overwrites_the_newer_one(qt_app, window):
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    older = len(window.jobs.calls) - 1
    window._refresh_readiness()
    newer = len(window.jobs.calls) - 1
    window.jobs.finish(newer, ready_report(**{"Audio backend": (FAIL, "none", {})}))
    window.jobs.finish(older, ready_report())
    window.jobs.calls[older]["fail"]("late failure")
    window.jobs.calls[older]["kwargs"]["on_cancelled"]()
    assert window.offline_status.property("state") == "fail"
    assert "Audio backend" in window.offline_status.toolTip()


def test_switching_setup_and_offline_never_leaves_checking(qt_app, window):
    for mode in (OperatingMode.FIELD_OFFLINE, OperatingMode.ONLINE_SETUP,
                 OperatingMode.FIELD_OFFLINE):
        window._apply_mode(mode.value)
    window.jobs.finish(-1, ready_report())
    for index in range(len(window.jobs.calls) - 1):          # late, older ones
        window.jobs.finish(index, untested_report())
    assert window.offline_status.property("state") == "ok"
    assert "Checking" not in window.ready_badge.text()


# --- the Field Readiness dialog -----------------------------------------------------

def _dialog(window, monkeypatch):
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    return ReadinessDialog(window.app, window)


def test_the_dialog_ignores_an_older_check_and_settles(qt_app, window, monkeypatch):
    dialog = _dialog(window, monkeypatch)                   # quick check at opening
    opening = len(window.jobs.calls) - 1
    dialog.recheck_button.setEnabled(True)
    dialog.recheck_button.click()                           # Re-check (smoke tests)
    recheck = len(window.jobs.calls) - 1
    window.jobs.finish(recheck, ready_report(mode=OperatingMode.ONLINE_SETUP))
    headline = dialog.headline.text()
    window.jobs.finish(opening, untested_report())          # the older one, late
    assert dialog.headline.text() == headline
    assert dialog.recheck_button.isEnabled()


@pytest.mark.parametrize("how", ["stalled", "cancelled", "failed"])
def test_the_dialog_never_stays_on_checking(qt_app, window, monkeypatch, how):
    import babelfishr.ui.readiness_dialog as module

    monkeypatch.setattr(module, "QUICK_STALL_LIMIT_S", 0.05)
    dialog = _dialog(window, monkeypatch)
    assert dialog.headline.text() == "Checking..."
    call = window.jobs.calls[-1]
    if how == "stalled":
        pump(qt_app, 0.3)
    elif how == "cancelled":
        call["kwargs"]["on_cancelled"]()
    else:
        call["fail"]("crashed")
    assert dialog.headline.text() == "Field check did not finish"
    assert dialog.recheck_button.isEnabled()
