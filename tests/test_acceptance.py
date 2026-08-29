"""The MVP acceptance criteria, one test each.

These are written against the criteria as stated in the product brief, so a
failure here means the MVP has regressed on something the brief called out
explicitly. Numbering matches the brief.
"""

from __future__ import annotations

import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.audio.devices import backend_status, list_input_devices
from babelfishr.audio.wavefile import read_wav
from babelfishr.config import Config
from babelfishr.models import ProcessingState, RadioProfile
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.storage import Store


# 1. The application lists audio input devices.
def test_01_lists_audio_input_devices():
    devices = list_input_devices()
    assert isinstance(devices, list)
    # With no PortAudio present the list is empty, but the call must still
    # succeed and the status must explain why.
    assert isinstance(backend_status(), str) and backend_status()
    for device in devices:
        assert device.max_input_channels > 0
        assert device.describe()


# 2. A user can select an input and see a level meter.
def test_02_level_meter_reports_a_reading():
    import numpy as np

    from babelfishr.audio.meter import LevelMeter
    from babelfishr.audio.source import CallbackAudioSource

    source = CallbackAudioSource(48_000)
    source.start()
    source.push(0.5 * np.sin(2 * np.pi * 440 * np.arange(4800) / 48_000))
    reading = LevelMeter().update(source.read())
    assert -12.0 < reading.rms_dbfs < -3.0
    assert 0.0 < reading.rms_fraction < 1.0
    assert not reading.clipped


def test_02b_meter_flags_clipping():
    import numpy as np

    from babelfishr.audio.meter import LevelMeter
    from babelfishr.audio.source import CallbackAudioSource

    source = CallbackAudioSource(48_000)
    source.start()
    source.push(np.ones(1000))
    reading = LevelMeter().update(source.read())
    assert reading.clipped and reading.clip_count == 1000


# 3. A WAV fixture with multiple transmissions produces the right number of
#    ordered events.
def test_03_fixture_produces_correct_ordered_events(app, fixture_wav,
                                                    expected_transmissions):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="acceptance")
    app.run_replay()
    transmissions = app.transmissions()
    assert len(transmissions) == expected_transmissions
    starts = [t.started_at for t in transmissions]
    assert starts == sorted(starts)


# 4. Pre-roll preserves the beginning of the first spoken word.
def test_04_pre_roll_preserves_onset():
    from babelfishr.detect import DetectorSettings, detect_in_array
    from babelfishr.testing import build_fixture

    fixture = build_fixture(
        [{"gap": 2.0}, {"kind": "voice", "duration": 2.0, "level_dbfs": -14}],
        sample_rate=48_000)
    detected = detect_in_array(fixture.audio, 48_000, DetectorSettings(pre_roll=0.3))
    assert len(detected) == 1
    assert detected[0].start_offset < fixture.transmissions[0].start


# 5. Hang time prevents short pauses from splitting one transmission.
def test_05_hang_time_prevents_splitting(app, gapped_wav):
    app.select_engines()
    app.start_session(replay_path=gapped_wav, name="gapped")
    app.run_replay()
    assert len(app.transmissions()) == 1


# 6. Each event produces a playable original recording.
def test_06_each_event_has_playable_audio(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="audio")
    app.run_replay()
    for tx in app.transmissions():
        assert tx.audio_path, "transmission has no recording"
        path = pathlib.Path(tx.audio_path)
        assert path.exists() and path.stat().st_size > 44  # bigger than a header
        samples, rate = read_wav(str(path))
        assert rate == tx.sample_rate
        assert samples.size > 0
        assert abs(samples.size / rate - tx.duration) < 0.05


# 7. Each event appears as a chat-style bubble.
def test_07_events_appear_as_bubbles(app, fixture_wav, expected_transmissions):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.ui.timeline import TimelineView

    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="ui")
    app.run_replay()

    timeline = TimelineView()
    timeline.set_transmissions(app.transmissions())
    qt_app.processEvents()
    assert timeline.count() == expected_transmissions
    bubble = next(iter(timeline._bubbles.values()))
    assert bubble.header.text()
    assert bubble.original_label.text()


# 8. The original transcript and translation are stored separately.
def test_08_transcript_and_translation_are_separate(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="separate")
    app.run_replay()
    translated = [t for t in app.transmissions() if t.translation]
    assert translated, "expected at least one translated transmission"
    for tx in translated:
        assert tx.transcript
        assert tx.transcript != tx.translation


