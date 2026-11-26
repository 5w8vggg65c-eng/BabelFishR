"""White status-band type and independently coloured capture/processing badges."""

from __future__ import annotations

import os

import pytest

from babelfishr.pipeline import PipelineState


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _contrast(a: str, b: str) -> float:
    def lum(h):
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_white_type_is_legible_on_the_band_and_each_badge():
    from babelfishr.ui import theme

    assert theme.CONSOLE["band"] == "#287882" and theme.DAYLIGHT["band"] == "#1F6F78"
    for tokens in (theme.CONSOLE, theme.DAYLIGHT):
        assert tokens["on_band"] == "#FFFFFF"
        for fill in (tokens["band"], *theme.BADGE_FILL.values()):
            assert _contrast(tokens["on_band"], fill) >= 4.5


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_state_changes_recolour_only_the_badge(qt_app, config, store, monkeypatch, dark):
    from PySide6 import QtGui

    from babelfishr.app import BabelFishRApp
    from babelfishr.ui import theme
    from babelfishr.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "is_dark", lambda widget=None: dark)
    expected = QtGui.QColor("white")
    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window._apply_theme()
    window.show()
    window._timer.stop()
    try:
        for state, colour in [(PipelineState.IDLE, "green"),
                              (PipelineState.LISTENING, "red"),
                              (PipelineState.RECEIVING, "red"),
                              (PipelineState.TRANSCRIBING, "blue"),
                              (PipelineState.TRANSLATING, "blue")]:
            # State reconciliation is exercised by the live capture tests.
            # Render each possible reconciled state on the actual widget.
            window._state = state
            window._refresh_state_badge()
            qt_app.processEvents()
            for widget in (window.mode_button, window.source_label, window.state_badge):
                widget.ensurePolished()
                role = (QtGui.QPalette.ButtonText if widget is window.mode_button
                        else QtGui.QPalette.WindowText)
                got = widget.palette().color(role)
                assert got.name() == expected.name(), (state, widget.objectName(), got.name())
            fill = window.state_badge.palette().color(QtGui.QPalette.Window)
            channels = {"red": fill.red(), "green": fill.green(), "blue": fill.blue()}
            assert max(channels, key=channels.get) == colour
        assert "▾" in window.mode_button.text()       # the arrow is part of its text
    finally:
        window.close()
        app.close()
