"""The Session's Save path in the window.

Session Options shows the path in effect, Choose folder... points the
selected Session at any writable folder (BabelFishR saves into its own
BabelFishR area there), Use standard location goes back. The row is
locked while that Session records. A folder that fails its check is
refused with the reason and nothing changes. Switching Session shows that
Session's own path and rechecks readiness; the Offline Field Use mark
checks the real destination and names the reason when it is missing; a
fallback during a watch is reported in the banner and clears any green.
Monitoring refuses to start on a missing destination. Exports open at the
Session's save path. Temporary folders stand in for a removable drive.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.modes import OperatingMode
from test_offline_indicator import Jobs
from test_offline_readiness import ready_report


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=15):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


@pytest.fixture
def drive(tmp_path):
    folder = tmp_path / "Volumes" / "FIELD"
    folder.mkdir(parents=True)
    return folder


@pytest.fixture
def wav(tmp_path):
    from babelfishr.testing import build_fixture

    return build_fixture([{"gap": 0.5}, {"kind": "voice", "duration": 1.0, "level_dbfs": -14},
                          {"gap": 0.6}], sample_rate=16_000).write(str(tmp_path / "one.wav"))


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.jobs = Jobs(monkeypatch)
    win.show()
    pump(qt_app)
    yield win
    if win.app.session is not None:
        win._stop_monitoring()
    win.app.wait_for_session_ends(10)
    deadline = time.monotonic() + 15
    while win.app.outstanding_work() and time.monotonic() < deadline:
        pump(qt_app)
    win.close()


def choose(monkeypatch, window, folder):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getExistingDirectory",
                        lambda *a, **k: str(folder))
    window.save_path_choose.click()


def test_choose_and_go_back_to_standard(qt_app, window, drive, monkeypatch):
    assert window.save_path_label.text().startswith("Standard location:")
    assert not window.save_path_reset.isEnabled()
    choose(monkeypatch, window, drive)
    info = window.app.save_path_info(window.app.conversation_id)
    assert info.is_custom and info.available
    assert str(drive) in window.save_path_label.text()
    assert str(info.owned_dir) in window.save_path_label.text()
    assert window.save_path_reset.isEnabled()
    window.save_path_reset.click()
    assert window.save_path_label.text().startswith("Standard location:")
    assert not window.app.save_path_info(window.app.conversation_id).is_custom


def test_a_folder_that_fails_its_check_is_refused_with_the_reason(qt_app, window,
                                                                  tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning",
                        lambda *a, **k: warned.append(a[2]))
    missing = tmp_path / "Volumes" / "EJECTED"
    choose(monkeypatch, window, missing)
    assert warned and "not found" in warned[0].lower()
    assert not window.app.save_path_info(window.app.conversation_id).is_custom
    assert not missing.exists(), "a phantom folder was created"


def test_switching_session_shows_its_own_path_and_rechecks(qt_app, window, drive,
                                                          monkeypatch):
    general = window.app.conversation_id
    choose(monkeypatch, window, drive)
    ops = window.app.create_conversation("Ops")
    window._refresh_session_tabs()
    calls = len(window.jobs.calls)
    index = next(i for i in range(window.session_tabs.count())
                 if window.session_tabs.tabData(i) == ops.id)
    window.session_tabs.setCurrentIndex(index)
    pump(qt_app)
    assert window.save_path_label.text().startswith("Standard location:")
    assert len(window.jobs.calls) > calls, "readiness was not rechecked"
    back = next(i for i in range(window.session_tabs.count())
                if window.session_tabs.tabData(i) == general)
    window.session_tabs.setCurrentIndex(back)
    pump(qt_app)
    assert str(drive) in window.save_path_label.text()


def test_the_offline_mark_checks_the_real_destination(qt_app, window, drive, monkeypatch):
    choose(monkeypatch, window, drive)
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    drive.rename(drive.with_name("FIELD-gone"))           # the drive was ejected
    report = window.app.readiness(run_smoke_tests=False)
    check = report.get("Recording directory writable")
    assert check.status.value == "fail"
    assert str(drive) in check.detail and "not found" in check.detail.lower()
    window.jobs.finish(-1, report)
    assert window.offline_status.property("state") == "fail"
    assert "not found" in window.offline_status.toolTip().lower()
    assert window.save_path_label.text().startswith("✕")


def test_a_fallback_during_a_watch_is_reported_and_clears_a_green(qt_app, window, drive,
                                                                  monkeypatch):
    choose(monkeypatch, window, drive)
    window._apply_mode(OperatingMode.FIELD_OFFLINE.value)
    window.jobs.finish(-1, ready_report())
    assert window.offline_status.property("state") == "ok"
    local = window.app.config.recording.directory
    window.app.events.publish("save-path", {
        "state": "fallback", "path": local, "reason": "not found",
        "message": f"The save path {drive}/BabelFishR could not be written (not found). "
                   f"New recordings are being saved in {local} instead."})
    window._drain_events()
    assert window.offline_status.property("state") in ("fail", "checking")
    assert window.offline_status.property("state") != "ok"
    assert local in window.warning_banner.text() and window.warning_banner.isVisible()


def test_monitoring_refuses_a_missing_destination(qt_app, window, drive, wav, monkeypatch):
    choose(monkeypatch, window, drive)
    drive.rename(drive.with_name("FIELD-gone"))
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "critical",
                        lambda *a, **k: shown.append(a[2]))
    window._start_monitoring(replay_path=wav)
    assert window.app.session is None
    assert shown and "not found" in shown[0].lower()
    assert not drive.exists(), "a phantom folder was created"


def test_locked_while_recording_this_session(qt_app, window, drive, wav, monkeypatch):
    choose(monkeypatch, window, drive)
    window._start_monitoring(replay_path=wav)
    assert window.app.session is not None
    assert not window.save_path_choose.isEnabled() and not window.save_path_reset.isEnabled()
    assert "Stop" in window.save_path_choose.toolTip()
    window._stop_monitoring()
    assert window.save_path_choose.isEnabled()


def test_exports_open_at_the_sessions_save_path(qt_app, window, drive, monkeypatch):
    from babelfishr.models import Session

    choose(monkeypatch, window, drive)
    window.app.store.save_session(Session(conversation_id=window.app.conversation_id))
    offered = []
    monkeypatch.setattr(QtWidgets.QFileDialog, "getSaveFileName",
                        lambda *a, **k: offered.append(a[2]) or ("", ""))
    window._export_text("md")
    info = window.app.save_path_info(window.app.conversation_id)
    assert offered and offered[0].startswith(str(window.app.default_export_dir(
        window.app.conversation_id)))
    assert str(info.owned_dir) in offered[0]


def test_the_choice_survives_a_relaunch(qt_app, window, drive, monkeypatch, config):
    choose(monkeypatch, window, drive)
    conversation = window.app.conversation_id
    window.close()
    from babelfishr.storage import Store
    from babelfishr.ui.main_window import MainWindow

    store = Store(config.database, recordings_dir=config.recording.directory)
    again = MainWindow(BabelFishRApp(config=Config.load(config.source_path), store=store))
    try:
        again.app.select_conversation(conversation)
        again._refresh_save_path_row()
        assert str(drive) in again.save_path_label.text()
    finally:
        again.close()
        store.close()
