"""Setup and Field Readiness look like the console.

Both real entry points: the Setup Assistant as at first launch (no console
window to inherit from) and Field Readiness opened from the console. Each
gets the console's palette and stylesheet - window background, surfaces,
teal accent, controls, status colours - in the dark and the light system
appearance, and follows a switch between them. Setup's choices scroll
instead of being squeezed or pushing the window off a small screen.
"""

from __future__ import annotations

import pytest
from PySide6 import QtGui, QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.readiness import CheckStatus
from babelfishr.ui import theme
from test_offline_readiness import FAIL, ready_report


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def set_appearance(qt_app, dark):
    palette = qt_app.palette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#1e1e1e" if dark else "#f0f0f0"))
    qt_app.setPalette(palette)
    qt_app.processEvents()


@pytest.fixture
def app(config, store, tmp_path):
    config.app_home = str(tmp_path / "home")
    application = BabelFishRApp(config=config, store=store)
    yield application


def background(widget):
    image = widget.grab().toImage()
    return QtGui.QColor(image.pixel(2, image.height() - 2)).name()


@pytest.mark.parametrize("dark", [True, False])
def test_setup_at_first_launch_uses_the_console_look(qt_app, app, dark):
    from babelfishr.ui.setup_assistant import SetupAssistant

    set_appearance(qt_app, dark)
    tokens = theme.tokens()
    setup = SetupAssistant(app)                      # no parent, as at first launch
    setup.resize(720, 600)
    setup.show()
    qt_app.processEvents()
    assert setup.parent() is None
    assert setup.styleSheet() == theme.stylesheet(setup)
    assert background(setup) == tokens["window"].lower()
    assert tokens["accent"] in setup.styleSheet()
    setup.close()


@pytest.mark.parametrize("dark", [True, False])
def test_readiness_uses_the_console_look_and_status_colours(qt_app, app, dark):
    from babelfishr.ui.readiness_dialog import ReadinessDialog

    set_appearance(qt_app, dark)
    tokens = theme.tokens()
    dialog = ReadinessDialog(app, report=ready_report(**{"Audio backend": (FAIL, "none", {})}))
    dialog.show()
    qt_app.processEvents()
    assert dialog.styleSheet() == theme.stylesheet(dialog)
    assert background(dialog) == tokens["window"].lower()
    rows = {dialog.tree.topLevelItem(i).text(0).split("  ", 1)[1]: dialog.tree.topLevelItem(i)
            for i in range(dialog.tree.topLevelItemCount())}
    assert rows["Audio backend"].foreground(0).color().name() == \
        theme.status_color("error").lower()
    assert rows["Audio input devices"].foreground(0).color().name() == \
        theme.status_color("ok").lower()
    dialog.close()


def test_a_switch_of_appearance_restyles_an_open_window(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    set_appearance(qt_app, True)
    setup = SetupAssistant(app)
    setup.show()
    dark_sheet = setup.styleSheet()
    set_appearance(qt_app, False)
    assert setup.styleSheet() != dark_sheet
    assert setup.styleSheet() == theme.stylesheet(setup)
    setup.close()
    set_appearance(qt_app, True)


def test_setup_choices_fit_a_small_window(qt_app, app):
    from babelfishr.ui.setup_assistant import SetupAssistant

    setup = SetupAssistant(app)
    setup.resize(640, 480)
    setup.show()
    qt_app.processEvents()
    assert setup.width() <= 660, "the content forced the window wider"
    page = setup.choices_scroll.widget()
    assert page.width() <= setup.choices_scroll.viewport().width()
    for check in setup.language_checks.values():          # nothing overlaps
        assert check.height() >= check.sizeHint().height() - 1
    setup.close()
