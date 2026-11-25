"""Operator-visible times, in the zone the operator chose.

Two saved settings (``[display]``):

* ``timezone`` - ``"local"`` or ``"utc"`` (as in Build 39), and
* ``utc_zone`` - what "UTC" means when it is chosen: ``"utc"`` (UTC/Zulu,
  +00:00, the default), a fixed offset from ``"-12:00"`` to ``"+14:00"`` in
  quarter hours (``"+05:30"``, ``"+05:45"``, ``"-03:30"``), or an IANA region
  such as ``"America/Denver"``, read with :mod:`zoneinfo` from the offline
  ``tzdata`` database shipped with the app, so daylight saving time follows
  each instant's own date, past or present.

Only how a time is *shown* changes. Stored instants stay timezone-aware UTC
and exports keep their machine-readable ISO-8601 instants. Outside Local,
every time names the offset or zone it is in, for that instant; dates come
from the chosen zone. A wall-clock time that occurs twice (the repeated hour
when daylight saving ends) carries its abbreviation even where the zone is
otherwise implied, so the two are told apart. Nothing here uses the network
or the computer's location.
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional, Tuple

LOCAL = "local"
UTC = "utc"
ZONES = (LOCAL, UTC)

#: utc_zone meaning UTC/Zulu itself.
ZULU = "utc"
#: The fixed offsets offered, in minutes: -12:00 to +14:00 by quarter hours.
OFFSET_MINUTES = tuple(range(-12 * 60, 14 * 60 + 1, 15))
_OFFSET = re.compile(r"^([+-])(\d{2}):(\d{2})$")
#: Not offered as regions: aliases and legacy or confusingly signed names.
_SKIPPED_PREFIXES = ("Etc/", "SystemV/", "US/", "posix/", "right/")

_mode = LOCAL
_utc_zone = ZULU
_tz: Optional[_dt.tzinfo] = None       # None: this computer's local zone
_kind = "local"                        # local | utc | offset | region
_problem = ""


def offset_key(minutes: int) -> str:
    sign = "-" if minutes < 0 else "+"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def _format_offset(delta: Optional[_dt.timedelta]) -> str:
    minutes = int((delta or _dt.timedelta(0)).total_seconds() // 60)
    return offset_key(minutes)


def resolve(utc_zone: str) -> Tuple[_dt.tzinfo, str, str]:
    """(tzinfo, kind, problem) for a ``utc_zone`` value. A value that cannot
    be used - unknown, malformed, or a region whose data is missing - falls
    back to UTC/Zulu and says why; it never raises."""
    value = (utc_zone or ZULU).strip()
    if value.lower() in (ZULU, "z", "zulu", "+00:00", "-00:00"):
        return _dt.timezone.utc, "utc", ""
    match = _OFFSET.match(value)
    if match:
        sign, hours, minutes = match.groups()
        total = (int(hours) * 60 + int(minutes)) * (-1 if sign == "-" else 1)
        if total in OFFSET_MINUTES:
            return _dt.timezone(_dt.timedelta(minutes=total)), "offset", ""
        return _dt.timezone.utc, "utc", (
            f"UTC{value} is not an offset between UTC-12:00 and UTC+14:00 in "
            f"quarter hours; showing UTC.")
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(value), "region", ""
    except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, OSError
        return _dt.timezone.utc, "utc", (
            f"The time zone region “{value}” is not available on this "
            f"computer; showing UTC instead.")


def set_display_zone(zone: str, utc_zone: str = ZULU) -> str:
    """Show times in *zone* (``local`` or ``utc``); for ``utc``, in
    *utc_zone*. Returns why the requested region could not be used, or ""."""
    global _mode, _utc_zone, _tz, _kind, _problem
    _mode = zone if zone in ZONES else LOCAL
    _utc_zone = utc_zone or ZULU
    _problem = ""
    if _mode == LOCAL:
        _tz, _kind = None, "local"
    else:
        _tz, _kind, _problem = resolve(_utc_zone)
    return _problem


def display_zone() -> str:
    return _mode


def utc_zone() -> str:
    return _utc_zone


def problem() -> str:
    return _problem


def is_utc() -> bool:
    """True for UTC and everything chosen under it (offsets, regions)."""
    return _mode == UTC


def to_display(when: _dt.datetime) -> _dt.datetime:
    """*when* converted to the display zone. A naive value is taken as UTC,
    as every stored instant is."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    return when.astimezone(_tz) if _tz is not None else when.astimezone()


