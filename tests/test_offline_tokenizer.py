"""A vocabulary-only Whisper model never reaches WhisperModel offline.

faster-whisper 1.2.1 (transcribe.py, WhisperModel.__init__) builds its
tokenizer from the model folder's tokenizer.json, and when that file is not
there calls ``tokenizers.Tokenizer.from_pretrained("openai/whisper-tiny")``
with no local-only flag: a Hugging Face Hub request, in Offline Field Use
too. Older conversions ship only vocabulary.*, which BabelFishR used to
accept as a complete model.

Every route here runs with an empty Hugging Face cache and every network
route replaced by a recorder (babelfishr.offline_selfcheck.Sentinels, the
same one the frozen app's --selftest-offline-tokenizer uses).
"""

from __future__ import annotations

import os
import pathlib
import sys
import types

import pytest

import pinning
from babelfishr.modes import OperatingMode
from babelfishr.offline_selfcheck import VOCAB_ONLY, Sentinels, write_vocab_only_model

MODEL = "small"
MESSAGE = "The speech model's tokenizer (tokenizer.json) is missing; repair the model in Online / Setup"


@pytest.fixture
def empty_hf_cache(tmp_path, monkeypatch):
    cache = tmp_path / "empty-hf-cache"
    cache.mkdir()
    for name in ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
        monkeypatch.setenv(name, str(cache))
    yield cache
    assert list(cache.iterdir()) == [], "something wrote into the Hugging Face cache"


@pytest.fixture
def sentinels(empty_hf_cache):
    spies = Sentinels().install()
    yield spies
    spies.remove()


@pytest.fixture
def profile(tmp_path, monkeypatch, config):
    home = tmp_path / "home"
    monkeypatch.setenv("BABELFISHR_HOME", str(home))
    config.app_home = str(home)
    config.asr.model = MODEL
    config.asr.engine = "faster-whisper"
    return home


def vocab_only(config) -> pathlib.Path:
    from babelfishr.providers.whisper_local import model_directory_for

    return write_vocab_only_model(model_directory_for(config.paths().models, MODEL))


def engine(config, **kwargs):
    from babelfishr.providers.whisper_local import FasterWhisperEngine

    kwargs.setdefault("models_root", str(config.paths().models))
    return FasterWhisperEngine(model=MODEL, local_files_only=True, **kwargs)


def test_the_installed_library_really_fetches_without_tokenizer_json():
    """The premise, read from the installed faster-whisper source."""
    faster_whisper = pytest.importorskip("faster_whisper")
    import inspect

    from faster_whisper import transcribe

    source = inspect.getsource(transcribe.WhisperModel.__init__)
    assert '"tokenizer.json"' in source
    fallback = source[source.index("os.path.isfile(tokenizer_file)"):]
    assert "Tokenizer.from_pretrained(" in fallback.split("self.feat_kwargs")[0]
    assert "local_files_only" not in fallback.split("self.feat_kwargs")[0]
    assert faster_whisper.__version__


def test_a_vocabulary_only_folder_is_not_complete(profile, config):
    from babelfishr.providers.whisper_local import ModelState, inspect_model_directory

    directory = vocab_only(config)
    assert inspect_model_directory(directory) == (ModelState.INCOMPLETE, ["tokenizer.json"])
    assert not engine(config).available()
    assert MESSAGE in engine(config).unavailable_reason()


@pytest.mark.parametrize("mode", list(OperatingMode))
def test_field_check_refuses_it_with_no_call(profile, config, sentinels, mode):
    from babelfishr.readiness import field_check

    directory = vocab_only(config)
    report = field_check(config, run_smoke_tests=True, mode=mode)
    check = report.get("Local ASR model present")
    assert check.status.value == "fail" and MESSAGE in check.detail, check.detail
    assert report.get("Local transcription smoke test").status.value == "skip"
    assert sentinels.calls == []
    assert sorted(p.name for p in directory.iterdir()) == sorted(VOCAB_ONLY)


@pytest.mark.parametrize("custom", [False, True], ids=["pinned-folder", "custom-folder"])
def test_every_engine_load_route_refuses_before_whispermodel(profile, config, sentinels,
                                                             tmp_path, custom):
    import numpy as np

    from babelfishr.providers import EngineUnavailable

    directory = vocab_only(config)
    kwargs = {"model_path": str(write_vocab_only_model(tmp_path / "custom"))} if custom else {}
    whisper = engine(config, **kwargs)
    for route in (whisper.preload, whisper.warm_up,
                  lambda: whisper.transcribe(np.zeros(16000, dtype=np.float32), 16000)):
        with pytest.raises(EngineUnavailable, match=r"tokenizer \(tokenizer.json\) is missing"):
            route()
    assert sentinels.calls == []
    assert (directory / "vocabulary.txt").exists()


