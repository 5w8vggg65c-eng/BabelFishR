"""The field-console theme: dark appearance gets the console palette, light
appearance keeps the system's, and the mode styling follows real app state."""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtGui, QtWidgets  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _set_system(dark: bool):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#1e1e1e" if dark else "#efefef"))
    palette.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#ffffff" if dark else "#000000"))
    QtWidgets.QApplication.setPalette(palette)


@pytest.fixture
def system_palette(qt_app):
    original = QtGui.QPalette(QtWidgets.QApplication.palette())
    yield _set_system
    QtWidgets.QApplication.setPalette(original)


def _luminance(hex_color: str) -> float:
    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    color = QtGui.QColor(hex_color)
    return (0.2126 * channel(color.red()) + 0.7152 * channel(color.green())
            + 0.0722 * channel(color.blue()))


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_console_text_meets_contrast_on_its_ground_and_panels():
    from babelfishr.ui.theme import CONSOLE, STATUS_DARK

    for ground in (CONSOLE["window"], CONSOLE["surface"]):
        assert _contrast(CONSOLE["text"], ground) >= 7.0
        assert _contrast(CONSOLE["text_muted"], ground) >= 4.5
        assert _contrast(CONSOLE["accent"], ground) >= 4.5
        for tone in ("working", "error", "ok"):
            assert _contrast(STATUS_DARK[tone], ground) >= 4.5, tone
    assert _contrast(CONSOLE["on_accent"], CONSOLE["accent"]) >= 4.5


def test_the_window_follows_the_system_appearance_in_and_out_of_the_console(
        qt_app, config, store, system_palette):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui import theme
    from babelfishr.ui.main_window import MainWindow

    system_palette(dark=True)
    window = MainWindow(BabelFishRApp(config=config, store=store))
    assert theme.is_dark(window)
    assert window.palette().color(QtGui.QPalette.Window).name().lower() == \
        theme.CONSOLE["window"].lower()
    assert window.header_band.objectName() == "headerBand"

    # The window's own console palette must not stop it noticing the system
    # going back to light.
    system_palette(dark=False)
    window._apply_theme()
    assert not theme.is_dark(window)
    assert window.palette().color(QtGui.QPalette.Window).name().lower() == "#efefef"
    window.close()


def test_the_mode_styling_is_the_mode_the_application_is_in(qt_app, config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    assert window.mode_button.property("mode") == app.mode.value
    assert app.mode.label in window.mode_button.text()
    window.close()


def test_primary_text_is_navy_on_cyan_and_disabled_controls_lose_their_colour(
        qt_app, system_palette):
    """Off-white on the cyan fill is only 2.3:1; navy is 7.6:1. And a disabled
    mode or Play button must not keep the colour that says it is live."""
    from babelfishr.ui.theme import CONSOLE

    assert CONSOLE["on_accent"] == CONSOLE["window"]
    assert _contrast(CONSOLE["on_accent"], CONSOLE["accent"]) >= 7.0
    assert _contrast(CONSOLE["text"], CONSOLE["accent"]) < 3.0   # why not off-white

    system_palette(dark=True)
    from babelfishr.ui import theme

    css = theme.stylesheet()
    assert "#modeButton:disabled, #playButton:disabled" in css
    assert "#primaryButton:disabled" in css
