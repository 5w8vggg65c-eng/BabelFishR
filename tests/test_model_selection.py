"""Non-default Whisper model selection.

The defect: the assistant passed its chosen model into preparation, but the
Field Check that followed read ``config.asr.model``, which was still the
default. Choosing ``tiny`` therefore downloaded tiny and then failed readiness
looking for ``small``.

The related requirement: the choice is persisted only when the whole operation
succeeds, so a failed or cancelled preparation cannot corrupt a setting that
was previously working.
"""

from __future__ import annotations

import os
import pathlib
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.app import BabelFishRApp  # noqa: E402
from babelfishr.config import Config  # noqa: E402
from babelfishr.modes import AppPaths, OperatingMode  # noqa: E402
from babelfishr.preparation import PreparationResult  # noqa: E402
from babelfishr.providers.whisper_local import model_directory_for  # noqa: E402
from babelfishr.readiness import Check, CheckStatus, ReadinessReport  # noqa: E402
from babelfishr.storage import Store  # noqa: E402
from babelfishr.ui.workers import prepare_field_job, working_config  # noqa: E402

pytestmark = pytest.mark.unit

NON_DEFAULT = "tiny"
DEFAULT = "small"


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    config = Config.load()
    assert config.asr.model == DEFAULT, "fixture assumes the default model"
    return BabelFishRApp(config=config, store=Store(":memory:"))


def _pump(qt_app, predicate, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _write_complete_model(directory: pathlib.Path) -> pathlib.Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "model.bin").write_bytes(b"weights" * 32)
    (directory / "config.json").write_text("{}", encoding="utf-8")
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    return directory


# ---- the working copy ---------------------------------------------------
def test_working_config_carries_the_selected_model():
    config = Config()
    config.asr.model = DEFAULT
    working = working_config(config, NON_DEFAULT)
    assert working.asr.model == NON_DEFAULT


def test_working_config_does_not_mutate_the_original():
    config = Config()
    config.asr.model = DEFAULT
    working_config(config, NON_DEFAULT)
    assert config.asr.model == DEFAULT, (
        "preparation must not rewrite the live configuration")


def test_working_config_is_a_deep_copy():
    config = Config()
    working = working_config(config, NON_DEFAULT)
    working.recording.directory = "/somewhere/else"
    assert config.recording.directory != "/somewhere/else"


# ---- preparation and readiness agree ------------------------------------
def test_readiness_checks_the_selected_model_not_the_default(app, monkeypatch,
                                                             tmp_path):
    """The core defect: tiny prepared, small checked."""
    import babelfishr.ui.workers as workers

    paths = AppPaths.resolve().ensure()
    # Only the non-default model exists on disk.
    _write_complete_model(model_directory_for(paths.models, NON_DEFAULT))

    prepared, checked = {}, {}

    def fake_prepare(config, *, asr_model=None, language_pairs=None,
                     report=None, skip_download=False, mode=None):
        prepared["model"] = asr_model
        prepared["config_model"] = config.asr.model
        result = PreparationResult()
        result.add("Local ASR model", True, f"prepared {asr_model}")
        return result

    def fake_field_check(config, run_smoke_tests=True, mode=None):
        checked["model"] = config.asr.model
        report = ReadinessReport()
        engine_model = config.asr.model
        directory = model_directory_for(config.paths().models, engine_model)
        present = (directory / "model.bin").exists()
        report.add(Check("Audio backend", CheckStatus.PASS))
        report.add(Check("Recording directory writable", CheckStatus.PASS))
        report.add(Check(
            "Local transcription smoke test",
            CheckStatus.PASS if present else CheckStatus.FAIL,
            f"{engine_model} at {directory}"))
        report.add(Check("Local translation smoke test", CheckStatus.PASS))
        return report

    monkeypatch.setattr("babelfishr.preparation.prepare_field", fake_prepare)
    monkeypatch.setattr("babelfishr.readiness.field_check", fake_field_check)

    payload = prepare_field_job(app.config, NON_DEFAULT, [("es", "en")])

    assert prepared["model"] == NON_DEFAULT, "tiny was not the model prepared"
    assert prepared["config_model"] == NON_DEFAULT, (
        "preparation received a config still naming the default model")
    assert checked["model"] == NON_DEFAULT, (
        "Field Check inspected the default model instead of the prepared one")
    assert payload["readiness"].field_ready, (
        "readiness failed even though the selected model was prepared")
    assert payload["asr_model"] == NON_DEFAULT


def test_job_reports_the_model_it_verified(app, monkeypatch):
    import babelfishr.ui.workers as workers

    monkeypatch.setattr(
        "babelfishr.preparation.prepare_field",
        lambda config, **kwargs: PreparationResult())
    monkeypatch.setattr(
        "babelfishr.readiness.field_check",
        lambda config, run_smoke_tests=True, mode=None: ReadinessReport())

    payload = prepare_field_job(app.config, NON_DEFAULT, [])
    assert payload["asr_model"] == NON_DEFAULT


