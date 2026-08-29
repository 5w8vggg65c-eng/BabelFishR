"""HDLC bit-stuffing, X.25 FCS and AX.25 frame parsing.

Shared by every 1200 bps packet mode (APRS, packet BBS, KISS-style links).
"""

from __future__ import annotations

import dataclasses
from typing import Iterator, List, Optional, Tuple

import numpy as np

FLAG = 0x7E
FCS_GOOD = 0xF0B8  # residue of a correct X.25 frame including its FCS


def crc_ccitt(data: bytes, seed: int = 0xFFFF) -> int:
    """X.25 / HDLC FCS: reflected CRC-16-CCITT, polynomial 0x8408."""
    crc = seed
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def fcs_ok(frame: bytes) -> bool:
    """True when *frame* (contents plus its two FCS bytes) checks out."""
    return len(frame) > 2 and crc_ccitt(frame) == FCS_GOOD


def append_fcs(payload: bytes) -> bytes:
    crc = crc_ccitt(payload) ^ 0xFFFF
    return payload + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def bits_to_bytes(bits: List[int]) -> bytes:
    """LSB-first packing, as used on the wire by HDLC."""
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        value = 0
        for j in range(8):
            value |= bits[i + j] << j
        out.append(value)
    return bytes(out)


def bytes_to_bits(data: bytes) -> List[int]:
    bits: List[int] = []
    for byte in data:
        for j in range(8):
            bits.append((byte >> j) & 1)
    return bits


def stuff(bits: List[int]) -> List[int]:
    """Insert a 0 after five consecutive 1s so data can never look like a flag."""
    out: List[int] = []
    ones = 0
    for b in bits:
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 5:
            out.append(0)
            ones = 0
    return out


def unstuff(bits: List[int]) -> List[int]:
    out: List[int] = []
    ones = 0
    for b in bits:
        if ones == 5:
            ones = 0
            if b == 0:
                continue  # discard the stuffed zero
            # Six ones inside a frame is a flag or an abort; stop here.
            return out
        out.append(b)
        ones = ones + 1 if b else 0
    return out


def nrzi_encode(bits: List[int], initial: int = 0) -> List[int]:
    """NRZI: a 0 bit flips the level, a 1 bit holds it."""
    level = initial
    out: List[int] = []
    for b in bits:
        if b == 0:
            level ^= 1
        out.append(level)
    return out


def nrzi_decode(levels: np.ndarray) -> List[int]:
    arr = np.asarray(levels, dtype=np.int8)
    if arr.size == 0:
        return []
    changed = np.empty(arr.size, dtype=np.int8)
    changed[0] = 0
    changed[1:] = arr[1:] ^ arr[:-1]
    return (1 - changed).astype(int).tolist()


def find_frames(bits: List[int], min_bytes: int = 15,
                max_bytes: int = 512) -> Iterator[Tuple[int, bytes]]:
    """Yield ``(bit_offset, frame_bytes)`` for every flag-delimited frame.

    Frames whose FCS fails are still yielded; the caller decides whether to
    keep them (useful for diagnosing a marginal path).
    """
    flag = [0, 1, 1, 1, 1, 1, 1, 0]
    n = len(bits)
    positions: List[int] = []
    i = 0
    while i <= n - 8:
        if bits[i:i + 8] == flag:
            positions.append(i)
            i += 8
        else:
            i += 1
    for a, b in zip(positions, positions[1:]):
        payload = bits[a + 8:b]
        if len(payload) < min_bytes * 8:
            continue
        data = bits_to_bytes(unstuff(payload))
        if min_bytes <= len(data) <= max_bytes:
            yield (a, data)


@dataclasses.dataclass
class Ax25Frame:
    source: str
    destination: str
    path: List[str]
    control: int
    pid: Optional[int]
    info: bytes
    fcs_ok: bool

    @property
    def is_ui(self) -> bool:
        return (self.control & 0xEF) == 0x03

    def text(self) -> str:
        return self.info.decode("utf-8", errors="replace")

    def header(self) -> str:
        route = ",".join([self.destination] + self.path)
        return f"{self.source}>{route}"

    def summary(self) -> str:
        body = self.text().strip()
        return f"{self.header()}:{body}" if body else self.header()

    def to_dict(self) -> dict:
        return {
            "source": self.source, "destination": self.destination,
            "path": self.path, "control": self.control, "pid": self.pid,
            "info": self.text(), "fcs_ok": self.fcs_ok, "ui": self.is_ui,
        }


def _decode_callsign(chunk: bytes) -> Tuple[str, bool, bool]:
    """AX.25 address field -> ``(callsign-ssid, last_flag, repeated_flag)``."""
    call = "".join(chr(b >> 1) for b in chunk[:6]).strip()
    ssid_byte = chunk[6]
    ssid = (ssid_byte >> 1) & 0x0F
    last = bool(ssid_byte & 0x01)
    repeated = bool(ssid_byte & 0x80)
    text = f"{call}-{ssid}" if ssid else call
    if repeated:
        text += "*"
    return (text, last, repeated)


def _encode_callsign(call: str, last: bool = False) -> bytes:
    call = call.upper().strip().rstrip("*")
    ssid = 0
    if "-" in call:
        call, _, ssid_text = call.partition("-")
        ssid = int(ssid_text or 0)
    padded = call.ljust(6)[:6]
    out = bytearray(b << 1 for b in padded.encode("ascii"))
    out.append(0x60 | ((ssid & 0x0F) << 1) | (1 if last else 0))
    return bytes(out)


def parse_ax25(frame: bytes) -> Optional[Ax25Frame]:
    """Parse an AX.25 UI/I frame. Returns ``None`` if it is not plausible."""
    if len(frame) < 16:
        return None
    ok = fcs_ok(frame)
    body = frame[:-2] if len(frame) > 2 else frame

    addresses: List[str] = []
    idx = 0
    while idx + 7 <= len(body):
        text, last, _ = _decode_callsign(body[idx:idx + 7])
        if not text or any(c not in " -*0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ" for c in text):
            return None
        addresses.append(text)
        idx += 7
        if last:
            break
        if len(addresses) > 10:
            return None
    if len(addresses) < 2 or idx >= len(body):
        return None

    control = body[idx]
    idx += 1
    pid = None
    if (control & 0x01) == 0 or (control & 0xEF) == 0x03:  # I or UI frame
        if idx < len(body):
            pid = body[idx]
            idx += 1
    info = body[idx:]
    return Ax25Frame(
        source=addresses[1], destination=addresses[0], path=addresses[2:],
        control=control, pid=pid, info=info, fcs_ok=ok,
    )


def build_ax25(source: str, destination: str, info: bytes,
               path: Optional[List[str]] = None, control: int = 0x03,
               pid: int = 0xF0) -> bytes:
    """Build a UI frame with a valid FCS (used by tests and ``selftest``)."""
    path = path or []
    addrs = [_encode_callsign(destination), _encode_callsign(source)]
    for i, digi in enumerate(path):
        addrs.append(_encode_callsign(digi, last=(i == len(path) - 1)))
    if not path:
        addrs[-1] = _encode_callsign(source, last=True)
    payload = b"".join(addrs) + bytes([control, pid]) + info
    return append_fcs(payload)


def frame_to_bits(frame: bytes, flags_before: int = 12,
                  flags_after: int = 3) -> List[int]:
    """Frame bytes -> stuffed, flag-delimited, NRZI-encoded bit stream."""
    flag_bits = bytes_to_bits(bytes([FLAG]))
    body = stuff(bytes_to_bits(frame))
    bits = flag_bits * flags_before + body + flag_bits * flags_after
    return nrzi_encode(bits)
