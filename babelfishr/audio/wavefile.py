"""WAV reading and writing with no third-party dependency.

The original capture is written here byte-for-byte as received, before any
resampling or normalisation.  Only the stdlib ``wave`` module is used so a
minimal install can still record and play back.
"""

from __future__ import annotations

import pathlib
import wave
from typing import Tuple

import numpy as np


def write_wav(path: str, samples: np.ndarray, sample_rate: int,
              bit_depth: int = 16) -> str:
    """Write mono float samples (-1..1) as PCM WAV. Returns the path."""
    if bit_depth not in (16, 24, 32):
        raise ValueError("bit_depth must be 16, 24 or 32")
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    raw, width = _encode(samples, bit_depth)
    with wave.open(str(target), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(width)
        handle.setframerate(int(sample_rate))
        handle.writeframes(raw)
    return str(target)


def write_wav_atomic(path: str, samples: np.ndarray, sample_rate: int,
                     bit_depth: int = 16) -> str:
    """Write a WAV into an *existing* folder, atomically.

    Creates no folder at all: the samples go to a uniquely named temporary
    file in the target's folder, are flushed to disk, and the file is then
    moved into place with ``os.replace``. On any failure only that
    temporary file is removed. Used for a Session's chosen save path, where
    creating a missing parent could recreate an ejected drive's path on the
    internal disk. Returns the path.
    """
    import os
    import tempfile

    if bit_depth not in (16, 24, 32):
        raise ValueError("bit_depth must be 16, 24 or 32")
    target = pathlib.Path(path)
    raw, width = _encode(samples, bit_depth)
    fd, temp = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".wav.tmp",
                                dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            with wave.open(stream, "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(width)
                handle.setframerate(int(sample_rate))
                handle.writeframes(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
    return str(target)


def _encode(samples: np.ndarray, bit_depth: int) -> Tuple[bytes, int]:
    """PCM bytes and sample width for mono float samples (-1..1)."""
    data = np.asarray(samples, dtype=np.float64).ravel()
    # Clip rather than wrap: a wrapped sample sounds like a gunshot.
    data = np.clip(data, -1.0, 1.0)

    if bit_depth == 16:
        raw = (data * 32767.0).astype("<i2").tobytes()
        width = 2
    elif bit_depth == 24:
        ints = (data * 8388607.0).astype("<i4")
        raw = ints.view("<u1").reshape(-1, 4)[:, :3].tobytes()
        width = 3
    else:
        raw = (data * 2147483647.0).astype("<i4").tobytes()
        width = 4
    return raw, width


def read_wav(path: str) -> Tuple[np.ndarray, int]:
    """Read a WAV file to mono float samples. Returns ``(samples, rate)``."""
    with wave.open(str(path), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        rate = handle.getframerate()
        raw = handle.readframes(handle.getnframes())

    if width == 1:  # unsigned 8-bit
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    elif width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        packed = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        expanded = np.zeros((packed.shape[0], 4), dtype=np.uint8)
        expanded[:, 1:] = packed  # shift into the high bytes to keep the sign
        data = expanded.view("<i4").ravel().astype(np.float64) / 2147483648.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width: {width} bytes")

    if channels > 1:
        usable = (data.size // channels) * channels
        data = data[:usable].reshape(-1, channels).mean(axis=1)
    return (data, int(rate))


def wav_duration(path: str) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate() or 1)
