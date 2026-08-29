"""Field Offline must be enforceable, not aspirational.

These are deterministic unit tests: they prove the *guarantees* (no cloud
provider constructed, no placeholder output, no download attempt, honest
failure) without needing a model, a key or a network.
"""

from __future__ import annotations

import pathlib

import numpy as np
import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.modes import (AppPaths, OfflineViolation, OperatingMode,
                              guard_cloud, guard_download, guard_mock)
from babelfishr.providers import (CLOUD_ENGINES, EngineUnavailable,
                                  PLACEHOLDER_ENGINES,
                                  build_transcription_engine,
                                  build_translation_engine)
from babelfishr.providers.base import EngineUnavailable as ProviderUnavailable


# ---- mode semantics ---------------------------------------------------
def test_only_online_setup_permits_cloud_mock_and_downloads():
    assert OperatingMode.ONLINE_SETUP.allows_cloud
    assert OperatingMode.ONLINE_SETUP.allows_mock
    assert OperatingMode.ONLINE_SETUP.allows_downloads
    for mode in (OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY):
        assert not mode.allows_cloud
        assert not mode.allows_mock
        assert not mode.allows_downloads


def test_record_only_runs_no_processing():
    assert not OperatingMode.RECORD_ONLY.runs_processing
    assert OperatingMode.FIELD_OFFLINE.runs_processing


@pytest.mark.parametrize("guard,arg", [
    (guard_cloud, "Claude"), (guard_mock, "mock"), (guard_download, "a model")])
def test_guards_refuse_in_field_offline(guard, arg):
    with pytest.raises(OfflineViolation):
        guard(OperatingMode.FIELD_OFFLINE, arg)
    guard(OperatingMode.ONLINE_SETUP, arg)  # permitted, must not raise


# ---- engine selection -------------------------------------------------
def test_field_offline_never_constructs_a_cloud_provider(monkeypatch):
    """Not merely 'never called' - never built, so it cannot be invoked."""
    constructed = []

    import babelfishr.providers.claude as claude_module

    original = claude_module.ClaudeTranslationEngine

    class Tracking(original):
        def __init__(self, *args, **kwargs):
            constructed.append("claude")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(claude_module, "ClaudeTranslationEngine", Tracking)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-for-testing")

    with pytest.raises(EngineUnavailable):
        build_translation_engine(mode=OperatingMode.FIELD_OFFLINE)
    assert constructed == [], "a cloud engine was constructed in Field Offline"


def test_field_offline_refuses_an_explicit_cloud_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-for-testing")
    with pytest.raises(EngineUnavailable) as info:
        build_translation_engine(requested="claude",
                                 mode=OperatingMode.FIELD_OFFLINE)
    assert "off this computer" in str(info.value)


def test_field_offline_never_returns_mock_output():
    for builder in (build_transcription_engine, build_translation_engine):
        with pytest.raises(EngineUnavailable):
            builder(mode=OperatingMode.FIELD_OFFLINE)
        with pytest.raises(EngineUnavailable):
            builder(requested="mock", mode=OperatingMode.FIELD_OFFLINE)