# ---- persistence on success only ---------------------------------------
def _install_job(monkeypatch, module, outcome: str):
    """Replace the worker job with one producing a chosen outcome."""
    def job(config, model, pairs, report=None, token=None):
        if outcome == "fail":
            raise RuntimeError("download failed")
        if outcome == "cancel":
            for _ in range(500):
                token.raise_if_cancelled()
                time.sleep(0.005)
        ready = ReadinessReport()
        ready.add(Check("Audio backend", CheckStatus.PASS))
        ready.add(Check("Recording directory writable", CheckStatus.PASS))
        ready.add(Check("Local transcription smoke test", CheckStatus.PASS))
        ready.add(Check("Local translation smoke test", CheckStatus.PASS))
        result = PreparationResult()
        result.add("Local ASR model", True, f"prepared {model}")
        return {"preparation": result, "readiness": ready, "asr_model": model,
                "language_pairs": list(pairs)}

    monkeypatch.setattr(module, "prepare_field_job", job)


def _assistant_with_model(app, model: str):
    import babelfishr.ui.setup_assistant as module

    assistant = module.SetupAssistant(app)
    index = assistant.model_box.findData(model)
    assert index >= 0, f"{model} is not offered by the assistant"
    assistant.model_box.setCurrentIndex(index)
    assert assistant.selected_model() == model
    return assistant


def test_successful_preparation_persists_the_selected_model(qt_app, app,
                                                            monkeypatch):
    import babelfishr.ui.setup_assistant as module

    _install_job(monkeypatch, module, "succeed")
    assistant = _assistant_with_model(app, NON_DEFAULT)
    assistant._start()
    assert _pump(qt_app, lambda: assistant.readiness is not None)
    assert assistant.readiness.field_ready

    reloaded = Config.load()
    assert reloaded.setup.asr_model == NON_DEFAULT
    assert reloaded.asr.model == NON_DEFAULT, (
        "the verified model must become the configured model")
    assert reloaded.operating_mode() is OperatingMode.FIELD_OFFLINE


def test_failed_preparation_preserves_the_previous_model(qt_app, app,
                                                         monkeypatch):
    import babelfishr.ui.setup_assistant as module

    # A previously working configuration.
    app.config.record_setup(asr_model=DEFAULT)
    assert Config.load().asr.model == DEFAULT

    _install_job(monkeypatch, module, "fail")
    assistant = _assistant_with_model(app, NON_DEFAULT)
    assistant._start()
    assert _pump(qt_app, lambda: "ERROR" in assistant.log_view.toPlainText())

    reloaded = Config.load()
    assert reloaded.asr.model == DEFAULT, (
        "a failed preparation corrupted the previously working model setting")
    assert reloaded.setup.asr_model == DEFAULT


def test_cancelled_preparation_preserves_the_previous_model(qt_app, app,
                                                            monkeypatch):
    import babelfishr.ui.setup_assistant as module

    app.config.record_setup(asr_model=DEFAULT)
    _install_job(monkeypatch, module, "cancel")
    assistant = _assistant_with_model(app, NON_DEFAULT)
    assistant._start()
    time.sleep(0.05)
    assistant._cancel()
    assert _pump(qt_app,
                 lambda: "cancelled" in assistant.status_label.text().lower())

    reloaded = Config.load()
    assert reloaded.asr.model == DEFAULT
    assert reloaded.setup.asr_model == DEFAULT


def test_unready_result_does_not_persist_the_model(qt_app, app, monkeypatch):
    """A completed run whose smoke test failed must not be recorded."""
    import babelfishr.ui.setup_assistant as module

    app.config.record_setup(asr_model=DEFAULT)

    def job(config, model, pairs, report=None, token=None):
        ready = ReadinessReport()
        ready.add(Check("Audio backend", CheckStatus.PASS))
        ready.add(Check("Recording directory writable", CheckStatus.PASS))
        ready.add(Check("Local transcription smoke test", CheckStatus.FAIL))
        return {"preparation": PreparationResult(), "readiness": ready,
                "asr_model": model, "language_pairs": list(pairs)}

    monkeypatch.setattr(module, "prepare_field_job", job)
    assistant = _assistant_with_model(app, NON_DEFAULT)
    assistant._start()
    assert _pump(qt_app, lambda: assistant.readiness is not None)
    assert not assistant.readiness.field_ready

    reloaded = Config.load()
    assert reloaded.asr.model == DEFAULT


# ---- CLI parity ---------------------------------------------------------
def test_cli_prepare_field_also_checks_the_selected_model(tmp_path, monkeypatch,
                                                          capsys):
    """The terminal path must not reintroduce the same mismatch."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path / "AppSupport"))
    checked = {}

    def fake_prepare(config, *, asr_model=None, **kwargs):
        result = PreparationResult()
        result.add("Local ASR model", True, f"prepared {asr_model}")
        return result

    def fake_field_check(config, run_smoke_tests=True, mode=None):
        checked["model"] = config.asr.model
        return ReadinessReport()

    monkeypatch.setattr("babelfishr.preparation.prepare_field", fake_prepare)
    monkeypatch.setattr("babelfishr.readiness.field_check", fake_field_check)

    from babelfishr.cli import main

    main(["prepare-field", "--asr-model", NON_DEFAULT, "--no-download"])
    assert checked.get("model") == NON_DEFAULT, (
        "the CLI ran Field Check against a different model than it prepared")
