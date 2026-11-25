"""The live status says which recording is being processed and what waits.

From the processor's own queue snapshot: the badge names the recording
being processed (its time) and how long it has taken, with the number
waiting behind it; each waiting card says its place in line, and the
notes clear when the work is done. Monitoring stays armed throughout.
Generated audio and a scripted engine that holds one clip.
"""

from __future__ import annotations

import os
import time

import pytest

from test_live_throughput import GAP, ScriptedASR, _push, _rows, _start, _voice, _wait


@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pump_until(qt_app, window, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        window._queue_tick = 0.0               # do not wait the full second
        window._drain_events()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_the_badge_and_cards_say_what_is_processing_and_what_waits(qt_app, config,
                                                                   store):
    from babelfishr.models import ProcessingState
    from babelfishr.timefmt import bare_clock
    from babelfishr.ui.main_window import MainWindow

    asr = ScriptedASR({1: "hold"})
    app, source, session = _start(config, store, asr)
    window = MainWindow(app)
    window.show()
    try:
        _push(source, [GAP, _voice(1.0), GAP, _voice(1.0), GAP, _voice(1.0), GAP,
                       _voice(1.0), GAP], seed=3)
        assert asr.held.wait(20.0)
        assert _wait(lambda: len(_rows(store, session.id)) == 4)
        rows = _rows(store, session.id)
        first = rows[0]
        assert _pump_until(qt_app, window,
                           lambda: "3 waiting" in window.state_badge.text())
        badge = window.state_badge.text()
        assert bare_clock(first.started_at) in badge, badge
        assert app.capture is not None, "monitoring stays armed"
        notes = {r.id: window.timeline.bubble(r.id).original_label.text()
                 for r in rows if window.timeline.bubble(r.id) is not None}
        assert "transcription" in notes[first.id]
        assert notes[rows[1].id].endswith("next in line")
        assert notes[rows[2].id].endswith("2nd in line")
        assert notes[rows[3].id].endswith("3rd in line")
        asr.release.set()
        assert _wait(lambda: all(r.state is ProcessingState.COMPLETE
                                 for r in _rows(store, session.id)))
        assert _pump_until(qt_app, window, lambda: not any(
            "in line" in window.timeline.bubble(r.id).original_label.text()
            for r in rows if window.timeline.bubble(r.id) is not None))
        assert "waiting" not in window.state_badge.text()
    finally:
        asr.release.set()
        app.stop_session()
        window.close()
        app.close()