def test_08b_correction_never_overwrites_the_original(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="corrections")
    app.run_replay()
    tx = app.transmissions()[0]
    original = tx.transcript
    app.correct(tx.id, transcript="corrected text", translation="corrected translation")
    reloaded = app.store.get_transmission(tx.id)
    assert reloaded.transcript == original
    assert reloaded.transcript_correction == "corrected text"
    assert reloaded.display_transcript == "corrected text"


# 9. Failed transcription or translation is recoverable and loses no audio.
def test_09_transcription_failure_keeps_audio(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine(fail=True, fail_message="no model")
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="fail")
    app.run_replay()

    transmissions = app.transmissions()
    assert transmissions
    for tx in transmissions:
        assert tx.state is ProcessingState.FAILED
        assert tx.error and tx.error.stage == "transcription"
        assert "no model" in tx.error.message
        assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
    app.stop_session()


def test_09b_translation_failure_keeps_the_transcript(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine(fail=True, fail_message="api down")
    app.start_session(replay_path=fixture_wav, name="failtranslate")
    app.run_replay()

    failed = [t for t in app.transmissions() if t.state is ProcessingState.FAILED]
    assert failed
    for tx in failed:
        assert tx.transcript, "the transcript must survive a translation failure"
        assert tx.error.stage == "translation"
        assert pathlib.Path(tx.audio_path).exists()
    app.stop_session()


def test_09c_failed_transmission_can_be_retried(config, store, fixture_wav):
    app = BabelFishRApp(config=config, store=store)
    engine = MockTranscriptionEngine(fail=True)
    app.transcription = engine
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="retry")
    app.run_replay()

    failed = [t for t in app.transmissions() if t.state is ProcessingState.FAILED]
    assert failed

    engine.fail = False  # the operator fixed whatever was wrong
    assert app.retry(failed[0].id)
    app.wait_for_processing(timeout=30)
    recovered = app.store.get_transmission(failed[0].id)
    assert recovered.state is ProcessingState.COMPLETE
    assert recovered.transcript
    assert recovered.error is None
    app.stop_session()


# 10. Frequency/channel metadata comes from a user-selected radio profile.
def test_10_profile_supplies_channel_metadata(app, fixture_wav):
    profile = app.save_profile(RadioProfile(
        name="UV-5R", channel_name="GMRS 16", frequency_mhz=462.5750))
    app.select_engines()
    app.start_session(replay_path=fixture_wav, profile_id=profile.id, name="profile")
    app.run_replay()
    for tx in app.transmissions():
        assert tx.channel_name == "GMRS 16"
        assert tx.frequency_mhz == pytest.approx(462.5750)
        assert tx.profile_id == profile.id


def test_10b_no_profile_means_no_invented_metadata(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="noprofile")
    app.run_replay()
    for tx in app.transmissions():
        assert tx.frequency_mhz is None
        assert tx.channel_name == ""


# 11. Sessions survive application restart.
def test_11_sessions_survive_restart(config, fixture_wav):
    store = Store(config.database, recordings_dir=config.recording.directory)
    app = BabelFishRApp(config=config, store=store)
    app.select_engines()
    session = app.start_session(replay_path=fixture_wav, name="persisted")
    app.run_replay()
    count = len(app.transmissions())
    app.close()

    # Fresh process would do exactly this: reopen the database.
    reopened = Store(config.database)
    restored = reopened.get_session(session.id)
    assert restored is not None
    assert restored.name == "persisted"
    assert len(reopened.list_transmissions(session_id=session.id)) == count
    assert restored.ended_at is not None
    reopened.close()


