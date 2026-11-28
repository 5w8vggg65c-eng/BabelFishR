"""Waiting for a window export, which runs off the GUI thread.

The main window's exports read, render and write on the thread pool and
report back through queued signals, so a test that triggers one turns the
event loop until the window says it has finished."""

from __future__ import annotations

import time


def finish_export(window, timeout: float = 30.0) -> None:
    """Turn the Qt event loop until *window* has no export running."""
    from PySide6 import QtWidgets

    qt_app = QtWidgets.QApplication.instance()
    deadline = time.monotonic() + timeout
    while window._export_job is not None:
        assert time.monotonic() < deadline, "the export did not finish"
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()
