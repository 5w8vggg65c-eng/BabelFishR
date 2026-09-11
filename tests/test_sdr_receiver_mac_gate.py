"""Regressions that must hold on the Mac build gate as well as here: SDR++
process detection from ps output whose argument boundaries are gone (a
--root under "Application Support" arrives as two words), and the two
timing repairs (the idle filler and the stand-in's pacing) under forced
late wakes. Nothing here needs a Mac: the Darwin path is exercised at the
subprocess boundary with owned local processes and ps-shaped output.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import subprocess
import sys
import threading
import time
import types

import numpy as np
import pytest

from babelfishr.receiver import ReceiverController, ReceiverUnavailable
from babelfishr.receiver import sdrpp as sdrpp_module
from babelfishr.receiver import stream as stream_module
from babelfishr.receiver.sdrpp import SdrppConfigurator, sdrpp_processes
from babelfishr.receiver.stream import DecodedVoiceSource, StreamBoundary, TuningState

STUBS = pathlib.Path(__file__).parent / "stubs"
FAKE_SDRPP = str(STUBS / "fake_sdrpp.py")
FAKE_DSD = str(STUBS / "fake_dsd_stream.py")
RATE = 48000


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=10.0, step=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def sdrpp_log() -> str:
    path = os.environ.get("FAKE_SDRPP_LOG", "")
    return pathlib.Path(path).read_text() if path and os.path.exists(path) else ""


def snapshot(root: pathlib.Path):
    return {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in root.glob("*.json")}


def ps_like(processes):
    """What `ps -axo pid=,comm=,args=` prints on macOS: the arguments joined
    by single spaces, their boundaries gone (adv_cmds ps/print.c)."""
    lines = []
    for pid, argv in processes:
        lines.append(f"{pid:5d} {argv[0]} {' '.join(argv)}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def darwin_ps(monkeypatch):
    """Run the Darwin branch of sdrpp_processes against ps output built from
    the processes the test owns, on any host."""
    owned = []
    monkeypatch.setattr(sdrpp_module.platform, "system", lambda: "Darwin")
    real_run = sdrpp_module.subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:1] == ["ps"]:
            return subprocess.CompletedProcess(command, 0, stdout=ps_like(owned), stderr="")
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(sdrpp_module.subprocess, "run", fake_run)
    return owned


@pytest.fixture
def receiver_config(config, tmp_path, monkeypatch):
    config.receiver.sdrpp_path = FAKE_SDRPP
    config.receiver.rigctl_port = free_port()
    config.receiver.audio_port = free_port()
    config.receiver.frequency_hz = 155.1e6
    config.receiver.connect_timeout_s = 10.0
    config.analysis.dsd_path = FAKE_DSD
    monkeypatch.setenv("FAKE_SDRPP_LOG", str(tmp_path / "sdrpp.log"))
    monkeypatch.delenv("FAKE_SDRPP_WAV", raising=False)
    for name in ("FAKE_SDRPP_NO_RIGCTL", "FAKE_SDRPP_DROP_AUDIO_AFTER", "FAKE_SDRPP_AUDIO_STOP_AFTER",
                 "FAKE_SDRPP_RIGCTL_DELAY", "FAKE_DSD_SIGTERM_DELAY", "FAKE_SDRPP_TUNE_FILE"):
        monkeypatch.delenv(name, raising=False)
    return config


def start_their_sdrpp(root: pathlib.Path, receiver_config):
    """The operator's own SDR++ (the stand-in) on *root*, without rigctl."""
    SdrppConfigurator(root).ensure(audio_host="127.0.0.1", audio_port=receiver_config.receiver.audio_port,
                                   sample_rate=48000, rigctl_host="127.0.0.1",
                                   rigctl_port=receiver_config.receiver.rigctl_port)
    env = dict(os.environ, FAKE_SDRPP_NO_RIGCTL="1")
    argv = [sys.executable, FAKE_SDRPP, "--root", str(root)]
    process = subprocess.Popen(argv, env=env)
    assert wait_for(lambda: "rigctl not started" in sdrpp_log(), timeout=10)
    return process, argv


# ---- process detection from ps output --------------------------------------------------------

def test_a_spaced_root_is_rebuilt_from_ps_and_the_running_sdrpp_is_protected(darwin_ps, receiver_config,
                                                                             tmp_path):
    root = tmp_path / "Library" / "Application Support" / "sdrpp"
    root.mkdir(parents=True)
    receiver_config.receiver.sdrpp_root = str(root)
    theirs, argv = start_their_sdrpp(root, receiver_config)
    darwin_ps.append((theirs.pid, argv))
    before = snapshot(root)
    try:
        assert sdrpp_processes(FAKE_SDRPP, str(root)) == [theirs.pid], \
            "the root split at its space was taken for a different receiver's"
        controller = ReceiverController(receiver_config)
        with pytest.raises(ReceiverUnavailable) as failure:
            controller.ensure_sdrpp(timeout=3.0)
        assert "Rigctl Server" in str(failure.value)
        assert controller._process is None, "a second SDR++ was started on the same receiver"
        assert snapshot(root) == before, "the running SDR++'s settings were rewritten"
        assert theirs.poll() is None
        controller.shutdown()
    finally:
        theirs.terminate()
        theirs.wait(timeout=5)


