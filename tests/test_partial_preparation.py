"""A speech model that worked must survive a translation failure.

The observed case on a real Mac: Whisper Medium downloaded, loaded offline and
passed its transcription smoke test; every Argos route then failed on a
certificate error. settings.toml still said `small`, so reopening setup went
looking for a model the operator had never asked for while a working 1.5 GB
Medium sat on disk.

Persisting the model is a statement of fact - this model is present and
verified. It is emphatically not a claim of readiness, and these tests pin
that distinction down: setup stays incomplete and Field Offline stays out of
reach while translation is unavailable.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6 import QtWidgets  # noqa: E402

from babelfishr.app import BabelFishRApp  # noqa: E402
from babelfishr.config import Config  # noqa: E402
from babelfishr.preparation import ASR_STEP, PreparationResult  # noqa: E402
from babelfishr.readiness import (Check, CheckStatus,  # noqa: E402
                                  ReadinessReport)
from babelfishr.ui.setup_assistant import SetupAssistant  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.app_home = str(tmp_path)
    cfg.database = str(tmp_path / "test.sqlite3")
    cfg.recording.directory = str(tmp_path / "recordings")
    cfg.asr.engine = "mock"
    cfg.translate.engine = "mock"
    cfg.source_path = str(tmp_path / "settings.toml")
    return cfg


@pytest.fixture
def app(config):
    application = BabelFishRApp(config=config)
    yield application
    application.close()


def observed_result() -> PreparationResult:
    """Exactly what the Mac reported: Medium fine, every route failed."""
    result = PreparationResult()
    result.add("Free storage", True, "40000 MB free")
    result.add("Recording directory writable", True, "")
    result.add(ASR_STEP, True,
               "medium at .../models/medium (1500 MB), offline transcription "
               "smoke test passed in 3.2s")
    result.add("Language pack es->en", False,
               "Could not retrieve the Argos language package index: "
               "SSLCertVerificationError: certificate verify failed: "
               "unable to get local issuer certificate")
    for pair in ("de->en", "fr->en", "uk->en", "ru->en"):
        result.add(f"Language pack {pair}", False,
                   "not attempted: the package index could not be retrieved")
    return result


def partial_readiness() -> ReadinessReport:
    """Record yes, transcribe yes, translate no."""
    report = ReadinessReport()
    report.add(Check("Audio backend", CheckStatus.PASS, ""))
    report.add(Check("Recording directory writable", CheckStatus.PASS, ""))
    report.add(Check("Local transcription smoke test", CheckStatus.PASS, ""))
    report.add(Check("Local translation smoke test", CheckStatus.FAIL,
                     "no language packages"))
    return report


def run_assistant(qt_app, app, model="medium"):
    dialog = SetupAssistant(app)
    dialog._finished({
        "preparation": observed_result(),
        "readiness": partial_readiness(),
        "asr_model": model,
        "language_pairs": [("es", "en")],
    })
    return dialog


# ---- the readiness this state must report ------------------------------
def test_the_partial_state_reports_record_yes_transcribe_yes_translate_no():
    readiness = partial_readiness()
    assert readiness.can_record is True
    assert readiness.can_transcribe is True
    assert readiness.can_translate is False
    assert readiness.field_ready is False, (
        "Field Offline must stay out of reach while translation is unavailable")


# ---- what is persisted, and what is not --------------------------------
def test_a_verified_model_is_persisted_even_though_translation_failed(
        qt_app, app, config):
    assert config.asr.model == "small"

    run_assistant(qt_app, app)

    assert config.asr.model == "medium"
    assert config.setup.asr_model == "medium"


def test_setup_stays_incomplete_and_the_mode_is_unchanged(qt_app, app, config):
    before = config.mode
    run_assistant(qt_app, app)

    assert config.setup.completed is False, (
        "a model on disk is not a prepared field kit")
    assert config.mode == before
    assert config.mode != "field-offline"
    assert config.needs_first_run_setup is True


def test_the_persisted_model_survives_a_restart(qt_app, app, config):
    import tomllib

    run_assistant(qt_app, app)
    path = config.save()

    with open(path, "rb") as handle:
        restored = Config.from_dict(tomllib.load(handle))

    assert restored.asr.model == "medium"
    assert restored.setup.asr_model == "medium"
    assert restored.setup.completed is False


def test_reopening_setup_offers_the_prepared_model_not_small(qt_app, app,
                                                             config):
    """The point of persisting it: do not re-download 480 MB of the wrong model."""
    run_assistant(qt_app, app)

    reopened = SetupAssistant(app)
    assert reopened.selected_model() == "medium"
    assert reopened.model_box.currentData() == "medium"


def test_nothing_is_persisted_when_the_model_itself_failed(qt_app, app, config):
    """The fallback is for a model that genuinely worked, not any failure."""
    result = PreparationResult()
    result.add(ASR_STEP, False, "could not prepare: connection reset")
    result.add("Language pack es->en", False, "index unavailable")

    dialog = SetupAssistant(app)
    dialog._finished({"preparation": result, "readiness": partial_readiness(),
                      "asr_model": "medium",
                      "language_pairs": [("es", "en")]})

    assert config.asr.model == "small", "a failed download must change nothing"
    assert config.setup.asr_model == ""
    assert config.setup.completed is False


def test_a_full_success_still_completes_setup_and_switches_mode(qt_app, app,
                                                                config):
    """The partial path must not have weakened the success path."""
    result = PreparationResult()
    result.add(ASR_STEP, True, "medium ok")
    result.add("Language pack es->en", True, "installed")

    ready = ReadinessReport()
    ready.add(Check("Audio backend", CheckStatus.PASS, ""))
    ready.add(Check("Recording directory writable", CheckStatus.PASS, ""))
    ready.add(Check("Local transcription smoke test", CheckStatus.PASS, ""))
    ready.add(Check("Local translation smoke test", CheckStatus.PASS, ""))
    assert ready.field_ready is True

    dialog = SetupAssistant(app)
    dialog._finished({"preparation": result, "readiness": ready,
                      "asr_model": "medium",
                      "language_pairs": [("es", "en")]})

    assert config.setup.completed is True
    assert config.mode == "field-offline"


def test_the_operator_is_told_the_model_was_kept(qt_app, app):
    dialog = run_assistant(qt_app, app)
    text = dialog.log_view.toPlainText()
    assert "Kept the prepared speech model" in text
    assert "medium" in text
    assert "still incomplete" in text