def _shown_offset(when: _dt.datetime) -> _dt.timedelta:
    return to_display(when).utcoffset() or _dt.timedelta(0)


def is_repeated(when: _dt.datetime) -> bool:
    """This instant's wall-clock time also names another instant within a
    few hours (the repeated hour when daylight saving ends)."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)
    own = _shown_offset(when)
    for probe in (when - _dt.timedelta(hours=3), when + _dt.timedelta(hours=3)):
        other = _shown_offset(probe)
        if other != own:
            twin = when + own - other
            if _shown_offset(twin) == other:
                return True
    return False


def _abbreviation(shown: _dt.datetime) -> str:
    name = shown.tzname() or ""
    return name if name.isalpha() else ""


def _suffix(when: _dt.datetime) -> str:
    """The zone this instant is shown in, with a leading space."""
    shown = to_display(when)
    if _kind == "utc":
        return " UTC"
    offset = f"UTC{_format_offset(shown.utcoffset())}"
    if _kind == "offset":
        return f" {offset}"
    if _kind == "region":
        name = _abbreviation(shown)
        return f" {name} ({offset})" if name else f" {offset}"
    # Local: no zone word, as before - except a repeated wall time.
    if is_repeated(when):
        return f" {_abbreviation(shown) or offset}"
    return ""


def stamp(when: _dt.datetime) -> str:
    """``2026-09-07 10:41:20`` (Local), ``… UTC``, ``… UTC+05:30`` or
    ``… MDT (UTC-06:00)``."""
    return to_display(when).strftime("%Y-%m-%d %H:%M:%S") + _suffix(when)


def clock(when: _dt.datetime) -> str:
    return to_display(when).strftime("%H:%M:%S") + _suffix(when)


def bare_clock(when: _dt.datetime) -> str:
    """``10:41:20`` with no zone word, for a column whose zone is shown
    beside it; a repeated wall time still gets its abbreviation."""
    shown = to_display(when)
    text = shown.strftime("%H:%M:%S")
    if is_repeated(when):
        text += " " + (_abbreviation(shown) or f"UTC{_format_offset(shown.utcoffset())}")
    return text


def day(when: _dt.datetime) -> _dt.date:
    """The calendar day in the display zone (for date boundaries)."""
    return to_display(when).date()


def span(start: _dt.datetime, end: Optional[_dt.datetime]) -> str:
    """``10:41:20–10:41:24`` plus the zone; dates are added when the ends
    fall on different days, and each end names its own zone when the two
    differ (a change of daylight saving time in between)."""
    end = end or start
    a, b = to_display(start), to_display(end)
    fmt = "%Y-%m-%d %H:%M:%S" if a.date() != b.date() else "%H:%M:%S"
    first, last = _suffix(start), _suffix(end)
    if first == last:
        return f"{a.strftime(fmt)}–{b.strftime(fmt)}{first}"
    return f"{a.strftime(fmt)}{first}–{b.strftime(fmt)}{last}"


def zone_label() -> str:
    """For the interface: what the times on screen are in."""
    if _kind == "utc":
        return "UTC"
    if _kind == "offset":
        return f"UTC{_utc_zone}"
    if _kind == "region":
        return f"{city(_utc_zone)} ({_utc_zone})"
    name = _dt.datetime.now().astimezone().tzname() or "local"
    return f"Local ({name})"


def heading() -> str:
    """The TIME column heading."""
    if _kind == "local":
        return "TIME"
    if _kind == "region":
        return f"TIME ({city(_utc_zone)})"
    return f"TIME ({zone_label()})"


def city(key: str) -> str:
    """``America/Denver`` -> ``Denver``; ``America/Argentina/Buenos_Aires``
    -> ``Buenos Aires``."""
    return key.rsplit("/", 1)[-1].replace("_", " ")


def choices() -> List[Tuple[str, str]]:
    """(label, utc_zone) for the picker: UTC/Zulu, every fixed offset, then
    the named regions available offline (none if the data is missing)."""
    out: List[Tuple[str, str]] = [("UTC / Zulu (+00:00)", ZULU)]
    out += [(f"UTC{offset_key(m)}", offset_key(m)) for m in OFFSET_MINUTES if m]
    out += [(f"{city(key)} — {key}", key) for key in regions()]
    return out


def regions() -> List[str]:
    try:
        from zoneinfo import available_timezones

        keys = available_timezones()
    except Exception:  # noqa: BLE001
        return []
    return sorted(k for k in keys
                  if "/" in k and not k.startswith(_SKIPPED_PREFIXES))
