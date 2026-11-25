"""Static is told apart from other unintelligible sound.

Positive acoustic evidence from the recording itself - stationary,
broadband, noise-like, above a quiet floor - and no credible speech. Never
from the speech engine's silence probability alone. Silence, a steady tone,
a digital signal, crackle and speech mixed with static are not static.
Audible speech, an operator's correction and a Retry-as-speech confirmation
win. Older rows carry no measurement and stay "Audio unintelligible".
Generated audio only.
"""

from __future__ import annotations

import csv
import io
import os

import numpy as np
import pytest
from numpy.fft import irfft, rfft, rfftfreq

from babelfishr.export import to_csv, to_markdown
from babelfishr.models import ProcessingState, Transmission
from babelfishr.static import static_evidence
from babelfishr.testing import silence, speech_like, tone


def radio_static(seconds, sr, dbfs=-30.0, seed=1):
    rng = np.random.default_rng(seed)
    x = rfft(rng.normal(0, 1, int(seconds * sr)))
    f = rfftfreq(int(seconds * sr), 1 / sr)
    x[(f < 300) | (f > 3400)] = 0
    y = irfft(x, int(seconds * sr))
    return y / np.sqrt(np.mean(y ** 2)) * 10 ** (dbfs / 20)


@pytest.mark.parametrize("sr", [16000, 48000])
def test_only_stationary_broadband_noise_is_static(sr):
    rng = np.random.default_rng(2)
    t = np.arange(4 * sr) / sr
    cases = {
        "white hiss": (rng.normal(0, 0.05, 4 * sr), "static"),
        "receiver static": (radio_static(4, sr), "static"),
        "speech": (speech_like(4, sr, level_dbfs=-20), "not static"),
        "steady tone": (tone(4, 1000, sr) * 0.3, "not static"),
        "pure silence": (np.zeros(4 * sr), "not static"),
        "quiet room floor": (silence(4, sr), "not static"),
        "4-level digital": (rng.choice([-0.3, -0.1, 0.1, 0.3], 4 * sr // 10)
                            .repeat(10)[:4 * sr], "not static"),
        "FSK": (0.3 * np.sign(np.sin(2 * np.pi * 1200 * t
                                     + 3 * np.sin(2 * np.pi * 50 * t))), "not static"),
        "speech mixed with static": (radio_static(4, sr) * 0.5
                                     + speech_like(4, sr, level_dbfs=-18), "not static"),
    }
    for name, (audio, verdict) in cases.items():
        assert static_evidence(audio, sr)["verdict"] == verdict, name


def _tx(**fields):
    base = dict(session_id="s", duration=4.0, state=ProcessingState.COMPLETE,
                signal_metadata={"static_check": {"verdict": "static"}})
    base.update(fields)
    return Transmission(**base)


def test_static_needs_the_absence_of_credible_speech():
    assert _tx(transcript="").is_static
    assert _tx(transcript="Thanks for watching!",            # a guess for the hiss
               signal_metadata={"static_check": {"verdict": "static"},
                                "speech_check": {"verdict": "likely_noise"}}).is_static
    assert not _tx(transcript="Unit four, copy, over.").is_static, "speech wins"
    assert not _tx(transcript="", transcript_correction="faint call, unit four").is_static
    assert not _tx(transcript="", signal_metadata={
        "static_check": {"verdict": "static"},
        "speech_confirmed": {"source_language": "en"}}).is_static
    assert not _tx(transcript="", signal_metadata={
        "static_check": {"verdict": "static"},
        "speech_check": {"speech_seconds": 1.2}}).is_static, "the detector heard speech"
    old = Transmission(session_id="s", transcript="", state=ProcessingState.COMPLETE,
                       signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    assert not old.is_static, "an unmeasured older row is never called static"


def test_static_is_said_in_readable_exports_and_kept_apart_from_unintelligible():
    static = _tx(id="static", transcript="")
    other = Transmission(id="other", session_id="s", duration=2.0,
                         state=ProcessingState.COMPLETE, transcript="Thank you.",
                         signal_metadata={"speech_check": {"verdict": "likely_noise"}})
    md = to_markdown([static, other])
    assert "**Original:** *Static*" in md
    assert "**Original:** *Audio unintelligible*" in md
    rows = {r["id"]: r for r in csv.DictReader(io.StringIO(to_csv([static, other])))}
    assert rows["static"]["transcript_status"] == "static"
    assert rows["other"]["transcript_status"].startswith("audio unintelligible")
    assert static.presented_transcript == "Static"


def test_processing_measures_the_recording_and_the_card_says_static(config, store,
                                                                   tmp_path):
    from babelfishr.app import BabelFishRApp
    from babelfishr.audio.wavefile import write_wav
    from test_live_audio_fixes import PairTranslation, ScriptedTranscription, result

    sr = 16000
    wav = write_wav(str(tmp_path / "hiss.wav"),
                    np.concatenate([silence(1.0, sr), radio_static(3.0, sr, -24.0),
                                    silence(2.0, sr)]), sr)
    app = BabelFishRApp(config=config, store=store)
    app.transcription = ScriptedTranscription(result("", ratio=0.0, nsp=0.9))
    app.translation = PairTranslation()
    try:
        app.start_session(replay_path=wav, name="hiss")
        app.run_replay()
        (row,) = app.transmissions()
        tx = store.get_transmission(row.id)
    finally:
        app.stop_session()
        app.close()
    assert tx.signal_metadata["static_check"]["verdict"] == "static"
    assert tx.is_static and tx.audio_path, "the recording is kept"

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from babelfishr.ui.timeline import TimelineView

    bubble = TimelineView().add(tx)
    assert bubble.original_label.text().startswith("Static")
    assert "Static" in bubble.pairing_label.text()
    assert "unintelligible" not in bubble.pairing_label.text().lower()
