"""Space plays and pauses - real key events, real focus, scoped to playback.

One Space press (with its release) toggles once: plays the selected
message and carries on through the following cards, pauses, resumes, and
also stops at the final card. Holding Space (auto-repeat) acts once.
When the last clip ends and focus has moved onto the playback bar's
Bookmark button, Space still never bookmarks. Typing a space in the
inline editor or the search field, and Space on an ordinary control
elsewhere, behave as usual. QTest sends the key events to the widget that
has focus; nothing calls the slot directly.

The transport buttons are always in view and bordered like the -5/+5 s
buttons; Play selected starts with a play triangle; CHECK offers
"Approve?" and an approved message says APPROVED instead.
"""

from __future__ import annotations

import pytest
from PySide6 import QtCore, QtWidgets
from PySide6.QtTest import QTest

from test_alpha5_playback import pump
from test_continuous_playback_window import Rig, qt_app  # noqa: F401

SPACE = QtCore.Qt.Key_Space


@pytest.fixture
def rig(qt_app, config, store, tmp_path):  # noqa: F811
    made = Rig(qt_app, config, store, tmp_path)
    made.window.activateWindow()
    pump(qt_app)
    yield made
    made.close()


def focus(rig, widget):
    rig.window.activateWindow()
    widget.setFocus(QtCore.Qt.OtherFocusReason)
    pump(rig.qt)
    assert QtWidgets.QApplication.focusWidget() is widget, "focus did not move"


def space(rig, widget=None, repeat=0):
    """A real press (and, held, the platform's auto-repeat events, as macOS
    sends them: flagged autorepeat) and the release, to the focus widget."""
    from PySide6 import QtGui

    target = widget or QtWidgets.QApplication.focusWidget()
    QTest.keyPress(target, SPACE)
    for _ in range(repeat):
        for kind in (QtCore.QEvent.KeyRelease, QtCore.QEvent.KeyPress):
            held = QtGui.QKeyEvent(kind, SPACE, QtCore.Qt.NoModifier, " ", True)
            QtWidgets.QApplication.sendEvent(
                QtWidgets.QApplication.focusWidget() or target, held)
    QTest.keyRelease(QtWidgets.QApplication.focusWidget() or target, SPACE)
    pump(rig.qt, 15)


def card_text(rig, name):
    return rig.window.timeline.bubble(name).original_label


def test_space_on_a_card_plays_it_then_the_following_ones(rig):
    w = rig.window
    w.timeline.select("t0")
    focus(rig, card_text(rig, "t0"))
    space(rig)
    assert rig.controller.owner == "t0" and rig.controller.state_for("t0") == "playing"
    assert rig.loads() == [rig.path("t0")]
    rig.finish()                                   # carries on through the Session
    assert rig.controller.owner == "t3"
    space(rig)                                     # pause
    assert rig.controller.state_for("t3") == "paused"
    space(rig)                                     # resume
    assert rig.controller.state_for("t3") == "playing"
    assert rig.loads() == [rig.path("t0"), rig.path("t3")], "Space restarted a clip"


def test_held_space_acts_once(rig):
    w = rig.window
    w.timeline.select("t0")
    focus(rig, card_text(rig, "t0"))
    space(rig, repeat=5)
    assert rig.controller.state_for("t0") == "playing"
    assert rig.loads() == [rig.path("t0")]


def test_space_stops_at_the_final_card_and_never_bookmarks(rig):
    w = rig.window
    rig.play("t4")                                 # the newest card
    assert rig.controller.owner == "t4"
    focus(rig, w.selected_bookmark_button)         # focus drifted onto Bookmark
    space(rig)
    assert rig.controller.state_for("t4") == "paused", "Space did not stop the last card"
    assert not rig.store.get_transmission("t4").bookmarked
    space(rig)                                     # resume, then let it end
    rig.finish()
    assert rig.controller.owner is None and not w.sequence.active
    focus(rig, w.selected_bookmark_button)
    space(rig, repeat=3)                           # at the end, with a held key
    assert not rig.store.get_transmission("t4").bookmarked, "Space set Bookmark"
    assert not w.selected_bookmark_button.isChecked()
    assert rig.controller.state_for("t4") == "playing", "Space replays the selection"
    space(rig)
    assert rig.controller.state_for("t4") == "paused"


