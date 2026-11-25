"""All type on the teal status band is light orange, in every state.

The band's two backgrounds (dark and light appearance) are unchanged. The
mode button (with its arrow), the source line and the state text all render
in one light orange; states are told apart by their words and glyphs.
"""

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


def test_the_band_backgrounds_are_unchanged_and_the_orange_is_legible():
    from babelfishr.ui import theme

    assert theme.CONSOLE["band"] == "#287882" and theme.DAYLIGHT["band"] == "#1F6F78"
    for tokens in (theme.CONSOLE, theme.DAYLIGHT):
        colour = tokens["band_text"].lstrip("#")
        r, g, b = (int(colour[i:i + 2], 16) for i in (0, 2, 4))
        assert r == 255 and r - b >= 0x80 and g > b, "not a visible orange"
        assert _contrast(tokens["band_text"], tokens["band"]) >= 2.8
        # Legibility comes from the tight dark halo at every glyph edge.
        assert _contrast(tokens["band_text"], theme.BAND_HALO) >= 7.0


@pytest.mark.parametrize("dark", [True, False], ids=["dark", "light"])
def test_every_state_renders_orange_type_on_the_band(qt_app, config, store,
                                                     monkeypatch, dark):
    from PySide6 import QtGui

    from babelfishr.app import BabelFishRApp
    from babelfishr.ui import theme
    from babelfishr.ui.main_window import MainWindow

    monkeypatch.setattr(theme, "is_dark", lambda widget=None: dark)
    expected = QtGui.QColor((theme.CONSOLE if dark else theme.DAYLIGHT)["band_text"])
    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window._apply_theme()
    window.show()
    try:
        for state in (PipelineState.IDLE, PipelineState.LISTENING,
                      PipelineState.RECEIVING, PipelineState.TRANSCRIBING,
                      PipelineState.TRANSLATING, PipelineState.ERROR):
            window._set_state(state)
            qt_app.processEvents()
            for widget in (window.mode_button, window.source_label, window.state_badge):
                widget.ensurePolished()
                role = (QtGui.QPalette.ButtonText if widget is window.mode_button
                        else QtGui.QPalette.WindowText)
                got = widget.palette().color(role)
                assert got.name() == expected.name(), (state, widget.objectName(), got.name())
        assert "▾" in window.mode_button.text()       # the arrow is part of its text
        # Outside the band nothing turned orange.
        assert window.counts_label.palette().color(QtGui.QPalette.WindowText).name() \
            != expected.name()
    finally:
        window.close()
        app.close()