def test_the_gate_holds_even_if_the_folder_looked_complete(profile, config, sentinels,
                                                           monkeypatch):
    """Defence in depth: the check right before construction does not rely
    on the presence check (a file removed between the two)."""
    from babelfishr.providers import EngineUnavailable
    from babelfishr.providers import whisper_local

    vocab_only(config)
    monkeypatch.setattr(whisper_local.FasterWhisperEngine, "model_present", lambda self: True)
    with pytest.raises(EngineUnavailable, match="tokenizer"):
        engine(config).preload()
    assert sentinels.calls == []


def test_an_empty_or_non_file_tokenizer_is_refused_too(profile, config, sentinels):
    """faster-whisper falls back to the Hub when tokenizer.json is not a
    regular file; an empty one is no tokenizer either."""
    from babelfishr.providers import EngineUnavailable
    from babelfishr.readiness import field_check

    directory = vocab_only(config)
    for make in (lambda p: p.write_bytes(b""), lambda p: p.mkdir()):
        path = directory / "tokenizer.json"
        make(path)
        with pytest.raises(EngineUnavailable, match=r"tokenizer \(tokenizer.json\) is missing"):
            engine(config).preload()
        check = field_check(config, run_smoke_tests=True,
                            mode=OperatingMode.FIELD_OFFLINE).get("Local ASR model present")
        assert check.status.value == "fail" and MESSAGE in check.detail
        path.rmdir() if path.is_dir() else path.unlink()
    assert sentinels.calls == []


def test_the_provider_factory_never_selects_it_offline(profile, config, sentinels):
    from babelfishr.providers import EngineUnavailable, build_transcription_engine

    vocab_only(config)
    for mode in (OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY):
        config.mode = mode.value
        with pytest.raises(EngineUnavailable, match=r"tokenizer \(tokenizer.json\) is missing"):
            build_transcription_engine(config)
    assert sentinels.calls == []


def test_capture_still_archives_with_the_model_refused(profile, config, store, sentinels,
                                                       fixture_wav):
    """Monitoring with the vocabulary-only model: every message is recorded
    and stored, its transcription failed with the clear message."""
    from babelfishr.app import BabelFishRApp
    from babelfishr.models import ProcessingState
    from babelfishr.providers.mock import MockTranslationEngine

    vocab_only(config)
    config.mode = OperatingMode.FIELD_OFFLINE.value
    app = BabelFishRApp(config=config, store=store)
    app.transcription = engine(config)
    app.translation = MockTranslationEngine()
    app.start_session(replay_path=fixture_wav, name="vocabulary only")
    app.run_replay()
    transmissions = app.transmissions()
    app.stop_session()
    assert transmissions
    for tx in transmissions:
        assert tx.audio_path and pathlib.Path(tx.audio_path).exists()
        assert tx.state is ProcessingState.FAILED and tx.error.stage == "transcription"
        assert MESSAGE in tx.error.message
    assert sentinels.calls == []


def test_record_only_never_invokes_asr_or_a_transfer(profile, config, store, sentinels,
                                                     fixture_wav, monkeypatch):
    from babelfishr.app import BabelFishRApp
    from babelfishr.providers import whisper_local
    from babelfishr.transfers import registry

    vocab_only(config)
    config.mode = OperatingMode.RECORD_ONLY.value
    began, loads = [], []
    owner = registry()
    real_begin = owner.begin
    monkeypatch.setattr(owner, "begin", lambda *a, **k: began.append(a) or real_begin(*a, **k))
    monkeypatch.setattr(whisper_local.FasterWhisperEngine, "_load",
                        lambda self: loads.append(self) or pytest.fail("ASR in Record Only"))
    app = BabelFishRApp(config=config, store=store)
    app.select_engines()
    assert app.transcription is None and app.translation is None
    app.start_session(replay_path=fixture_wav, name="record only")
    app.run_replay()
    transmissions = app.transmissions()
    app.stop_session()
    assert transmissions
    assert all(pathlib.Path(t.audio_path).exists() for t in transmissions)
    assert all(t.error is None for t in transmissions), "recorded, not failed"
    assert began == [] and loads == [] and sentinels.calls == []


@pytest.mark.parametrize("args", [["field-check"], ["engines"]])
def test_cli_routes_refuse_it_with_no_call(profile, config, sentinels, capsys, tmp_path,
                                           monkeypatch, args):
    from babelfishr import cli

    monkeypatch.chdir(tmp_path)
    assert cli.main(["mode", "--set", "field-offline"]) == 0
    from babelfishr.config import Config

    loaded = Config.load(None)
    vocab_only(loaded)
    capsys.readouterr()
    cli.main(args)
    out = capsys.readouterr().out
    assert "tokenizer (tokenizer.json) is missing" in out, out
    assert sentinels.calls == []


