"""Regression tests for the Whisper model cache-layout mismatch.

The defect: preparation passed ``download_root=<AppPaths.models>`` to
``WhisperModel``, which makes faster-whisper treat that path as a *Hugging Face
cache* and populate ``models--Systran--faster-whisper-small/snapshots/<sha>/``.
Presence checks meanwhile looked for ``<models>/<name>/model.bin``. The two
layouts never met, so a successful download still reported "no model", and
field loading silently depended on the HF cache surviving.

The fix routes preparation, the manifest, presence checks, Field Check, the
engine factory and the real-model tests through one resolver.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from babelfishr.config import Config
from babelfishr.modes import AppPaths, OperatingMode
from babelfishr.providers.whisper_local import (REQUIRED_ASSETS,
                                                FasterWhisperEngine, ModelState,
                                                inspect_model_directory,
                                                model_directory_for)

pytestmark = pytest.mark.unit


def _write_complete_model(directory: pathlib.Path) -> pathlib.Path:
    """A directory shaped exactly as download_model(output_dir=...) leaves it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.bin").write_bytes(b"weights" * 64)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    (directory / "preprocessor_config.json").write_text("{}", encoding="utf-8")
    (directory / "vocabulary.json").write_text("{}", encoding="utf-8")
    return directory


def _write_hf_cache_layout(models_root: pathlib.Path) -> pathlib.Path:
    """The layout the OLD code actually produced."""
    snapshot = (models_root / "models--Systran--faster-whisper-small"
                / "snapshots" / "abc123")
    snapshot.mkdir(parents=True, exist_ok=True)
    (snapshot / "model.bin").write_bytes(b"weights")
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    return snapshot


# ---- the mismatch itself ----------------------------------------------
def test_hf_cache_layout_is_not_mistaken_for_a_prepared_model(tmp_path):
    """The old failure: a populated HF cache still means 'not prepared'.

    This is the correct answer - we must not load out of a cache we do not
    own - and the remedy must point at preparation rather than staying silent.
    """
    _write_hf_cache_layout(tmp_path)
    engine = FasterWhisperEngine(model="small", models_root=str(tmp_path),
                                 local_files_only=True)
    assert not engine.model_present()
    assert not engine.available()
    assert "prepare-field" in engine.unavailable_reason()


def test_prepared_layout_is_found(tmp_path):
    """The layout download_model(output_dir=...) writes must be recognised."""
    _write_complete_model(model_directory_for(tmp_path, "small"))
    engine = FasterWhisperEngine(model="small", models_root=str(tmp_path),
                                 local_files_only=True)
    assert engine.model_present()
    assert engine.available()
    assert engine.unavailable_reason() == ""


def test_one_resolver_is_used_everywhere(tmp_path, monkeypatch):
    """Preparation, presence, Field Check and the factory must agree."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    paths = AppPaths.resolve().ensure()
    expected = model_directory_for(paths.models, "small")
    _write_complete_model(expected)

    config = Config()
    config.app_home = str(tmp_path)
    config.asr.model = "small"

    # 1. the resolver
    assert model_directory_for(paths.models, "small") == expected

    # 2. the engine the factory builds
    from babelfishr.providers import _transcription_factories

    engine = _transcription_factories(config)["faster-whisper"]()
    assert engine.model_directory() == expected
    assert engine.model_present()

    # 3. Field Check
    from babelfishr.readiness import CheckStatus, field_check

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.FIELD_OFFLINE)
    check = report.get("Local ASR model present")
    assert check.status is CheckStatus.PASS
    assert str(expected) in check.detail


def test_field_offline_selects_the_prepared_model(tmp_path, monkeypatch):
    """prepare -> Field Check -> Field Offline engine selection, end to end."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    paths = AppPaths.resolve().ensure()
    _write_complete_model(model_directory_for(paths.models, "small"))

    config = Config()
    config.app_home = str(tmp_path)
    config.asr.model = "small"
    config.mode = OperatingMode.FIELD_OFFLINE.value

    from babelfishr.providers import build_transcription_engine

    engine = build_transcription_engine(config)
    assert engine.id == "faster-whisper", "must not fall back to a mock"
    assert engine.local_files_only, "field loading must not be able to download"
    assert engine.model_directory() == model_directory_for(paths.models, "small")