def test_11b_unfinished_work_resumes_after_restart(config, fixture_wav):
    """A crash mid-processing must not strand transmissions."""
    store = Store(config.database)
    app = BabelFishRApp(config=config, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    session = app.start_session(replay_path=fixture_wav, name="resume")
    app.run_replay()

    # Simulate a transmission that was captured but never processed.
    tx = app.transmissions()[0]
    tx.state = ProcessingState.CAPTURED
    tx.transcript = ""
    store.save_transmission(tx)
    app.stop_session()

    reopened = Store(config.database)
    app2 = BabelFishRApp(config=config, store=reopened)
    app2.transcription = MockTranscriptionEngine()
    app2.translation = MockTranslationEngine()
    app2.start_session(replay_path=fixture_wav, name="resume2")
    assert app2.resume_pending() >= 1
    app2.wait_for_processing(timeout=30)
    assert reopened.get_transmission(tx.id).transcript
    app2.stop_session()
    app2.close()


# 12. Search and export work.
def test_12_search_finds_original_and_translated_text(app, fixture_wav):
    app.select_engines()
    app.start_session(replay_path=fixture_wav, name="search")
    app.run_replay()
    transmissions = app.transmissions()

    original_word = transmissions[0].transcript.split()[0]
    assert app.search(original_word)

    translated = [t for t in transmissions if t.translation]
    if translated:
        word = translated[0].translation.split()[0]
        assert app.search(word)


def test_12b_export_produces_every_format(app, fixture_wav, tmp_path):
    from babelfishr import export

    app.select_engines()
    session = app.start_session(replay_path=fixture_wav, name="export")
    app.run_replay()
    transmissions = app.transmissions()

    assert "babelfishr" in export.to_json(transmissions, session)
    assert export.to_csv(transmissions).count("\n") == len(transmissions) + 1
    assert "Original" in export.to_markdown(transmissions, session)

    bundle = export.export_session(app.store, session.id, str(tmp_path / "bundle"))
    root = pathlib.Path(bundle)
    assert (root / "session.json").exists()
    assert (root / "transcript.md").exists()
    assert (root / "transmissions.csv").exists()
    assert len(list((root / "audio").glob("*.wav"))) == len(transmissions)


def test_12c_bundle_is_self_contained(app, fixture_wav, tmp_path):
    """Audio, metadata, transcript and translation must travel together."""
    import json

    from babelfishr import export

    app.select_engines()
    session = app.start_session(replay_path=fixture_wav, name="bundle")
    app.run_replay()
    bundle = pathlib.Path(export.export_session(
        app.store, session.id, str(tmp_path / "b")))
    manifest = json.loads((bundle / "session.json").read_text())
    for entry in manifest["transmissions"]:
        assert entry["audio_path"].startswith("audio/")
        assert (bundle / entry["audio_path"]).exists()
        assert "transcript" in entry and "translation" in entry


# 13. Tests run with deterministic mock providers and need no paid API access.
def test_13_mock_providers_are_deterministic():
    import numpy as np

    audio = np.sin(np.arange(16_000) * 0.05)
    engine = MockTranscriptionEngine()
    first = engine.transcribe(audio, 16_000)
    second = engine.transcribe(audio, 16_000)
    assert first.text == second.text and first.language == second.language
    assert first.text


def test_13b_no_network_or_credentials_are_required(app, fixture_wav):
    summary = app.select_engines()
    assert summary.transcription_placeholder or "Mock" in summary.transcription
    app.start_session(replay_path=fixture_wav, name="offline")
    assert app.run_replay() > 0


# 14. Audio is not uploaded unless the user configures a cloud provider.
def test_14_default_engines_are_local(app):
    summary = app.select_engines()
    assert not summary.sends_data_offsite
    assert summary.privacy_notices == []


def test_14b_cloud_engine_declares_what_it_sends():
    from babelfishr.providers.claude import ClaudeTranslationEngine

    engine = ClaudeTranslationEngine()
    assert engine.privacy.is_cloud
    assert engine.privacy.sends_text
    assert not engine.privacy.sends_audio, "audio must never be uploaded"
    assert "Anthropic" in engine.privacy.describe()


def test_14c_cloud_engine_is_unavailable_without_a_key(monkeypatch):
    from babelfishr.providers.claude import ClaudeTranslationEngine

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "babelfishr.providers.credentials.get_from_keychain", lambda *a, **k: None)
    monkeypatch.setattr(
        "babelfishr.providers.credentials._read_dotenv", lambda p: {})
    assert not ClaudeTranslationEngine().available()


def test_14d_no_credentials_are_hardcoded():
    """The repository must contain no real key material."""
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    pattern = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
    for path in root.rglob("*.py"):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.findall(text):
            assert "invalid" in match or "test" in match, f"possible key in {path}"


# 15. The README distinguishes simulated from physical validation.
def test_15_readme_separates_simulated_and_physical_validation():
    readme = pathlib.Path(__file__).resolve().parent.parent / "README.md"
    assert readme.exists(), "README.md is required"
    text = readme.read_text(encoding="utf-8").lower()
    assert "not been tested" in text or "not tested" in text or "untested" in text
    assert "falconclaw" in text
    assert "macos" in text
