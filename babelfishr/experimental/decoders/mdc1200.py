"""MDC-1200 (Motorola) 1200 bps FFSK data burst decoder.

MDC-1200 is the short data burst heard at the start or end of a key-up on many
commercial and public-safety analogue systems.  It carries the transmitting
radio's unit ID plus an opcode (PTT ID, emergency, radio check, ...).

Wire format: a 24-bit clock-recovery preamble, a 40-bit sync word
``07 09 2A 44 6F`` (MSB first), then 112 payload bits column-interleaved over a
16x7 grid, differentially encoded, at 1200 baud with mark 1200 Hz / space
1800 Hz.

The framing above is well documented; the exact interleave transpose and CRC
convention vary between published descriptions, so this decoder tries the
plausible variants and reports full confidence only when a CRC checks out.  A
burst that is clearly MDC but does not validate is still reported (with reduced
confidence and the raw payload) rather than silently dropped.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np

from ..results import DecodeResult
from .afsk import demodulate, recover_bits
from .base import BaseDecoder, register

MARK_HZ = 1200.0
SPACE_HZ = 1800.0
BAUD = 1200.0
SYNC_BYTES = bytes([0x07, 0x09, 0x2A, 0x44, 0x6F])
PAYLOAD_BITS = 112

#: Common opcodes. Systems can and do define their own.
OPCODES: Dict[Tuple[int, int], str] = {
    (0x01, 0x80): "PTT ID (pre)",
    (0x01, 0x00): "PTT ID (post)",
    (0x00, 0x80): "PTT ID",
    (0x11, 0x8A): "radio check request",
    (0x11, 0x0A): "radio check reply",
    (0x22, 0x06): "emergency",
    (0x2B, 0x0C): "emergency acknowledge",
    (0x35, 0x89): "status request",
    (0x06, 0x01): "radio enable",
    (0x06, 0x00): "radio disable",
    (0x63, 0x85): "call alert",
}


def _sync_bits() -> np.ndarray:
    bits = []
    for byte in SYNC_BYTES:
        for i in range(7, -1, -1):  # MSB first
            bits.append((byte >> i) & 1)
    return np.array(bits, dtype=np.int8)


SYNC_PATTERN = _sync_bits()


def differential_decode(symbols: np.ndarray) -> np.ndarray:
    """XOR of successive symbols; MDC is self-clocking/differential."""
    arr = np.asarray(symbols, dtype=np.int8)
    if arr.size < 2:
        return np.zeros(0, dtype=np.int8)
    return (arr[1:] ^ arr[:-1] ^ 1).astype(np.int8)


def find_sync(bits: np.ndarray, max_errors: int = 2) -> List[int]:
    """Offsets just past every sync word occurrence."""
    n, m = bits.size, SYNC_PATTERN.size
    if n < m:
        return []
    windows = np.lib.stride_tricks.sliding_window_view(bits, m)
    errors = np.count_nonzero(windows != SYNC_PATTERN, axis=1)
    return [int(i) + m for i in np.flatnonzero(errors <= max_errors)]


def _pack_msb(bits: np.ndarray) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        value = 0
        for j in range(8):
            value = (value << 1) | int(bits[i + j])
        out.append(value)
    return bytes(out)


def deinterleave(bits: np.ndarray, rows: int = 7, cols: int = 16) -> List[bytes]:
    """Undo the 16x7 column interleave; both transposes are returned."""
    usable = bits[:rows * cols]
    if usable.size < rows * cols:
        return []
    a = usable.reshape(cols, rows).T.ravel()
    b = usable.reshape(rows, cols).T.ravel()
    return [_pack_msb(a), _pack_msb(b), _pack_msb(usable)]


def _crc16_ccitt_msb(data: bytes, seed: int = 0x0000) -> int:
    crc = seed
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _crc16_ccitt_lsb(data: bytes, seed: int = 0xFFFF) -> int:
    crc = seed
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def validate(block: bytes) -> Optional[Dict[str, object]]:
    """Check a candidate 7-byte MDC block, returning its fields if the CRC fits."""
    if len(block) < 6:
        return None
    op, arg, unit_hi, unit_lo = block[0], block[1], block[2], block[3]
    given = (block[4] << 8) | block[5]
    given_swapped = (block[5] << 8) | block[4]
    body = block[:4]
    for name, computed in (
        ("ccitt-msb", _crc16_ccitt_msb(body)),
        ("ccitt-msb-ffff", _crc16_ccitt_msb(body, 0xFFFF)),
        ("ccitt-lsb", _crc16_ccitt_lsb(body)),
    ):
        if computed in (given, given_swapped):
            return {
                "op": op, "arg": arg, "unit_id": f"{unit_hi:02X}{unit_lo:02X}",
                "crc_variant": name, "crc": given, "valid": True,
            }
    return None


class Mdc1200Decoder(BaseDecoder):
    id = "mdc1200"
    name = "MDC-1200"
    description = "Motorola 1200 bps FFSK unit-ID / emergency data burst"
    sample_rate = 9600

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        out: List[DecodeResult] = []
        seen = set()
        nrz = demodulate(audio, sample_rate, MARK_HZ, SPACE_HZ, BAUD)
        if nrz.size == 0:
            return []
        for polarity in (1.0, -1.0):
            symbols = recover_bits(nrz * polarity, sample_rate, BAUD)
            for bits in (differential_decode(symbols), symbols):
                for start in find_sync(bits):
                    payload = bits[start:start + PAYLOAD_BITS]
                    if payload.size < PAYLOAD_BITS:
                        continue
                    result = self._interpret(payload, len(audio) / float(sample_rate))
                    key = (result.data.get("unit_id"), result.data.get("op"),
                           result.data.get("raw_hex"))
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(result)
        # Prefer CRC-validated decodes; drop unvalidated ones if any validated.
        validated = [r for r in out if r.data.get("valid")]
        return validated or out[:1]

    def _interpret(self, payload: np.ndarray, duration: float) -> DecodeResult:
        candidates = deinterleave(payload)
        for block in candidates:
            fields = validate(block)
            if fields is None:
                continue
            op, arg = int(fields["op"]), int(fields["arg"])
            meaning = OPCODES.get((op, arg), f"op 0x{op:02X} arg 0x{arg:02X}")
            return DecodeResult(
                decoder=self.id,
                label=f"MDC-1200 unit {fields['unit_id']}: {meaning}",
                confidence=1.0, duration=duration,
                data={**fields, "meaning": meaning, "raw_hex": block.hex()},
            )
        raw = candidates[0].hex() if candidates else ""
        return DecodeResult(
            decoder=self.id, label="MDC-1200 burst (unvalidated)",
            confidence=0.45, duration=duration,
            data={"valid": False, "raw_hex": raw, "best_effort": True,
                  "note": "sync matched but no CRC variant validated"},
        )


def synthesize(op: int, arg: int, unit_id: int, sample_rate: int = 9600,
               amplitude: float = 0.5, preamble_bits: int = 24) -> np.ndarray:
    """Render an MDC-1200 burst (tests / selftest).

    Uses the column-interleave and MSB CRC-CCITT variant; the decoder accepts
    any of the variants it knows about.
    """
    body = bytes([op & 0xFF, arg & 0xFF, (unit_id >> 8) & 0xFF, unit_id & 0xFF])
    crc = _crc16_ccitt_msb(body)
    block = body + bytes([(crc >> 8) & 0xFF, crc & 0xFF]) + b"\x00"

    data_bits = []
    for byte in block:
        for i in range(7, -1, -1):
            data_bits.append((byte >> i) & 1)
    data_bits += [0] * (PAYLOAD_BITS - len(data_bits))
    # Inverse of deinterleave()'s first candidate: rows x cols -> cols x rows.
    interleaved = np.array(data_bits, dtype=np.int8).reshape(7, 16).T.ravel()

    preamble = [i % 2 for i in range(preamble_bits)]
    bits = np.concatenate([np.array(preamble, dtype=np.int8), SYNC_PATTERN, interleaved])

    # Differential encoding, inverted so the decoder's XOR recovers the bits.
    level = 0
    symbols = []
    for b in bits:
        level ^= (int(b) ^ 1)
        symbols.append(level)

    sps = sample_rate / BAUD
    n = int(round(len(symbols) * sps))
    idx = np.minimum((np.arange(n) / sps).astype(int), len(symbols) - 1)
    freqs = np.where(np.array(symbols, dtype=np.int8)[idx] == 1, MARK_HZ, SPACE_HZ)
    phase = 2 * np.pi * np.cumsum(freqs) / sample_rate
    return amplitude * np.sin(phase)


register(Mdc1200Decoder())