def test_field_offline_refuses_when_only_a_cache_exists(tmp_path, monkeypatch):
    """A populated HF cache must not make Field Offline think it is ready."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    paths = AppPaths.resolve().ensure()
    _write_hf_cache_layout(paths.models)

    config = Config()
    config.app_home = str(tmp_path)
    config.asr.model = "small"
    config.mode = OperatingMode.FIELD_OFFLINE.value

    from babelfishr.providers import EngineUnavailable, build_transcription_engine

    with pytest.raises(EngineUnavailable):
        build_transcription_engine(config)


# ---- incomplete models -------------------------------------------------
def test_interrupted_download_is_reported_as_incomplete(tmp_path):
    directory = model_directory_for(tmp_path, "small")
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(b"partial")

    state, missing = inspect_model_directory(directory)
    assert state is ModelState.INCOMPLETE
    assert "config.json" in missing

    engine = FasterWhisperEngine(model="small", models_root=str(tmp_path))
    assert not engine.model_present()
    reason = engine.unavailable_reason()
    assert "incomplete" in reason and "config.json" in reason


def test_incomplete_model_is_not_deleted_by_inspection(tmp_path):
    """Repair must be safe: nothing may destroy a partially downloaded model."""
    directory = model_directory_for(tmp_path, "small")
    directory.mkdir(parents=True)
    payload = b"expensive partial download"
    (directory / "model.bin").write_bytes(payload)

    engine = FasterWhisperEngine(model="small", models_root=str(tmp_path))
    engine.model_state()
    engine.model_present()
    engine.unavailable_reason()
    assert (directory / "model.bin").read_bytes() == payload


def test_empty_directory_reads_as_missing_not_incomplete(tmp_path):
    directory = model_directory_for(tmp_path, "small")
    directory.mkdir(parents=True)
    assert inspect_model_directory(directory)[0] is ModelState.MISSING


def test_tokenizer_may_be_either_asset(tmp_path):
    """Older conversions ship vocabulary.* instead of tokenizer.json."""
    directory = model_directory_for(tmp_path, "small")
    directory.mkdir(parents=True)
    (directory / "model.bin").write_bytes(b"w")
    (directory / "config.json").write_text("{}")
    assert inspect_model_directory(directory)[0] is ModelState.INCOMPLETE
    (directory / "vocabulary.txt").write_text("a\nb\n")
    assert inspect_model_directory(directory)[0] is ModelState.COMPLETE


# ---- manifest ----------------------------------------------------------
def test_manifest_records_the_resolved_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    paths = AppPaths.resolve().ensure()
    directory = _write_complete_model(model_directory_for(paths.models, "small"))

    from babelfishr.preparation import _write_manifest

    _write_manifest(paths, "small", directory, 480.0, "faster-whisper/small")
    manifest = json.loads((paths.models / "manifest.json").read_text())
    assert manifest["small"]["path"] == str(directory)
    assert str(directory).endswith("models/small")
    # The manifest must point where presence checks look.
    engine = FasterWhisperEngine(model="small", models_root=str(paths.models))
    assert str(engine.model_directory()) == manifest["small"]["path"]


def test_prepare_field_skip_download_reports_the_real_state(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    config = Config()
    config.app_home = str(tmp_path)

    from babelfishr.preparation import prepare_field

    result = prepare_field(config, asr_model="small", skip_download=True,
                           report=lambda text: None)
    asr = [step for step in result.steps if step[0] == "Local ASR model"]
    assert asr, "preparation must report on the ASR model"
    name, ok, detail = asr[0]
    if not ok:
        assert "missing" in detail or "incomplete" in detail or "not installed" in detail
