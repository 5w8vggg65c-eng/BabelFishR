"""The recorded waveform's playhead can be clicked or dragged to seek.

Only within the recording being played or paused and drawn there: playing
stays playing, paused stays paused, positions scale to the drawn plot and
the clip's duration and are clamped to it, and a drag lets go when
continuous playback moves on to the next clip. Left/Right move 5 s. Real
WAVs and real widget geometry; a scripted backend (no sound).
"""

from __future__ import annotations

import os

import pytest
from PySide6 import QtCore
from PySide6.QtTest import QTest

from test_alpha5_playback import pump
from test_playback_follow import FollowRig


@pytest.fixture(scope="module")
def qt_app():
    from PySide6 import QtWidgets
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def rig(qt_app, config, store, tmp_path):
    made = FollowRig(qt_app, config, store, tmp_path)
    yield made
    made.close()


def _x(rig, fraction):
    area = rig.window.waveform._wave_rect()
    return QtCore.QPoint(int(area.left() + fraction * (area.width() - 1)),
                         area.center().y())


def _seeks(rig):
    return [c[1] for c in rig.backend.calls if c[0] == "seek"]


def test_clicking_seeks_and_keeps_playing(rig):
    rig.play("t0")
    pump(rig.qt)
    wave = rig.window.waveform
    assert wave.current_tx_id == "t0"
    QTest.mouseClick(wave, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, _x(rig, 0.5))
    pump(rig.qt)
    assert abs(rig.backend.position() - 4000) <= 40, rig.backend.position()   # 8 s clip
    assert rig.controller.state_for("t0") == "playing"


def test_clicking_while_paused_stays_paused(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt)
    w.playback_pause_button.click()
    pump(rig.qt)
    QTest.mouseClick(w.waveform, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, _x(rig, 0.25))
    pump(rig.qt)
    assert abs(rig.backend.position() - 2000) <= 40
    assert rig.controller.state_for("t0") == "paused"


def test_nothing_happens_when_the_drawn_recording_is_not_playing(rig):
    w = rig.window
    w.timeline.select("t3")                          # drawn, not playing
    pump(rig.qt)
    QTest.mouseClick(w.waveform, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, _x(rig, 0.5))
    pump(rig.qt)
    assert _seeks(rig) == [] and rig.controller.owner is None


def test_dragging_is_clamped_and_lets_go_when_the_next_clip_starts(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt)
    wave = w.waveform
    QTest.mousePress(wave, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, _x(rig, 0.2))
    QTest.mouseMove(wave, _x(rig, 0.7))
    pump(rig.qt)
    assert abs(rig.backend.position() - 5600) <= 40
    QTest.mouseMove(wave, QtCore.QPoint(wave.width() + 50, 30))      # past the end
    assert rig.backend.position() == 8000 or rig.controller.owner != "t0"
    pump(rig.qt, 20)                                 # t0 ends; t3 starts
    if rig.controller.owner == "t0":
        rig.finish()
    assert rig.controller.owner == "t3"
    before = len(_seeks(rig))
    QTest.mouseMove(wave, _x(rig, 0.1))              # still holding the button
    pump(rig.qt)
    assert len(_seeks(rig)) == before, "a stale drag moved the next clip"
    QTest.mouseRelease(wave, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, _x(rig, 0.1))
    QTest.mousePress(wave, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier,
                     QtCore.QPoint(-40, 30))         # before the start: clamped to 0
    QTest.mouseRelease(wave, QtCore.Qt.LeftButton)
    pump(rig.qt)
    assert rig.backend.position() == 0 and rig.controller.owner == "t3"


def test_left_and_right_move_five_seconds(rig):
    w = rig.window
    rig.play("t0")
    pump(rig.qt)
    w.waveform.setFocus()
    QTest.keyClick(w.waveform, QtCore.Qt.Key_Right)
    pump(rig.qt)
    assert rig.backend.position() == 5000
    QTest.keyClick(w.waveform, QtCore.Qt.Key_Left)
    QTest.keyClick(w.waveform, QtCore.Qt.Key_Left)
    pump(rig.qt)
    assert rig.backend.position() == 0
    assert rig.controller.state_for("t0") == "playing"


def test_shared_slider_scrubs_paused_audio_from_keyboard_and_mouse(rig):
    w = rig.window
    rig.play("t0")
    w.playback_pause_button.click()
    slider = w.playback_seek
    assert slider.isVisible() and slider.isEnabled()
    point = QtCore.QPoint(slider.width() // 2, slider.height() // 2)
    QTest.mouseClick(slider, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    halfway = rig.backend.position()
    assert abs(halfway - 4000) < 200
    QTest.keyClick(slider, QtCore.Qt.Key_Right)
    assert rig.backend.position() == halfway + 1000
    assert rig.controller.state_for("t0") == "paused"


def test_shared_slider_does_not_scrub_the_next_short_recording_during_a_drag(rig):
    w = rig.window
    rig.play("t0")
    slider = w.playback_seek
    point = QtCore.QPoint(slider.width() // 4, slider.height() // 2)
    QTest.mousePress(slider, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    rig.finish()
    assert rig.controller.owner == "t3"
    seeks = list(_seeks(rig))
    QTest.mouseMove(slider, point + QtCore.QPoint(15, 0))
    QTest.mouseRelease(slider, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    assert _seeks(rig) == seeks
