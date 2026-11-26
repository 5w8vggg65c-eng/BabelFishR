"""Operator model selection uses installed weights and preserves in-flight work."""

from unittest.mock import Mock

import pytest

from babelfishr.app import ProcessingBusy
from babelfishr.config import Config
from babelfishr.providers.whisper_local import FasterWhisperEngine


def installed(directory):
    directory.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (directory / name).write_bytes(b"fixture")


def test_switch_uses_managed_weights_and_persists(app, tmp_path, monkeypatch):
    cfg = app.config
    cfg.app_home = str(tmp_path / "home")
    cfg.asr.engine = "faster-whisper"
    cfg.asr.model_path = str(tmp_path / "custom-small")
    installed(app.whisper_model_directory("small"))
    managed = app.whisper_model_directory("medium")
    installed(managed)
    monkeypatch.setattr(FasterWhisperEngine, "library_installed", lambda self: True)
    monkeypatch.setattr(FasterWhisperEngine, "prepare",
                        lambda *args, **kwargs: pytest.fail("unexpected download"))
    app.select_engines()
    assert app.transcription.model_directory() == tmp_path / "custom-small"
    app.set_whisper_model("medium")
    app.select_engines()
    assert app.transcription.model_size == "medium"
    assert app.transcription.model_directory() == managed
    reloaded = Config.load(str(cfg.settings_path()))
    assert reloaded.asr.model == reloaded.setup.asr_model == "medium"
    assert not reloaded.asr.model_path


def test_queued_or_live_work_retains_its_model(app, monkeypatch):
    engine = app.transcription = Mock()
    with monkeypatch.context() as patch:
        patch.setattr(app, "session", object())
        with pytest.raises(ProcessingBusy):
            app.set_whisper_model("medium")
    with monkeypatch.context() as patch:
        patch.setattr(app, "standalone_pipeline", Mock(accepting=True, pending=1))
        with pytest.raises(ProcessingBusy):
            app.set_whisper_model("medium")
    assert app.transcription is engine
    engine.close.assert_not_called()
    assert app.config.asr.model == "small"


def test_missing_weights_and_failed_save_preserve_selection(app, tmp_path, monkeypatch):
    cfg = app.config
    cfg.app_home = str(tmp_path / "home")
    cfg.asr.model_path = str(tmp_path / "custom")
    incomplete = app.whisper_model_directory("medium")
    incomplete.mkdir(parents=True)
    (incomplete / "model.bin").touch()
    with pytest.raises(ValueError, match="incomplete"):
        app.set_whisper_model("medium")
    assert cfg.asr.model == "small"
    installed(app.whisper_model_directory("large-v3"))
    monkeypatch.setattr(cfg, "save", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        app.set_whisper_model("large-v3")
    assert cfg.asr.model == "small" and cfg.asr.model_path == str(tmp_path / "custom")
    assert cfg.setup.asr_model == ""


def test_picker_exposes_installed_missing_and_current_model(app, tmp_path, monkeypatch):
    widgets = pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qapp = widgets.QApplication.instance() or widgets.QApplication([])
    from babelfishr.ui.local_engines import WhisperModelPicker

    app.config.app_home = str(tmp_path / "home")
    installed(app.whisper_model_directory("medium"))
    box = WhisperModelPicker(app)
    assert box.currentData() == "small" and "Not installed" in box.currentText()
    mid = box.findData("medium")
    assert box.model().item(mid).isEnabled() and "Installed" in box.itemText(mid)
    assert not box.model().item(box.findData("large-v3")).isEnabled()
    with monkeypatch.context() as patch:
        patch.setattr(app, "session", object())
        box.refresh_availability()
        assert not box.isEnabled()
    box.refresh_availability()
    assert box.isEnabled()
    box.close()
    qapp.processEvents()


def test_selecting_in_console_applies_the_displayed_backend(app, tmp_path, monkeypatch):
    widgets = pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    qapp = widgets.QApplication.instance() or widgets.QApplication([])
    from babelfishr.ui.main_window import MainWindow

    app.config.app_home = str(tmp_path / "home")
    app.config.asr.engine = "faster-whisper"
    installed(app.whisper_model_directory("small"))
    installed(app.whisper_model_directory("medium"))
    monkeypatch.setattr(FasterWhisperEngine, "library_installed", lambda self: True)
    monkeypatch.setattr(MainWindow, "_refresh_readiness", lambda *a, **kw: None)
    window = MainWindow(app)
    window.whisper_model_box.setCurrentIndex(window.whisper_model_box.findData("medium"))
    assert app.transcription.model_size == "medium"
    assert "CPU / int8" in window.speech_engine_line.text()
    assert "Medium" in window.whisper_model_box.currentText()
    assert "Mock translation" in window.translation_engine_line.text()
    assert Config.load(str(app.config.settings_path())).asr.model == "medium"
    window.close()
    qapp.processEvents()
