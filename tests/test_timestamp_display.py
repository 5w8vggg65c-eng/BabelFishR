"""View > Show Times In: Local (default) or UTC.

Display only: stored instants stay UTC and exports keep their ISO-8601
values. In UTC every operator-visible time says so; dates (and so day
boundaries) come from the chosen zone. The choice is saved and applied at
once to messages already shown and to new ones.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time

import pytest

from babelfishr import timefmt
from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.export import to_json
from babelfishr.models import ProcessingState, Session, Transmission

# 23:30 UTC on 1 Jan: already 2 Jan in Tokyo (UTC+9, no DST).
LATE = dt.datetime(2026, 1, 1, 23, 30, 5, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def tokyo_and_local(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    timefmt.set_display_zone(timefmt.LOCAL)
    yield
    timefmt.set_display_zone(timefmt.LOCAL)
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


def test_local_is_the_default_everywhere():
    assert Config().display.timezone == "local"
    assert Config.from_dict({}).display.timezone == "local"
    assert timefmt.display_zone() == timefmt.LOCAL


def test_local_and_utc_stamps_and_the_day_boundary():
    assert timefmt.stamp(LATE) == "2026-01-02 08:30:05"          # Tokyo
    assert timefmt.day(LATE) == dt.date(2026, 1, 2)
    timefmt.set_display_zone(timefmt.UTC)
    assert timefmt.stamp(LATE) == "2026-01-01 23:30:05 UTC"
    assert timefmt.clock(LATE) == "23:30:05 UTC"
    assert timefmt.day(LATE) == dt.date(2026, 1, 1)
    # A naive value is a stored UTC instant.
    assert timefmt.stamp(LATE.replace(tzinfo=None)) == "2026-01-01 23:30:05 UTC"


def test_a_span_names_both_dates_when_it_crosses_midnight():
    end = LATE + dt.timedelta(minutes=40)                        # 00:10 UTC, 2 Jan
    timefmt.set_display_zone(timefmt.UTC)
    assert timefmt.span(LATE, end) == "2026-01-01 23:30:05–2026-01-02 00:10:05 UTC"
    timefmt.set_display_zone(timefmt.LOCAL)
    assert timefmt.span(LATE, end) == "08:30:05–09:10:05"   # same Tokyo day


def test_exports_keep_the_canonical_instant_in_either_zone():
    tx = Transmission(session_id="s", started_at=LATE, duration=2.0)
    local = json.loads(to_json([tx]))["transmissions"][0]["started_at"]
    timefmt.set_display_zone(timefmt.UTC)
    utc = json.loads(to_json([tx]))["transmissions"][0]["started_at"]
    assert local == utc and local.startswith("2026-01-01T23:30:05")


# --- the window -------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(config, store):
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    return app, MainWindow(app)


def _saved_message(app, store, when=LATE):
    store.save_session(Session(id="s1", conversation_id=app.conversation_id))
    tx = Transmission(session_id="s1", started_at=when, ended_at=when + dt.timedelta(seconds=4),
                      duration=4.0, state=ProcessingState.COMPLETE, transcript="radio check")
    store.save_transmission(tx)
    return tx


def test_switching_to_utc_redraws_shown_and_new_messages_and_is_saved(
        qt_app, config, store, tmp_path):
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app, window = _window(config, store)
    try:
        tx = _saved_message(app, store)
        window._reload_timeline()
        bubble = window.timeline.bubble(tx.id)
        assert window.time_heading.text() == "TIME"
        assert "2026-01-02 08:30:05" in bubble.header.text() and "UTC" not in bubble.header.text()
        assert bubble.time_label.text() == "08:30:05"
        window.timeline.select(tx.id)
        window._refresh_selected_panel()

        window.time_zone_actions["utc"].trigger()             # View > Show Times In > UTC
        assert window.time_heading.text() == "TIME (UTC)"
        assert "2026-01-01 23:30:05 UTC" in bubble.header.text()
        assert bubble.time_label.text() == "23:30:05"
        assert "UTC" in bubble.time_label.toolTip()
        assert window.time_zone_actions["utc"].isChecked()
        assert not window.time_zone_actions["local"].isChecked()
        assert Config.load(config.source_path).display.timezone == "utc"

        # A message arriving afterwards is drawn in UTC too.
        later = LATE + dt.timedelta(hours=1)
        new = Transmission(session_id="s1", started_at=later, duration=1.0,
                           state=ProcessingState.COMPLETE, transcript="copy")
        store.save_transmission(new)
        window.timeline.add(new)
        assert "2026-01-02 00:30:05 UTC" in window.timeline.bubble(new.id).header.text()
        # Stored instants are untouched.
        assert store.get_transmission(tx.id).started_at == LATE

        window.time_zone_actions["local"].trigger()            # and back
        assert window.time_heading.text() == "TIME"
        assert "UTC" not in bubble.header.text()
    finally:
        window.close()
        app.close()


def test_the_saved_choice_applies_at_the_next_launch(qt_app, config, store):
    config.display.timezone = "utc"
    app, window = _window(config, store)
    try:
        tx = _saved_message(app, store)
        window._reload_timeline()
        assert window.time_heading.text() == "TIME (UTC)"
        assert "UTC" in window.timeline.bubble(tx.id).header.text()
    finally:
        window.close()
        app.close()


def test_the_selected_time_range_follows_the_zone(qt_app, config, store):
    app, window = _window(config, store)
    try:
        tx = _saved_message(app, store)
        window._reload_timeline()
        window.timeline.select(tx.id)
        assert window.timeline.selected_transmission().id == tx.id
        window._refresh_selected_panel()
        assert window.selected_range.text().startswith("08:30:05–08:30:09")
        window._set_time_zone("utc")
        assert window.selected_range.text().startswith("23:30:05–23:30:09 UTC")
    finally:
        window.close()
        app.close()


def test_session_options_has_the_same_setting_in_step_with_the_view_menu(
        qt_app, config, store, tmp_path):
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app, window = _window(config, store)
    try:
        box = window.time_zone_box
        assert box.window() is window.session_options_dialog
        assert box.accessibleName() == "Timestamp time zone"
        assert box.currentData() == "local"
        # The dialog changes the menu, the heading and the saved setting ...
        box.setCurrentIndex(box.findData("utc"))
        assert window.time_zone_actions["utc"].isChecked()
        assert window.time_heading.text() == "TIME (UTC)"
        assert Config.load(config.source_path).display.timezone == "utc"
        # ... and the menu changes the dialog.
        window.time_zone_actions["local"].trigger()
        assert box.currentData() == "local"
        assert timefmt.display_zone() == timefmt.LOCAL
        # A display choice: still available while monitoring.
        window._set_controls_enabled(False)
        assert box.isEnabled()
        assert not window.quiet_seconds_box.isEnabled()
        window._set_controls_enabled(True)
    finally:
        window.close()
        app.close()


def test_a_saved_utc_shows_in_the_dialog_at_launch(qt_app, config, store):
    config.display.timezone = "utc"
    app, window = _window(config, store)
    try:
        assert window.time_zone_box.currentData() == "utc"
    finally:
        window.close()
        app.close()
