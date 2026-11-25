"""Session Options grows downwards, never sideways.

The operator: "don't allow it for sideways scrolling; if there's more
information, have it wrap downwards." The horizontal scroll bar is always
off and its range is zero because the form genuinely fits: labels move above
their fields at narrow widths, long text wraps, and no long item (a profile
name, a language, a region) sets the form's minimum width. Every visible
control's right edge is inside the viewport at 360, 430 and normal widths;
everything is reachable by vertical scrolling and the keyboard; Close stays
on the dialog. Synthetic settings only.
"""

from __future__ import annotations

import os

import pytest
from PySide6 import QtCore, QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.models import RadioProfile

WIDTHS = (360, 430, 780)
LONG = ("Coastal emergency coordination net, secondary repeater, northern "
        "sector (temporary assignment for the regional exercise)")


@pytest.fixture(scope="module")
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, n=6):
    for _ in range(n):
        qt_app.processEvents()


@pytest.fixture
def opened(qt_app, config, store):
    from babelfishr.ui.main_window import MainWindow

    config.display.timezone = "utc"
    config.display.utc_zone = "America/Argentina/ComodRivadavia" \
        if "America/Argentina/ComodRivadavia" in [k for _, k in __import__(
            "babelfishr.timefmt", fromlist=["choices"]).choices()] \
        else "America/Indiana/Indianapolis"
    store.save_profile(RadioProfile(name=LONG, channel_name=LONG))
    app = BabelFishRApp(config=config, store=store)
    window = MainWindow(app)
    window._refresh_profiles()
    index = window.profile_box.findText(LONG, QtCore.Qt.MatchStartsWith)
    assert index > 0
    window.profile_box.setCurrentIndex(index)
    window.channel_label.setText(LONG + " " + LONG)          # a long explanatory line
    window.resize(1100, 760)
    window.show()
    window.session_options_button.click()
    pump(qt_app)
    yield window
    window.session_options_dialog.close()
    window.close()
    app.close()


def _form_widgets(window):
    dialog = window.session_options_dialog
    for widget in window.setup_box.findChildren(QtWidgets.QWidget):
        if not widget.isVisible() or widget.window() is not dialog:
            continue
        if isinstance(widget.parent(), (QtWidgets.QComboBox, QtWidgets.QAbstractSpinBox)):
            continue                                      # inner line edits, buttons
        yield widget


@pytest.mark.parametrize("width", WIDTHS)
def test_everything_fits_the_width_with_no_sideways_scrolling(qt_app, opened, width):
    dialog = opened.session_options_dialog
    dialog.resize(width, 520)
    pump(qt_app)
    scroll = dialog.scroll
    assert scroll.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
    assert scroll.horizontalScrollBar().maximum() == 0
    viewport = scroll.viewport()
    assert opened.setup_box.minimumSizeHint().width() <= viewport.width()
    checked = 0
    for widget in _form_widgets(opened):
        right = widget.mapTo(viewport, QtCore.QPoint(widget.width(), 0)).x()
        assert right <= viewport.width() + 1, (type(widget).__name__,
                                               widget.accessibleName(), right,
                                               viewport.width())
        checked += 1
    for named in (opened.time_zone_box, opened.utc_zone_box, opened.profile_box,
                  opened.source_mode_box, opened.target_language_box,
                  opened.quiet_seconds_box):
        assert named.isVisible() and named.width() >= 60, named.accessibleName()
    assert checked > 10
    # Close is on the dialog, not pushed off it.
    close = dialog.close_button
    assert close.isVisible()
    assert close.mapTo(dialog, QtCore.QPoint(close.width(), 0)).x() <= dialog.width()


def test_long_text_wraps_down_and_long_items_do_not_set_the_width(qt_app, opened):
    dialog = opened.session_options_dialog
    dialog.resize(360, 520)
    pump(qt_app)
    line = opened.channel_label
    assert line.wordWrap() and line.height() > 2 * line.fontMetrics().height()
    for box in (opened.profile_box, opened.utc_zone_box, opened.target_language_box):
        assert box.sizeAdjustPolicy() == \
            QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon
        assert box.minimumSizeHint().width() < 240
    # The full long choice is still readable: the tooltip says it.
    assert LONG in opened.profile_box.toolTip()
    assert opened.utc_zone_box.currentText() in opened.utc_zone_box.toolTip()


def test_a_short_window_scrolls_down_to_everything(qt_app, opened):
    dialog = opened.session_options_dialog
    dialog.resize(360, 260)
    pump(qt_app)
    scroll = dialog.scroll
    assert scroll.verticalScrollBar().maximum() > 0
    scroll.ensureWidgetVisible(opened.quiet_seconds_box)
    pump(qt_app)
    box = opened.quiet_seconds_box
    top = box.mapTo(scroll.viewport(), QtCore.QPoint(0, 0)).y()
    assert 0 <= top <= scroll.viewport().height() - box.height() + 1
    assert scroll.horizontalScrollBar().maximum() == 0


def test_keyboard_reaches_every_control_in_order(qt_app, opened):
    dialog = opened.session_options_dialog
    dialog.resize(360, 300)
    pump(qt_app)
    opened.time_zone_box.setFocus()
    seen = []
    widget = opened.time_zone_box
    for _ in range(40):
        seen.append(widget)
        widget = widget.nextInFocusChain()
        if widget is opened.time_zone_box:
            break
    for control in (opened.utc_zone_box, opened.profile_box, opened.source_mode_box,
                    opened.target_language_box, opened.quiet_seconds_box):
        assert control in seen or control.lineEdit() in seen if isinstance(
            control, QtWidgets.QComboBox) and control.isEditable() else control in seen


def test_resizing_keeps_values_focus_and_the_utc_choice(qt_app, opened):
    dialog = opened.session_options_dialog
    opened.quiet_seconds_box.setValue(1.0)
    opened.target_language_box.setCurrentIndex(3)
    zone = opened.utc_zone_box.currentData()
    opened.quiet_seconds_box.setFocus()
    for width in (780, 360, 430, 780):
        dialog.resize(width, 400)
        pump(qt_app)
    assert opened.quiet_seconds_box.value() == 1.0
    assert opened.target_language_box.currentIndex() == 3
    assert opened.utc_zone_box.currentData() == zone
    assert opened.app.config.display.utc_zone == zone
