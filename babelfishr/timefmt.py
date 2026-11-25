"""Operator-visible times, in the zone the operator chose: Local or UTC.

Only how a time is *shown* changes. Stored instants stay timezone-aware
UTC, and exports keep their machine-readable ISO-8601 instants. When UTC is
chosen every time says so ("UTC"); local times carry no suffix, as before.
Dates come from the same zone, so a message just after midnight UTC shows
the UTC date, and the same message in Local the local date.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

LOCAL = "local"
UTC = "utc"
ZONES = (LOCAL, UTC)

_zone = LOCAL


def set_display_zone(zone: str) -> None:
    global _zone
    _zone = zone if zone in ZONES else LOCAL


def display_zone() -> str:
    return _zone


def is_utc() -> bool:
    return _zone == UTC


def to_display(when: _dt.datetime) -> _dt.datetime:
    """*when* converted to the display zone. A naive value is taken as UTC,
    as every stored instant is."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_dt.timezone.utc) if _zone == UTC else when.astimezone()


def _suffix() -> str:
    return " UTC" if _zone == UTC else ""


def stamp(when: _dt.datetime) -> str:
    """``2026-09-07 10:41:20`` (Local) or ``2026-09-07 10:41:20 UTC``."""
    return to_display(when).strftime("%Y-%m-%d %H:%M:%S") + _suffix()


def clock(when: _dt.datetime) -> str:
    """``10:41:20`` (Local) or ``10:41:20 UTC``."""
    return to_display(when).strftime("%H:%M:%S") + _suffix()


def bare_clock(when: _dt.datetime) -> str:
    """``10:41:20`` with no zone word, for a column whose zone is shown
    beside it."""
    return to_display(when).strftime("%H:%M:%S")


def day(when: _dt.datetime) -> _dt.date:
    """The calendar day in the display zone (for date boundaries)."""
    return to_display(when).date()


def span(start: _dt.datetime, end: Optional[_dt.datetime]) -> str:
    """``10:41:20–10:41:24`` (+ `` UTC``); the start's date is added when
    the end falls on another day in the display zone."""
    end = end or start
    a, b = to_display(start), to_display(end)
    if a.date() != b.date():
        text = f"{a:%Y-%m-%d %H:%M:%S}–{b:%Y-%m-%d %H:%M:%S}"
    else:
        text = f"{a:%H:%M:%S}–{b:%H:%M:%S}"
    return text + _suffix()


def zone_label() -> str:
    """For the interface: what the times on screen are in."""
    if _zone == UTC:
        return "UTC"
    name = _dt.datetime.now().astimezone().tzname() or "local"
    return f"Local ({name})"
