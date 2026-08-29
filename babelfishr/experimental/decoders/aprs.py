"""Interpretation of APRS payloads carried inside AX.25 UI frames."""

from __future__ import annotations

import re
from typing import Dict, Optional

_POSITION_RE = re.compile(
    r"(?P<lat>\d{4}\.\d{2})(?P<ns>[NS])(?P<sym1>.)(?P<lon>\d{5}\.\d{2})(?P<ew>[EW])(?P<sym2>.)"
)
_MESSAGE_RE = re.compile(r"^:(?P<addressee>.{9}):(?P<text>.*?)(?:\{(?P<msgno>\w+))?$")

DATA_TYPES = {
    "!": "position (no timestamp)", "=": "position (messaging)",
    "/": "position (timestamped)", "@": "position (timestamped, messaging)",
    ":": "message", ">": "status", ";": "object", ")": "item",
    "_": "weather", "T": "telemetry", "`": "mic-e", "'": "mic-e (old)",
    "?": "query", "<": "station capabilities", "$": "raw NMEA",
}


def dm_to_degrees(value: str, hemisphere: str) -> float:
    """APRS ddmm.mm / dddmm.mm -> signed decimal degrees."""
    if "." not in value:
        return 0.0
    head, _, _ = value.partition(".")
    deg_len = len(head) - 2
    degrees = float(value[:deg_len])
    minutes = float(value[deg_len:])
    result = degrees + minutes / 60.0
    return -result if hemisphere in ("S", "W") else result


def describe_payload(info: bytes) -> Optional[Dict[str, object]]:
    """Best-effort structured view of an APRS information field."""
    if not info:
        return None
    text = info.decode("utf-8", errors="replace")
    kind = DATA_TYPES.get(text[0])
    out: Dict[str, object] = {"type": kind or "unknown", "text": text}

    match = _POSITION_RE.search(text)
    if match:
        out["latitude"] = round(dm_to_degrees(match.group("lat"), match.group("ns")), 6)
        out["longitude"] = round(dm_to_degrees(match.group("lon"), match.group("ew")), 6)
        out["symbol"] = match.group("sym1") + match.group("sym2")
        tail = text[match.end():]
        course_speed = re.match(r"(\d{3})/(\d{3})", tail)
        if course_speed:
            out["course_deg"] = int(course_speed.group(1))
            out["speed_knots"] = int(course_speed.group(2))
            tail = tail[7:]
        altitude = re.search(r"/A=(-?\d{6})", tail)
        if altitude:
            out["altitude_ft"] = int(altitude.group(1))
        out["comment"] = tail.strip()

    if text.startswith(":"):
        msg = _MESSAGE_RE.match(text)
        if msg:
            out["addressee"] = msg.group("addressee").strip()
            out["message"] = msg.group("text")
            if msg.group("msgno"):
                out["message_number"] = msg.group("msgno")

    if text.startswith(">"):
        out["status"] = text[1:].strip()

    weather = re.search(r"t(-?\d{3})", text)
    if weather and (text.startswith("_") or "h" in text):
        out["temperature_f"] = int(weather.group(1))

    return out


def format_summary(payload: Dict[str, object]) -> str:
    if "latitude" in payload:
        base = f"position {payload['latitude']:.5f}, {payload['longitude']:.5f}"
        if payload.get("comment"):
            base += f" - {payload['comment']}"
        return base
    if "message" in payload:
        return f"message to {payload.get('addressee')}: {payload['message']}"
    if "status" in payload:
        return f"status: {payload['status']}"
    return str(payload.get("text", ""))
