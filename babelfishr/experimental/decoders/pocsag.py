"""POCSAG (CCIR Radiopaging Code No. 1) decoder.

POCSAG is direct 2-FSK on the carrier, so it appears as a *baseband* NRZ square
wave after FM demodulation - not as audio tones.  Feed it discriminator-tapped
audio or the SDR path; a radio's speaker output usually rolls off the low
frequencies too hard for reliable copy at 512 baud.

Structure: 576-bit alternating preamble, then batches of a 32-bit frame sync
word (0x7CD215D8) followed by 8 frames of 2 codewords.  Codewords are
BCH(31,21) plus an even parity bit.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np

from ...dsp.filters import moving_average
from ..results import DecodeResult
from .base import BaseDecoder, register

SYNC_WORD = 0x7CD215D8
IDLE_WORD = 0x7A89C197
BATCH_CODEWORDS = 16
BCH_POLY = 0b11101101001  # x^10+x^9+x^8+x^6+x^5+x^3+1, the POCSAG BCH(31,21)

NUMERIC_MAP = "0123456789*U -)("

FUNCTION_NAMES = {0: "A (tone/numeric)", 1: "B", 2: "C", 3: "D (alphanumeric)"}


@dataclasses.dataclass
class Message:
    address: int
    function: int
    text: str
    numeric: str
    kind: str
    codewords: int
    errors: int

    @property
    def content(self) -> str:
        return self.text if self.kind == "alpha" else self.numeric

    def to_dict(self) -> Dict[str, object]:
        return {
            "address": self.address, "ric": self.address,
            "function": self.function,
            "function_name": FUNCTION_NAMES.get(self.function, str(self.function)),
            "kind": self.kind, "text": self.text, "numeric": self.numeric,
            "codewords": self.codewords, "errors": self.errors,
        }


def bch_syndrome(codeword: int) -> int:
    """Remainder of the 31 message+parity bits under the BCH generator."""
    value = codeword >> 1  # strip the even-parity bit
    for shift in range(30, 9, -1):
        if value & (1 << shift):
            value ^= BCH_POLY << (shift - 10)
    return value & 0x3FF


def parity_ok(codeword: int) -> bool:
    return bin(codeword).count("1") % 2 == 0


def correct(codeword: int) -> Tuple[int, int]:
    """Correct up to 2 bit errors. Returns ``(codeword, errors)``; -1 if hopeless."""
    if bch_syndrome(codeword) == 0 and parity_ok(codeword):
        return (codeword, 0)
    for i in range(32):
        candidate = codeword ^ (1 << i)
        if bch_syndrome(candidate) == 0 and parity_ok(candidate):
            return (candidate, 1)
    for i in range(32):
        for j in range(i + 1, 32):
            candidate = codeword ^ (1 << i) ^ (1 << j)
            if bch_syndrome(candidate) == 0 and parity_ok(candidate):
                return (candidate, 2)
    return (-1, 3)


def bch_encode(data21: int) -> int:
    """21 data bits -> a full 32-bit POCSAG codeword with BCH + parity."""
    value = (data21 & 0x1FFFFF) << 10
    remainder = value
    for shift in range(30, 9, -1):
        if remainder & (1 << shift):
            remainder ^= BCH_POLY << (shift - 10)
    word = (value | (remainder & 0x3FF)) << 1
    if bin(word).count("1") % 2:
        word |= 1
    return word


def bits_from_audio(audio: np.ndarray, sample_rate: int, baud: float) -> np.ndarray:
    """Slice baseband NRZ into hard bits with simple mid-bit sampling."""
    x = np.asarray(audio, dtype=np.float64)
    if x.size < 64:
        return np.zeros(0, dtype=np.int8)
    # Remove the DC/slow wander an FM discriminator adds, then slice at zero.
    baseline = moving_average(x, max(3, int(sample_rate / max(baud, 1) * 20)))
    centred = x - baseline
    sps = sample_rate / baud
    sliced = (centred > 0).astype(np.int8)

    out: List[int] = []
    phase = sps / 2.0
    previous = sliced[0]
    for level in sliced:
        phase -= 1.0
        if phase <= 0.0:
            out.append(int(level))
            phase += sps
        if level != previous:
            phase += (sps / 2.0 - phase) * 0.3
            previous = level
    return np.array(out, dtype=np.int8)


def _words_from_bits(bits: np.ndarray, start: int, count: int) -> List[int]:
    words = []
    for k in range(count):
        chunk = bits[start + k * 32:start + (k + 1) * 32]
        if chunk.size < 32:
            break
        value = 0
        for b in chunk:  # MSB first on the wire
            value = (value << 1) | int(b)
        words.append(value)
    return words


def find_sync_offsets(bits: np.ndarray, max_errors: int = 2) -> List[int]:
    if bits.size < 32:
        return []
    pattern = np.array([(SYNC_WORD >> i) & 1 for i in range(31, -1, -1)], dtype=np.int8)
    windows = np.lib.stride_tricks.sliding_window_view(bits, 32)
    errors = np.count_nonzero(windows != pattern, axis=1)
    return [int(i) for i in np.flatnonzero(errors <= max_errors)]


def decode_bitstream(bits: np.ndarray) -> List[Message]:
    """Walk batches, assembling address + message codewords into pages."""
    messages: List[Message] = []
    current: Optional[dict] = None

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        text, numeric = _render(current["payload"])
        kind = "alpha" if current["function"] == 3 else "numeric"
        if not text.strip() and kind == "alpha":
            kind = "numeric"
        messages.append(Message(
            address=current["address"], function=current["function"],
            text=text, numeric=numeric, kind=kind,
            codewords=current["codewords"], errors=current["errors"],
        ))
        current = None

    for offset in find_sync_offsets(bits):
        words = _words_from_bits(bits, offset + 32, BATCH_CODEWORDS)
        if len(words) < BATCH_CODEWORDS:
            continue
        for index, raw in enumerate(words):
            fixed, errors = correct(raw)
            if fixed < 0:
                continue
            if fixed == IDLE_WORD:
                flush()
                continue
            if fixed & 0x80000000:  # message codeword
                if current is None:
                    continue
                current["payload"].extend(
                    (fixed >> bit) & 1 for bit in range(30, 10, -1))
                current["codewords"] += 1
                current["errors"] += errors
            else:  # address codeword
                flush()
                address = ((fixed >> 13) & 0x3FFFF) << 3 | (index // 2)
                current = {
                    "address": address, "function": (fixed >> 11) & 0x03,
                    "payload": [], "codewords": 1, "errors": errors,
                }
    flush()
    return messages


def _render(payload: List[int]) -> Tuple[str, str]:
    """Payload bits -> (alphanumeric text, numeric text)."""
    text_chars: List[str] = []
    for i in range(0, len(payload) - 6, 7):
        value = 0
        for j, bit in enumerate(payload[i:i + 7]):
            value |= bit << j  # each character is sent LSB first
        if value == 0:
            continue
        text_chars.append(chr(value) if 32 <= value < 127 else
                          ("\n" if value in (10, 13) else ""))
    numeric_chars: List[str] = []
    for i in range(0, len(payload) - 3, 4):
        value = 0
        for j, bit in enumerate(payload[i:i + 4]):
            value |= bit << j
        numeric_chars.append(NUMERIC_MAP[value & 0x0F])
    return ("".join(text_chars), "".join(numeric_chars).rstrip(" "))


class PocsagDecoder(BaseDecoder):
    id = "pocsag"
    name = "POCSAG"
    description = "Pager protocol at 512 / 1200 / 2400 baud"
    sample_rate = 22050

    bauds: Iterable[float] = (512.0, 1200.0, 2400.0)

    def decode(self, audio: np.ndarray, sample_rate: int) -> List[DecodeResult]:
        out: List[DecodeResult] = []
        seen = set()
        duration = len(audio) / float(sample_rate)
        for baud in self.bauds:
            for polarity in (1.0, -1.0):
                bits = bits_from_audio(np.asarray(audio) * polarity, sample_rate, baud)
                if bits.size < 64:
                    continue
                for msg in decode_bitstream(bits):
                    key = (msg.address, msg.content)
                    if key in seen:
                        continue
                    seen.add(key)
                    body = msg.content.strip()
                    label = f"POCSAG{int(baud)} RIC {msg.address}"
                    if body:
                        label += f": {body}"
                    confidence = 1.0 if msg.errors == 0 else max(0.5, 1.0 - 0.15 * msg.errors)
                    out.append(DecodeResult(
                        decoder=self.id, label=label, confidence=confidence,
                        duration=duration,
                        data={**msg.to_dict(), "baud": int(baud),
                              "inverted": polarity < 0},
                    ))
        return out


def _pending_address(queue: List[int], addr_word: int) -> bool:
    """True while the address codeword is still waiting for its frame slot."""
    return bool(queue) and queue[0] == addr_word


def synthesize(address: int, message: str, baud: float = 1200.0,
               sample_rate: int = 22050, function: int = 3,
               amplitude: float = 0.5, preamble_bits: int = 576) -> np.ndarray:
    """Render a single-page POCSAG transmission as baseband NRZ."""
    bits: List[int] = [(i + 1) % 2 for i in range(preamble_bits)]

    def push_word(word: int) -> None:
        bits.extend((word >> i) & 1 for i in range(31, -1, -1))

    frame_index = address & 0x07
    # 21 data bits of an address codeword: bit20 = 0 (address), bits 19..2 the
    # 18 high address bits, bits 1..0 the function code.
    addr_word = bch_encode(((address >> 3) & 0x3FFFF) << 2 | (function & 0x03))

    payload_bits: List[int] = []
    for ch in message:
        value = ord(ch) & 0x7F
        payload_bits.extend((value >> j) & 1 for j in range(7))
    while len(payload_bits) % 20:
        payload_bits.append(0)

    msg_words = []
    for i in range(0, len(payload_bits), 20):
        value = 0
        for bit in payload_bits[i:i + 20]:
            value = (value << 1) | bit
        msg_words.append(bch_encode((1 << 20) | value))

    # Lay the page out across as many batches as the message needs: a batch is
    # a sync word plus 16 codewords, and the address must land in its own frame.
    queue = [addr_word] + msg_words
    slot = frame_index * 2
    index = 0
    while queue or index % BATCH_CODEWORDS:
        if index % BATCH_CODEWORDS == 0:
            push_word(SYNC_WORD)
        position = index % BATCH_CODEWORDS
        if queue and (index >= slot or not _pending_address(queue, addr_word)):
            push_word(queue.pop(0))
        else:
            push_word(IDLE_WORD)
        index += 1
    # Trailing idle batch so the decoder sees the end of the message.
    push_word(SYNC_WORD)
    for _ in range(BATCH_CODEWORDS):
        push_word(IDLE_WORD)

    sps = sample_rate / baud
    n = int(round(len(bits) * sps))
    idx = np.minimum((np.arange(n) / sps).astype(int), len(bits) - 1)
    nrz = np.where(np.array(bits, dtype=np.int8)[idx] == 1, 1.0, -1.0)
    # Gentle shaping: real FSK is band-limited, not a perfect square wave.
    return amplitude * moving_average(nrz, max(2, int(sps / 4)))


register(PocsagDecoder())
