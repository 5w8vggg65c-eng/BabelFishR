"""Field Offline with outbound networking genuinely broken.

The requirement is zero network dependency and no attempted cloud use - not a
claim that an environment variable acts as a firewall. So these tests make
outbound access *fail* at the socket layer and then run the Field Offline
pipeline over prepared local assets. Any attempt to reach out raises, which
fails the test rather than silently degrading.
"""

from __future__ import annotations

import pathlib
import socket

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.modes import OperatingMode, offline_environment
from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
from babelfishr.storage import Store
from babelfishr.testing import standard_fixture

pytestmark = pytest.mark.unit


class NetworkBlocked(AssertionError):
    """Raised when anything tries to open an outbound connection."""


@pytest.fixture
def no_network(monkeypatch):
    """Make every outbound path fail loudly, the way a pulled cable would."""
    def refuse(*args, **kwargs):
        raise NetworkBlocked("outbound network access was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    try:
        import http.client

        monkeypatch.setattr(http.client.HTTPConnection, "connect", refuse)
        monkeypatch.setattr(http.client.HTTPSConnection, "connect", refuse)
    except Exception:  # noqa: BLE001 - stdlib always present, be safe anyway
        pass
    return refuse


@pytest.fixture
def offline_app(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    config = Config.load()
    config.mode = OperatingMode.FIELD_OFFLINE.value
    return config


# ---- the claim itself ---------------------------------------------------
def test_no_proxy_is_not_claimed_as_a_network_control():
    """NO_PROXY bypasses proxies; it blocks nothing."""
    assert "NO_PROXY" not in offline_environment()


def test_offline_environment_only_sets_model_hub_flags():
    for key in offline_environment():
        assert key.startswith(("HF_", "TRANSFORMERS_"))


def test_offline_environment_is_documented_as_not_a_firewall():
    from babelfishr import modes

    doc = modes.offline_environment.__doc__ or ""
    assert "not a network control" in doc or "blocks nothing" in doc


# ---- the pipeline, with the network broken ------------------------------
def test_full_pipeline_runs_with_networking_broken(no_network, offline_app,
                                                   tmp_path):
    """Capture, transcribe, translate and store, with sockets refusing."""
    fixture = standard_fixture(48_000)
    wav = fixture.write(str(tmp_path / "fixture.wav"))

    store = Store(offline_app.database,
                  recordings_dir=offline_app.recording.directory)
    app = BabelFishRApp(config=offline_app, store=store)
    # Stand in for prepared local assets: this environment cannot download
    # real weights, so local engines are simulated. What is being proved is
    # that the pipeline needs no network, not that Whisper works.
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()

    app.start_session(replay_path=wav, name="offline-integration")
    app.run_replay()

    transmissions = app.transmissions()
    assert len(transmissions) == 5
    for tx in transmissions:
        assert pathlib.Path(tx.audio_path).exists()
    assert any(tx.transcript for tx in transmissions)
    app.stop_session()
    app.close()


def test_search_and_export_work_with_networking_broken(no_network, offline_app,
                                                       tmp_path):
    from babelfishr import export

    fixture = standard_fixture(48_000)
    wav = fixture.write(str(tmp_path / "fixture.wav"))
    store = Store(offline_app.database,
                  recordings_dir=offline_app.recording.directory)
    app = BabelFishRApp(config=offline_app, store=store)
    app.transcription = MockTranscriptionEngine()
    app.translation = MockTranslationEngine()
    session = app.start_session(replay_path=wav, name="offline-export")
    app.run_replay()

    assert app.search("") is not None
    bundle = export.export_session(store, session.id, str(tmp_path / "bundle"))
    assert (pathlib.Path(bundle) / "session.json").exists()
    app.stop_session()
    app.close()


def test_field_check_runs_with_networking_broken(no_network, offline_app):
    from babelfishr.readiness import field_check

    report = field_check(offline_app, run_smoke_tests=True,
                         mode=OperatingMode.FIELD_OFFLINE)
    assert report is not None
    assert report.get("Cloud processing disabled") is not None


def test_engine_selection_makes_no_connection(no_network, offline_app):
    from babelfishr.providers import EngineUnavailable, build_translation_engine

    with pytest.raises(EngineUnavailable):
        build_translation_engine(offline_app)


def test_model_presence_check_makes_no_connection(no_network, offline_app):
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    engine = FasterWhisperEngine(
        model="small", models_root=str(offline_app.paths().models),
        local_files_only=True)
    assert not engine.model_present()
    assert engine.unavailable_reason()


def test_argos_enumeration_makes_no_connection(no_network, offline_app):
    pytest.importorskip("argostranslate.translate")
    from babelfishr.providers.argos import ArgosTranslateEngine

    engine = ArgosTranslateEngine(target_language="en")
    assert isinstance(engine.installed_pairs(), list)


# ---- refusals -----------------------------------------------------------
def test_field_offline_refuses_preparation(offline_app):
    from babelfishr.modes import OfflineViolation
    from babelfishr.preparation import prepare_field

    with pytest.raises(OfflineViolation):
        prepare_field(offline_app, asr_model="small")


def test_field_offline_refuses_language_installation(offline_app):
    from babelfishr.modes import OfflineViolation
    from babelfishr.preparation import install_language

    with pytest.raises(OfflineViolation):
        install_language("es", "en", OperatingMode.FIELD_OFFLINE)


def test_record_only_also_refuses_downloads():
    from babelfishr.modes import OfflineViolation
    from babelfishr.preparation import install_language

    with pytest.raises(OfflineViolation):
        install_language("es", "en", OperatingMode.RECORD_ONLY)


def test_verification_without_download_is_still_allowed(offline_app):
    """Checking what is present must work offline; only fetching is refused."""
    from babelfishr.preparation import prepare_field

    result = prepare_field(offline_app, asr_model="small", skip_download=True,
                           report=lambda text: None)
    assert result is not None


def test_cloud_engine_is_never_constructed_offline(no_network, offline_app,
                                                   monkeypatch):
    """With the network broken, a cloud attempt would raise - it must not occur."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-for-testing")
    from babelfishr.providers import EngineUnavailable, build_translation_engine

    with pytest.raises(EngineUnavailable) as info:
        build_translation_engine(offline_app)
    # EngineUnavailable, not NetworkBlocked: nothing tried to connect.
    assert not isinstance(info.value, NetworkBlocked)