def test_a_plain_root_is_detected_and_a_different_root_is_not(darwin_ps, receiver_config, tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    receiver_config.receiver.sdrpp_root = str(root)
    theirs, argv = start_their_sdrpp(root, receiver_config)
    darwin_ps.append((theirs.pid, argv))
    # A second SDR++ on a known different root: not this receiver's.
    darwin_ps.append((theirs.pid + 100000, [sys.executable, FAKE_SDRPP, "--root", str(other)]))
    try:
        assert sdrpp_processes(FAKE_SDRPP, str(root)) == [theirs.pid]
        assert sdrpp_processes(FAKE_SDRPP, str(other)) == [theirs.pid + 100000]
        assert sdrpp_processes(FAKE_SDRPP, str(tmp_path / "nowhere")) == []
        # The SDR++.app binary is named sdrpp; a default launch has no --root.
        darwin_ps.append((424242, ["/Applications/SDR++.app/Contents/MacOS/sdrpp", "--autostart"]))
        assert 424242 in sdrpp_processes("", str(root))
        controller = ReceiverController(receiver_config)
        with pytest.raises(ReceiverUnavailable):
            controller.ensure_sdrpp(timeout=3.0)
        assert controller._process is None
    finally:
        theirs.terminate()
        theirs.wait(timeout=5)


def test_a_root_that_cannot_be_established_protects_the_running_sdrpp(darwin_ps, tmp_path):
    # Words after --root that reach no existing directory: unknowable from ps.
    darwin_ps.append((777, ["/Applications/SDR++.app/Contents/MacOS/sdrpp", "--root",
                            "/Volumes/Some", "Disk/that", "is", "gone", "--autostart"]))
    assert sdrpp_processes("", str(tmp_path)) == [777], \
        "an unreadable root was taken as proof that this SDR++ is someone else's"
    assert sdrpp_module._root_from_flattened_words(["/Volumes/Some", "Disk/that"]) is None
    spaced = tmp_path / "Application Support" / "sdrpp"
    spaced.mkdir(parents=True)
    words = str(spaced).split(" ") + ["--autostart"]
    assert sdrpp_module._root_from_flattened_words(words) == str(spaced)


# ---- the two timing repairs under forced late wakes --------------------------------------------

REAL_SLEEP = time.sleep


def forced_sleep(seconds_each):
    """A sleep() that always wakes late, whatever was asked for - the loaded
    machine the Mac gate turned out to be."""
    def sleep(_requested):
        REAL_SLEEP(seconds_each)
    return sleep


def test_the_idle_filler_covers_elapsed_time_when_its_wakes_come_late(monkeypatch):
    source = DecodedVoiceSource("dsd-neo", "127.0.0.1", 1, TuningState())
    source._running = True
    source._start_mono = time.monotonic()
    source._timeline = 0.0
    monkeypatch.setattr(stream_module.time, "sleep", forced_sleep(0.5))
    filler = threading.Thread(target=source._fill_silence, daemon=True)
    filler.start()
    began = time.monotonic()
    REAL_SLEEP(2.5)
    source._running = False
    filler.join(timeout=2.0)
    elapsed = time.monotonic() - began
    covered = 0.0
    while True:
        block = source.read(timeout=0.0)
        if block is None:
            break
        if not isinstance(block, StreamBoundary):
            assert float(np.max(np.abs(block.samples))) == 0.0
            covered += block.samples.size / source.sample_rate
    assert covered >= elapsed - 0.6, f"covered {covered:.2f}s of {elapsed:.2f}s with 0.5 s wakes"
    assert abs(source._timeline - covered) < 1e-3          # frames are whole samples


def test_the_fake_receiver_streams_at_the_sink_rate_when_its_wakes_come_late(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("fake_sdrpp_for_pacing", FAKE_SDRPP)
    fake = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake)
    monkeypatch.setattr(fake.time, "sleep", forced_sleep(0.5))
    monkeypatch.delenv("FAKE_SDRPP_WAV", raising=False)
    port = free_port()
    state = fake.State()
    state.playing = True
    threading.Thread(target=fake.audio_sink, args=("127.0.0.1", port, RATE, state), daemon=True).start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and socket.socket().connect_ex(("127.0.0.1", port)) != 0:
        REAL_SLEEP(0.05)
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.settimeout(0.2)
    # How long does 2.5 s of audio take to arrive when every wake is 0.5 s
    # late? Paced by the clock: about 2.5 s plus one late wake. Paced by
    # sleep (the previous stand-in): 125 pieces × 0.5 s, over a minute.
    wanted = int(2.5 * RATE) * 2
    received = 0
    began = time.monotonic()
    while received < wanted and time.monotonic() - began < 12.0:
        try:
            data = client.recv(65536)
        except socket.timeout:
            continue
        if not data:
            break
        received += len(data)
    took = time.monotonic() - began
    client.close()
    assert received >= wanted, f"only {received / (RATE * 2):.2f}s of audio arrived in {took:.1f}s"
    assert took <= 3.6, f"2.5 s of audio took {took:.2f}s to arrive with 0.5 s wakes"