def test_missing_local_engine_does_not_fall_through_to_cloud(monkeypatch):
    """The rule that matters: nothing leaves the Mac because argos is missing."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-for-testing")
    with pytest.raises(EngineUnavailable) as info:
        build_translation_engine(mode=OperatingMode.FIELD_OFFLINE)
    message = str(info.value)
    assert "claude" not in message.lower() or "off this computer" in message
    assert "still captured" in message


def test_online_setup_still_allows_the_mock_for_development():
    engine = build_translation_engine(mode=OperatingMode.ONLINE_SETUP)
    assert engine.id in PLACEHOLDER_ENGINES or engine.id == "argos"


def test_auto_selection_never_reaches_a_cloud_engine():
    from babelfishr.providers import TRANSLATION_PREFERENCE

    assert not (set(TRANSLATION_PREFERENCE) & CLOUD_ENGINES), (
        "auto selection must never be able to pick a cloud engine")


# ---- ASR offline loading ----------------------------------------------
def test_whisper_defaults_to_local_files_only():
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    assert FasterWhisperEngine().local_files_only is True


def test_whisper_availability_requires_the_model_not_just_the_library(tmp_path):
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    engine = FasterWhisperEngine(model="tiny", models_root=str(tmp_path),
                                 local_files_only=True)
    assert not engine.model_present()
    assert not engine.available(), (
        "availability must mean the model is present, not that the import worked")
    assert "prepare-field" in engine.unavailable_reason()


def test_whisper_offline_failure_is_honest_and_immediate(tmp_path):
    """A missing model must fail cleanly, not hang attempting a download."""
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    pytest.importorskip("faster_whisper")
    engine = FasterWhisperEngine(model="tiny", models_root=str(tmp_path),
                                 local_files_only=True)
    with pytest.raises(ProviderUnavailable) as info:
        engine.transcribe(np.zeros(16_000), 16_000)
    assert "prepare-field" in str(info.value)


def test_whisper_model_presence_requires_every_asset(tmp_path):
    """model.bin alone is an interrupted download, not a usable model."""
    from babelfishr.providers.whisper_local import (FasterWhisperEngine,
                                                    ModelState)

    directory = tmp_path / "tiny"
    directory.mkdir()
    engine = FasterWhisperEngine(model="tiny", models_root=str(tmp_path))
    assert not engine.model_present(), "an empty directory is not a model"

    (directory / "model.bin").write_bytes(b"x" * 128)
    assert not engine.model_present(), "model.bin alone is not a usable model"
    assert engine.model_state()[0] is ModelState.INCOMPLETE
    assert "incomplete" in engine.unavailable_reason()

    (directory / "config.json").write_text("{}")
    (directory / "tokenizer.json").write_text("{}")
    assert engine.model_present()
    assert engine.model_state()[0] is ModelState.COMPLETE


# ---- translation pair awareness ---------------------------------------
def test_argos_availability_requires_an_installed_pair():
    from babelfishr.providers.argos import ArgosTranslateEngine

    engine = ArgosTranslateEngine(target_language="en")
    if not engine.library_installed():
        pytest.skip("argostranslate is not installed")
    if engine.installed_pairs():
        pytest.skip("language packs are installed in this environment")
    assert not engine.available(), (
        "availability must mean a usable pair exists, not that the import worked")
    assert "languages install" in engine.unavailable_reason()


# ---- Record Only -------------------------------------------------------
def test_record_only_needs_no_engines(config, store, fixture_wav):
    config.mode = OperatingMode.RECORD_ONLY.value
    app = BabelFishRApp(config=config, store=store)
    summary = app.select_engines()
    assert app.transcription is None and app.translation is None
    assert "Record Only" in summary.warnings[0]

    app.start_session(replay_path=fixture_wav, name="record-only")
    app.run_replay()
    transmissions = app.transmissions()
    assert transmissions
    for tx in transmissions:
        assert pathlib.Path(tx.audio_path).exists()
    app.stop_session()


def test_field_offline_records_even_with_no_processing(config, store, fixture_wav):
    """Transcription unavailable must never stop capture."""
    config.mode = OperatingMode.FIELD_OFFLINE.value
    app = BabelFishRApp(config=config, store=store)
    summary = app.select_engines()
    assert summary.warnings, "unavailable engines must be reported, not hidden"
    assert app.transcription is None

    app.start_session(replay_path=fixture_wav, name="offline")
    app.run_replay()
    assert len(app.transmissions()) == 5
    for tx in app.transmissions():
        assert pathlib.Path(tx.audio_path).exists()
    app.stop_session()


# ---- readiness ---------------------------------------------------------
def test_field_check_reports_missing_asr_model(config):
    from babelfishr.readiness import CheckStatus, field_check

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.FIELD_OFFLINE)
    check = report.get("Local ASR model present")
    assert check is not None
    if check.status is CheckStatus.FAIL:
        assert check.remedy, "a failure must tell the operator what to do"
    assert not report.can_transcribe


def test_field_check_reports_missing_translation_pair(config):
    from babelfishr.readiness import CheckStatus, field_check

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.FIELD_OFFLINE)
    check = report.get("Installed translation paths")
    assert check is not None
    if check.status is CheckStatus.FAIL:
        assert "languages install" in check.remedy or "pip install" in check.remedy


def test_field_check_confirms_cloud_and_mock_are_disabled(config):
    from babelfishr.readiness import CheckStatus, field_check

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.FIELD_OFFLINE)
    assert report.get("Cloud processing disabled").status is CheckStatus.PASS
    assert report.get("Mock engines disabled").status is CheckStatus.PASS


def test_field_check_warns_when_online_mode_permits_cloud(config):
    from babelfishr.readiness import CheckStatus, field_check

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.ONLINE_SETUP)
    assert report.get("Cloud processing disabled").status is CheckStatus.WARN
    assert report.get("Mock engines disabled").status is CheckStatus.WARN


def test_field_check_recommends_record_only_when_processing_is_missing(config,
                                                                      monkeypatch):
    from babelfishr.readiness import Check, CheckStatus, ReadinessReport

    report = ReadinessReport(mode=OperatingMode.FIELD_OFFLINE)
    report.add(Check("Audio backend", CheckStatus.PASS))
    report.add(Check("Recording directory writable", CheckStatus.PASS))
    report.add(Check("Local transcription smoke test", CheckStatus.FAIL))
    assert report.can_record
    assert not report.field_ready
    assert report.recommended_mode() is OperatingMode.RECORD_ONLY
    assert "Record Only" in report.summary()


def test_field_check_never_downloads(config, monkeypatch):
    """Field Check must not reach the network under any circumstance."""
    import socket

    from babelfishr.readiness import field_check

    def refuse(*args, **kwargs):
        raise AssertionError("field-check attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    report = field_check(config, run_smoke_tests=True,
                         mode=OperatingMode.FIELD_OFFLINE)
    assert report is not None


# ---- paths -------------------------------------------------------------
def test_app_paths_are_not_a_cache_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    paths = AppPaths.resolve()
    assert "Cache" not in str(paths.models)
    assert paths.models.name == "models"
    paths.ensure()
    assert paths.models.is_dir() and paths.language_packs.is_dir()
    assert paths.writable()


def test_documented_cli_commands_all_exist():
    """Documentation referenced 'languages --install' before it existed."""
    from babelfishr.cli import build_parser

    parser = build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    commands = set(subparsers[0].choices)
    for required in ("prepare-field", "field-check", "languages", "devices",
                     "doctor", "replay", "listen", "search", "export",
                     "profiles", "selftest", "gui", "config", "analyze", "mode",
                     "level", "calibrate", "test-record", "sessions", "engines"):
        assert required in commands, f"documented command {required!r} is missing"


def test_argos_error_messages_reference_a_real_command():
    """The old message pointed at 'languages --install', which never existed."""
    from babelfishr.cli import build_parser
    from babelfishr.providers.argos import ArgosTranslateEngine

    parser = build_parser()
    subparsers = [a for a in parser._actions if hasattr(a, "choices") and a.choices]
    languages = subparsers[0].choices["languages"]
    actions = {a.dest for a in languages._actions}
    assert "action" in actions and "source" in actions and "target" in actions

    engine = ArgosTranslateEngine(target_language="en")
    message = engine.unavailable_reason()
    assert "languages --install" not in message