def test_cli_replay_still_records_with_the_model_refused(profile, sentinels, capsys,
                                                         tmp_path, monkeypatch, fixture_wav):
    from babelfishr import cli
    from babelfishr.config import Config

    monkeypatch.chdir(tmp_path)
    assert cli.main(["mode", "--set", "field-offline"]) == 0
    vocab_only(Config.load(None))
    capsys.readouterr()
    cli.main(["replay", fixture_wav])
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "tokenizer (tokenizer.json) is missing" in text, text
    recordings = list((tmp_path / "home").rglob("*.wav"))
    assert recordings, "the replay recorded nothing"
    assert sentinels.calls == []


# ---- positive control and repair ---------------------------------------------------
def test_a_good_pinned_model_passes_the_gate(profile, config, monkeypatch, empty_hf_cache):
    from babelfishr.providers import whisper_local

    files = dict(VOCAB_ONLY, **{"tokenizer.json": b'{"model": {"vocab": {}}}'})
    pinning.pin(monkeypatch, MODEL, files)
    directory = whisper_local.model_directory_for(config.paths().models, MODEL)
    directory.mkdir(parents=True)
    for name, data in files.items():
        (directory / name).write_bytes(data)
    whisper_local.verify_model(MODEL, config.paths().models)
    built = []
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = lambda path, **kwargs: built.append(path) or object()
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    assert whisper_local.tokenizer_problem(directory) == ""
    whisper = engine(config)
    assert whisper.available()
    whisper.preload()
    assert built == [str(directory)]


@pytest.fixture
def legacy_turbo(tmp_path):
    from test_download_real_hub import FILES

    directory = tmp_path / "models" / "large-v3-turbo"
    directory.mkdir(parents=True)
    for name, data in FILES.items():
        if name != "tokenizer.json":
            (directory / name).write_bytes(data)
    (directory / "vocabulary.json").write_bytes(b'{"a": 0}')
    return directory


def test_online_repair_fetches_only_the_tokenizer(legacy_turbo, tmp_path, monkeypatch):
    pytest.importorskip("httpx2")
    from babelfishr.providers import whisper_local
    from babelfishr.providers.whisper_local import ModelState, inspect_model_directory

    assert inspect_model_directory(legacy_turbo)[0] is ModelState.INCOMPLETE
    # The in-memory Hub serves the pinned revision; vocabulary.json is part of
    # this test pin so the legacy file counts as genuine.
    import httpx2
    from test_download_real_hub import FILES
    import huggingface_hub
    from huggingface_hub.utils import _http
    from test_download_real_hub import COMMIT, REPO, InMemoryHub

    files = dict(FILES, **{"vocabulary.json": b'{"a": 0}'})
    server = InMemoryHub(files=files)
    pinning.pin(monkeypatch, "large-v3-turbo", files, repo=REPO, revision=COMMIT)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf-home"))
    monkeypatch.setenv("HF_HUB_DISABLE_XET", "1")
    previous = _http._GLOBAL_CLIENT_FACTORY
    huggingface_hub.set_client_factory(
        lambda: httpx2.Client(transport=httpx2.MockTransport(server)))
    try:
        before = {p.name: p.stat().st_ino for p in legacy_turbo.iterdir()}
        whisper_local.prepare_model("large-v3-turbo", tmp_path / "models")
    finally:
        huggingface_hub.set_client_factory(previous)
    assert server.served == {"tokenizer.json": len(FILES["tokenizer.json"])}
    assert inspect_model_directory(legacy_turbo)[0] is ModelState.COMPLETE
    for name, inode in before.items():
        assert (legacy_turbo / name).stat().st_ino == inode, f"{name} was replaced"


# ---- the packaged self-check ---------------------------------------------------------
def test_the_packaged_selftest_passes_from_source(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "entry-home"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.syspath_prepend(str(pathlib.Path(__file__).parent.parent / "packaging"))
    import app_entry

    assert app_entry.main(["--selftest-offline-tokenizer"]) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "ok:   a vocabulary-only model is refused offline" in out


def test_the_packaged_selftest_catches_a_missing_gate(monkeypatch, tmp_path):
    """Negative control: with the gate removed, the self-check fails."""
    from babelfishr import offline_selfcheck
    from babelfishr.providers import whisper_local

    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "entry-home"))
    monkeypatch.setattr(whisper_local, "tokenizer_problem", lambda *a, **k: "")
    lines = []
    assert offline_selfcheck.run(out=lines.append) == 1
    assert any("faster_whisper.WhisperModel" in line or "not refuse" in line
               for line in lines), lines


def test_the_bundle_verification_runs_the_selftest():
    script = (pathlib.Path(__file__).parent.parent / "packaging"
              / "verify_independence.sh").read_text()
    assert "--selftest-offline-tokenizer" in script
    assert os.access(pathlib.Path(__file__).parent.parent / "packaging"
                     / "verify_independence.sh", os.X_OK)
