"""UTC offsets and named regions, chosen in Session Options.

Under UTC the operator picks UTC/Zulu, a fixed offset (-12:00 to +14:00 in
quarter hours) or a named IANA region read offline with zoneinfo and the
bundled tzdata. Every displayed time names its own offset or zone for that
instant, daylight saving follows each instant's date, and the repeated hour
when daylight saving ends is told apart. Stored instants and exports never
change. Build 39 settings read the same. Synthetic data only; no network.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from babelfishr import timefmt
from babelfishr.config import Config
from babelfishr.export import to_json
from babelfishr.models import ProcessingState, Session, Transmission

UTC = dt.timezone.utc
REPO = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def tokyo_local(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    timefmt.set_display_zone(timefmt.LOCAL)
    yield
    timefmt.set_display_zone(timefmt.LOCAL)
    monkeypatch.delenv("TZ", raising=False)
    time.tzset()


# --- the choices ------------------------------------------------------------------

def test_every_quarter_hour_offset_and_named_regions_are_offered():
    keys = [key for _, key in timefmt.choices()]
    assert keys[0] == timefmt.ZULU
    offsets = [k for k in keys if k[:1] in "+-"]
    assert offsets[0] == "-12:00" and offsets[-1] == "+14:00"
    assert len(offsets) == 26 * 4                      # every quarter hour but 00:00
    for wanted in ("+05:30", "+05:45", "-03:30", "+12:45", "-09:30"):
        assert wanted in offsets
    for region in ("America/Denver", "America/Phoenix", "America/New_York",
                   "Europe/London", "Asia/Kathmandu", "Pacific/Auckland"):
        assert region in keys
    labels = dict((key, label) for label, key in timefmt.choices())
    assert labels["America/Denver"] == "Denver — America/Denver"
    assert labels[timefmt.ZULU] == "UTC / Zulu (+00:00)"
    assert not any(k.startswith(("Etc/", "US/")) for k in keys)


# --- formatting ------------------------------------------------------------------

def test_fixed_fractional_offsets():
    when = dt.datetime(2026, 1, 1, 23, 30, tzinfo=UTC)
    for key, shown in (("+05:30", "2026-01-02 05:00:00 UTC+05:30"),
                       ("+05:45", "2026-01-02 05:15:00 UTC+05:45"),
                       ("-03:30", "2026-01-01 20:00:00 UTC-03:30"),
                       ("+14:00", "2026-01-02 13:30:00 UTC+14:00"),
                       ("-12:00", "2026-01-01 11:30:00 UTC-12:00")):
        assert timefmt.set_display_zone("utc", key) == ""
        assert timefmt.stamp(when) == shown
        assert timefmt.heading() == f"TIME (UTC{key})"


def test_zulu_reads_exactly_as_build_39():
    when = dt.datetime(2026, 1, 1, 23, 30, 5, tzinfo=UTC)
    timefmt.set_display_zone("utc")                    # Build 39: no utc_zone
    assert timefmt.stamp(when) == "2026-01-01 23:30:05 UTC"
    assert timefmt.heading() == "TIME (UTC)"
    assert Config.from_dict({"display": {"timezone": "utc"}}).display.utc_zone == "utc"


def test_denver_follows_daylight_saving_by_each_instants_date():
    timefmt.set_display_zone("utc", "America/Denver")
    summer = dt.datetime(2026, 7, 1, 18, 0, tzinfo=UTC)
    winter = dt.datetime(2026, 1, 1, 18, 0, tzinfo=UTC)
    old = dt.datetime(2006, 3, 20, 18, 0, tzinfo=UTC)  # before the 2007 US rule change
    assert timefmt.stamp(summer) == "2026-07-01 12:00:00 MDT (UTC-06:00)"
    assert timefmt.stamp(winter) == "2026-01-01 11:00:00 MST (UTC-07:00)"
    assert timefmt.stamp(old) == "2006-03-20 11:00:00 MST (UTC-07:00)"
    assert timefmt.heading() == "TIME (Denver)"
    timefmt.set_display_zone("utc", "America/Phoenix")
    assert timefmt.stamp(summer).endswith("MST (UTC-07:00)")      # no DST in Phoenix


def test_the_repeated_fall_back_hour_is_told_apart():
    timefmt.set_display_zone("utc", "America/Denver")
    first = dt.datetime(2026, 11, 1, 7, 30, tzinfo=UTC)          # 01:30 MDT
    second = first + dt.timedelta(hours=1)                       # 01:30 MST
    assert timefmt.bare_clock(first) == "01:30:00 MDT"
    assert timefmt.bare_clock(second) == "01:30:00 MST"
    assert timefmt.is_repeated(first) and timefmt.is_repeated(second)
    ordinary = dt.datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert timefmt.bare_clock(ordinary) == "05:00:00"
    # A span across the change names each end's own zone.
    assert timefmt.span(first, second) == \
        "01:30:00 MDT (UTC-06:00)–01:30:00 MST (UTC-07:00)"


def test_kathmandu_and_auckland_and_midnight():
    timefmt.set_display_zone("utc", "Asia/Kathmandu")
    when = dt.datetime(2026, 1, 1, 18, 30, tzinfo=UTC)
    assert timefmt.stamp(when) == "2026-01-02 00:15:00 UTC+05:45"   # numeric abbreviation
    assert timefmt.day(when) == dt.date(2026, 1, 2)
    timefmt.set_display_zone("utc", "Pacific/Auckland")
    assert timefmt.stamp(when) == "2026-01-02 07:30:00 NZDT (UTC+13:00)"
    end = when + dt.timedelta(hours=20)
    assert timefmt.span(when, end).startswith("2026-01-02 07:30:00")


@pytest.mark.parametrize("bad", ["Mars/Olympus_Mons", "../../etc/passwd", "+15:00",
                                 "+05:20", "garbage"])
def test_an_unusable_choice_falls_back_to_utc_and_says_why(bad):
    problem = timefmt.set_display_zone("utc", bad)
    assert problem and "UTC" in problem
    assert timefmt.stamp(dt.datetime(2026, 1, 1, tzinfo=UTC)).endswith(" UTC")
    assert timefmt.utc_zone() == bad, "the saved choice itself is kept"


def test_missing_region_data_falls_back_and_offsets_still_work(monkeypatch):
    import zoneinfo

    def unavailable(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", unavailable)
    monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set())
    assert timefmt.set_display_zone("utc", "America/Denver")
    assert timefmt.zone_label() == "UTC"
    keys = [k for _, k in timefmt.choices()]
    assert "+05:45" in keys and "America/Denver" not in keys
    assert timefmt.set_display_zone("utc", "+05:45") == ""


def test_exports_never_change():
    tx = Transmission(session_id="s", started_at=dt.datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
                      duration=1.0)
    def rows():
        return json.loads(to_json([tx]))["transmissions"]   # not the export's own time

    baseline = rows()
    for zone, utc_zone in (("utc", "America/Denver"), ("utc", "+05:45"), ("local", "utc")):
        timefmt.set_display_zone(zone, utc_zone)
        assert rows() == baseline
    assert baseline[0]["started_at"].startswith("2026-11-01T07:30:00")


# --- settings ----------------------------------------------------------------------

def test_the_choice_round_trips_through_the_settings_file(tmp_path):
    path = tmp_path / "settings.toml"
    cfg = Config()
    assert (cfg.display.timezone, cfg.display.utc_zone) == ("local", "utc")
    cfg.display.timezone, cfg.display.utc_zone = "utc", "Asia/Kathmandu"
    cfg.save(str(path))
    again = Config.load(str(path))
    assert (again.display.timezone, again.display.utc_zone) == ("utc", "Asia/Kathmandu")


# --- offline packaging -----------------------------------------------------------------

def test_tzdata_is_a_dependency_and_is_bundled():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tzdata>=' in pyproject.split("dependencies =")[1].split("]")[0]
    spec = (REPO / "packaging" / "babelfishr.spec").read_text(encoding="utf-8")
    assert 'collect_data_files("tzdata")' in spec
    entry = (REPO / "packaging" / "app_entry.py").read_text(encoding="utf-8")
    assert '"tzdata"' in entry and "reset_tzpath([])" in entry


def test_regions_resolve_from_the_packaged_database_alone():
    """With the system search path emptied, zoneinfo can only use tzdata -
    as inside the frozen app, and on Windows."""
    pytest.importorskip("tzdata")
    code = ("import zoneinfo, datetime as d; zoneinfo.reset_tzpath([]); "
            "z = zoneinfo.ZoneInfo('America/Denver'); "
            "print(d.datetime(2026,7,1,tzinfo=z).tzname(), "
            "len(zoneinfo.available_timezones()) > 400)")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                         text=True, timeout=60, env={**os.environ, "PYTHONTZPATH": ""})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["MDT", "True"]


# --- the window ------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _window(config, store):
    from babelfishr.app import BabelFishRApp
    from babelfishr.ui.main_window import MainWindow

    app = BabelFishRApp(config=config, store=store)
    return app, MainWindow(app)


def _message(app, store, when):
    store.save_session(Session(id="s1", conversation_id=app.conversation_id))
    tx = Transmission(session_id="s1", started_at=when, duration=2.0,
                      state=ProcessingState.COMPLETE, transcript="radio check")
    store.save_transmission(tx)
    return tx


def test_the_picker_shows_only_under_utc_and_applies_to_shown_and_new(qt_app, config,
                                                                     store, tmp_path):
    config.source_path = str(tmp_path / "settings.toml")
    config.save()
    app, window = _window(config, store)
    try:
        first = _message(app, store, dt.datetime(2026, 11, 1, 7, 30, tzinfo=UTC))
        window._reload_timeline()
        form, picker = window.options_form, window.utc_zone_box
        assert not form.isRowVisible(picker), "no region picker under Local"
        window.time_zone_box.setCurrentIndex(window.time_zone_box.findData("utc"))
        assert form.isRowVisible(picker)
        assert picker.currentData() == "utc"              # Zulu by default
        # Searchable: the completer finds a city by any part of its name.
        completer = picker.completer()
        completer.setCompletionPrefix("kathm")
        assert completer.completionCount() >= 1
        assert "Kathmandu" in completer.currentCompletion()
        picker.setCurrentIndex(picker.findData("America/Denver"))
        window._on_utc_zone_index(picker.currentIndex())
        bubble = window.timeline.bubble(first.id)
        assert "01:30:00 MDT (UTC-06:00)" in bubble.header.text()
        assert bubble.time_label.text() == "01:30:00 MDT"       # the repeated hour
        assert window.time_heading.text() == "TIME (Denver)"
        assert window.time_zone_actions["utc"].text().startswith("UTC — Denver")
        saved = Config.load(config.source_path).display
        assert (saved.timezone, saved.utc_zone) == ("utc", "America/Denver")
        # A message arriving now is drawn in the same zone.
        second = Transmission(session_id="s1", duration=1.0, transcript="copy",
                              started_at=first.started_at + dt.timedelta(hours=1),
                              state=ProcessingState.COMPLETE)
        store.save_transmission(second)
        window.timeline.add(second)
        assert window.timeline.bubble(second.id).time_label.text() == "01:30:00 MST"
        # Local and back keeps the region.
        window.time_zone_actions["local"].trigger()
        assert not form.isRowVisible(picker)
        window.time_zone_actions["utc"].trigger()
        assert picker.currentData() == "America/Denver"
        assert store.get_transmission(first.id).started_at == first.started_at
    finally:
        window.session_options_dialog.close()
        window.close()
        app.close()


def test_typing_an_offset_and_a_wrong_entry(qt_app, config, store):
    config.display.timezone = "utc"
    app, window = _window(config, store)
    try:
        picker = window.utc_zone_box
        window._on_utc_zone_text("UTC+05:45")
        assert app.config.display.utc_zone == "+05:45"
        assert window.time_heading.text() == "TIME (UTC+05:45)"
        window._on_utc_zone_text("Atlantis")               # names nothing
        assert app.config.display.utc_zone == "+05:45"
        assert picker.currentData() == "+05:45"
    finally:
        window.close()
        app.close()


def test_view_menu_opens_the_same_picker(qt_app, config, store):
    app, window = _window(config, store)
    try:
        window.utc_zone_action.trigger()
        qt_app.processEvents()
        assert window.session_options_dialog.isVisible()
        assert window.time_zone_box.currentData() == "utc"
        assert window.options_form.isRowVisible(window.utc_zone_box)
        assert window.time_zone_actions["utc"].isChecked()
    finally:
        window.session_options_dialog.close()
        window.close()
        app.close()


def test_a_saved_region_shows_at_launch_and_a_missing_one_is_reported(qt_app, config,
                                                                      store):
    config.display.timezone, config.display.utc_zone = "utc", "Asia/Kathmandu"
    app, window = _window(config, store)
    try:
        assert window.utc_zone_box.currentData() == "Asia/Kathmandu"
        assert window.time_heading.text() == "TIME (Kathmandu)"
    finally:
        window.close()
        app.close()
    from babelfishr.storage import Store

    config.display.utc_zone = "Nowhere/Gone"
    app, window = _window(config, Store(config.database))
    try:
        assert "not available" in window.warning_banner.text()
        assert window.time_heading.text() == "TIME (UTC)"
        assert app.config.display.utc_zone == "Nowhere/Gone", "not overwritten"
    finally:
        window.close()
        app.close()
