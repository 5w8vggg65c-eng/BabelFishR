"""Audio devices, WAV I/O, replay, metering, calibration and safety recording."""

from __future__ import annotations

import datetime as dt
import pathlib

import numpy as np
import pytest

from babelfishr.audio.devices import (AudioDevice, backend_available,
                                      backend_status, find_device,
                                      list_input_devices)
from babelfishr.audio.meter import LevelMeter, calibrate
from babelfishr.audio.safety import SafetyRecorder
from babelfishr.audio.source import (CallbackAudioSource, ReplayAudioSource,
                                     open_source)
from babelfishr.audio.wavefile import read_wav, wav_duration, write_wav

SR = 48_000


@pytest.mark.parametrize("bit_depth,tolerance", [(16, 1e-4), (24, 1e-6), (32, 1e-8)])
def test_wav_round_trip(tmp_path, bit_depth, tolerance):
    original = 0.5 * np.sin(2 * np.pi * 440 * np.arange(SR) / SR)
    path = write_wav(str(tmp_path / "t.wav"), original, SR, bit_depth)
    restored, rate = read_wav(path)
    assert rate == SR
    assert restored.size == original.size
    assert np.max(np.abs(restored - original)) < tolerance


def test_wav_duration(tmp_path):
    path = write_wav(str(tmp_path / "t.wav"), np.zeros(SR * 2), SR)
    assert wav_duration(path) == pytest.approx(2.0)


def test_wav_writing_clips_rather_than_wrapping(tmp_path):
    path = write_wav(str(tmp_path / "t.wav"), np.array([2.0, -2.0]), SR)
    restored, _ = read_wav(path)
    assert np.all(np.abs(restored) <= 1.0)


def test_stereo_is_folded_to_mono(tmp_path):
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        frames = np.zeros(SR * 2, dtype="<i2")
        frames[0::2] = 16384   # left
        frames[1::2] = -16384  # right
        handle.writeframes(frames.tobytes())
    samples, rate = read_wav(str(path))
    assert rate == SR and samples.size == SR
    assert np.allclose(samples, 0.0, atol=1e-4)


def test_replay_source_delivers_every_sample(tmp_path):
    audio = np.sin(2 * np.pi * 440 * np.arange(SR * 2) / SR)
    path = write_wav(str(tmp_path / "t.wav"), audio, SR)
    source = ReplayAudioSource(path, block_size=1024)
    source.start()
    total = sum(block.samples.size for block in source.blocks())
    assert total == audio.size
    assert source.finished


def test_open_source_dispatches_on_the_spec(tmp_path):
    path = write_wav(str(tmp_path / "t.wav"), np.zeros(100), SR)
    assert isinstance(open_source(path), ReplayAudioSource)
    assert isinstance(open_source(f"replay:{path}"), ReplayAudioSource)


def test_block_reports_clipping():
    source = CallbackAudioSource(SR)
    source.start()
    source.push(np.ones(100))
    block = source.read()
    assert block.clipped and block.clipped_samples == 100


def test_block_timing_is_monotonic():
    source = CallbackAudioSource(SR)
    source.start()
    for _ in range(3):
        source.push(np.zeros(4800))
    offsets = [source.read().offset for _ in range(3)]
    assert offsets == sorted(offsets)
    assert offsets[1] == pytest.approx(0.1)


def test_meter_peak_hold_decays():
    meter = LevelMeter(decay_db_per_second=20.0)
    source = CallbackAudioSource(SR)
    source.start()
    source.push(0.5 * np.ones(4800))
    first = meter.update(source.read(), now=0.0)
    source.push(np.zeros(4800))
    later = meter.update(source.read(), now=1.0)
    assert later.peak_hold_dbfs < first.peak_hold_dbfs


def test_calibration_detects_a_silent_input():
    source = CallbackAudioSource(SR)
    source.start()
    for _ in range(20):
        source.push(np.zeros(4800))
    source.stop()
    result = calibrate(source, seconds=1.0)
    assert not result.ok
    assert any("silent" in w for w in result.warnings)


def test_calibration_detects_clipping():
    rng = np.random.default_rng(0)
    source = CallbackAudioSource(SR)
    source.start()
    for _ in range(20):
        source.push(np.clip(rng.normal(0, 0.9, 4800), -1, 1))
    source.stop()
    result = calibrate(source, seconds=1.0)
    assert not result.ok
    assert any("clipping" in w for w in result.warnings)


def test_calibration_accepts_healthy_hiss():
    rng = np.random.default_rng(1)
    source = CallbackAudioSource(SR)
    source.start()
    for _ in range(40):
        source.push(rng.normal(0, 0.004, 4800))
    source.stop()
    result = calibrate(source, seconds=2.0)
    assert result.ok
    assert -70 < result.recommended_threshold_dbfs < -20


def test_safety_recorder_is_off_by_default(tmp_path):
    recorder = SafetyRecorder(str(tmp_path))
    source = CallbackAudioSource(SR)
    source.start()
    source.push(np.ones(100))
    assert recorder.feed(source.read()) is None
    assert "off" in recorder.describe()


def test_safety_recorder_enforces_a_minimum_chunk_length(tmp_path):
    """Tiny chunks would mean thousands of files; the floor is deliberate."""
    assert SafetyRecorder(str(tmp_path), chunk_seconds=0.1).chunk_seconds == 5.0


def test_safety_recorder_chunks_and_prunes(tmp_path):
    recorder = SafetyRecorder(str(tmp_path), chunk_seconds=5.0, enabled=True,
                              retention_hours=24)
    source = CallbackAudioSource(8000)
    source.start()
    written = []
    for _ in range(24):  # 24 x 4000 samples at 8 kHz = 12 s, so two chunks
        source.push(np.full(4000, 0.1))
        chunk = recorder.feed(source.read())
        if chunk:
            written.append(chunk)
    final = recorder.close()
    if final:
        written.append(final)
    assert len(written) >= 2
    assert all(pathlib.Path(c.path).exists() for c in written)

    written[0].started_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    assert recorder.enforce_retention() >= 1
    assert not pathlib.Path(written[0].path).exists()


def test_device_helpers_survive_a_missing_backend():
    assert isinstance(list_input_devices(), list)
    assert isinstance(backend_available(), bool)
    assert backend_status()
    if not list_input_devices():
        assert find_device(None) is None


def test_device_describe_is_informative():
    device = AudioDevice(index=2, name="USB Audio CODEC", max_input_channels=2,
                         default_sample_rate=48_000.0, host_api="Core Audio",
                         is_default=True)
    text = device.describe()
    assert "USB Audio CODEC" in text and "default" in text and "Core Audio" in text