def test_space_on_the_transport_buttons_toggles_once(rig):
    w = rig.window
    w.timeline.select("t0")
    focus(rig, w.selected_play_button)
    space(rig)
    assert rig.controller.state_for("t0") == "playing"
    focus(rig, w.playback_pause_button)
    space(rig)
    assert rig.controller.state_for("t0") == "paused", "toggled twice (button and Space)"
    focus(rig, w.playback_stop_button)
    space(rig)
    assert rig.controller.state_for("t0") == "playing"


def test_typing_spaces_in_the_editor_and_search_still_works(rig):
    w = rig.window
    w.timeline.select("t0")
    bubble = w.timeline.bubble("t0")
    bubble.edit_button.click()
    pump(rig.qt)
    editor = bubble.original_label
    assert not editor.isReadOnly()
    focus(rig, editor)
    editor.moveCursor(editor.textCursor().MoveOperation.End)
    QTest.keyClicks(editor, "a b")
    pump(rig.qt)
    assert editor.toPlainText().endswith("a b")
    assert rig.controller.owner is None, "typing a space started playback"
    bubble.cancel_edit()
    focus(rig, w.search_field)
    QTest.keyClicks(w.search_field, "two words")
    assert w.search_field.text() == "two words" and rig.controller.owner is None


def test_space_on_an_unrelated_control_activates_it(rig):
    w = rig.window
    clicked = []
    w.rename_session_button.clicked.disconnect()
    w.rename_session_button.clicked.connect(lambda: clicked.append(True))
    focus(rig, w.rename_session_button)
    space(rig)
    assert clicked == [True] and rig.controller.owner is None


def test_the_transport_is_bordered_with_a_play_triangle_and_shown_only_when_usable(rig):
    w = rig.window
    assert not w.playback_controls.isVisible(), "shown with nothing selected"
    w.timeline.select("t2-missing")                  # its file is missing
    pump(rig.qt)
    assert not w.playback_controls.isVisible(), "shown for a missing recording"
    w.timeline.select("t0")                          # finished and playable
    pump(rig.qt)
    assert w.playback_controls.isVisible() and w.selected_play_button.isEnabled()
    assert w.selected_play_button.text() == "\u25b6 Play selected"
    assert w.playback_pause_button.isVisible() and not w.playback_pause_button.isEnabled()
    assert w.playback_stop_button.isVisible() and not w.playback_stop_button.isEnabled()
    names = {b.objectName() for b in (w.selected_play_button, w.playback_pause_button,
                                       w.playback_stop_button, w.playback_rewind_button,
                                       w.playback_forward_button)}
    assert names == {"transportButton"}
    style = QtWidgets.QApplication.instance().styleSheet() or w.styleSheet()
    assert "#transportButton" in style or "QPushButton" in style


def test_check_asks_approve_and_approved_is_distinct(rig):
    from babelfishr.models import ProcessingState
    from babelfishr.ui.timeline import APPROVE

    tx = rig.store.get_transmission("t0")
    tx.transcript_confidence = 0.2                 # low confidence: CHECK
    tx.state = ProcessingState.COMPLETE
    rig.store.save_transmission(tx)
    rig.window._reload_timeline()
    pump(rig.qt)
    bubble = rig.window.timeline.bubble("t0")
    assert bubble.tx.needs_review
    assert APPROVE == "Approve?" and bubble.approve_button.text() == "Approve?"
    assert bubble.approve_button.isVisibleTo(bubble) and bubble.review_label.text() == "CHECK"
    bubble.approve_button.click()
    rig.window._drain_events()                    # the store's update, applied
    pump(rig.qt)
    bubble = rig.window.timeline.bubble("t0")
    assert not bubble.approve_button.isVisibleTo(bubble)
    assert bubble.review_label.text() == "APPROVED"
