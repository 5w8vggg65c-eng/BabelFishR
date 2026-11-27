"""Installing large-v3-turbo (or any missing Whisper model) without a restart.

Setup Assistant lists Turbo. Local Engines has Install... beside the model
picker: in Online / Setup it offers each model that is missing (Install)
or incomplete (Repair); in Offline Field Use and Record Only, and while
monitoring, it is disabled and nothing reaches the network. Installing
reuses the preparation step (download or repair, then a transcription with
downloads disabled), leaves the selected model alone, and refreshes the
picker so the new model can be chosen at once. A failure or cancel is
reported and leaves an incomplete model marked Incomplete.

The download itself is stood in for (no network): it writes the model
files, or fails, or is cancelled.
"""

from __future__ import annotations

import time

import pytest
from PySide6 import QtWidgets

from babelfishr.app import BabelFishRApp
from babelfishr.modes import OfflineViolation, OperatingMode
from test_offline_indicator import Jobs

TURBO = "large-v3-turbo"


@pytest.fixture(scope="module")
def qt_app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pump(qt_app, rounds=15):
    for _ in range(rounds):
        qt_app.processEvents()
        time.sleep(0.003)


def complete_files(directory):
    from babelfishr.providers.whisper_local import REQUIRED_ASSETS

    directory.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_ASSETS + ("tokenizer.json",):
        (directory / name).write_bytes(b"synthetic")


@pytest.fixture
def fake_preparation(monkeypatch):
    """prepare_model and the offline smoke test, stood in for."""
    import babelfishr.providers.whisper_local as whisper_local

    calls = {"downloads": [], "fail": None, "warm": []}

    def prepare_model(name, models_root, **_progress):
        from babelfishr.providers.whisper_local import model_directory_for

        calls["downloads"].append(name)
        if calls["fail"]:
            raise RuntimeError(calls["fail"])
        directory = model_directory_for(models_root, name)
        complete_files(directory)
        return directory

    class Result:
        engine_version = "test"

    monkeypatch.setattr(whisper_local, "prepare_model", prepare_model)
    monkeypatch.setattr(whisper_local.FasterWhisperEngine, "warm_up",
                        lambda self: calls["warm"].append(self.model_size) or Result())
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        import sys
        import types
        monkeypatch.setitem(sys.modules, "faster_whisper",
                            types.SimpleNamespace(WhisperModel=object))
    return calls


@pytest.fixture
def window(qt_app, config, store, tmp_path, monkeypatch):
    from babelfishr.ui.main_window import MainWindow

    config.app_home = str(tmp_path / "home")
    config.mode = OperatingMode.ONLINE_SETUP.value
    config.asr.model = "small"
    Jobs(monkeypatch)                      # readiness stays out of the way
    win = MainWindow(BabelFishRApp(config=config, store=store))
    win.show()
    pump(qt_app)
    yield win
    win.close()


def _wait_installed(qt_app, window, timeout=15):
    deadline = time.monotonic() + timeout
    while window._install_worker is not None and time.monotonic() < deadline:
        pump(qt_app)
    assert window._install_worker is None, "the install did not finish"


def _menu_labels(window):
    window._fill_install_model_menu()
    return [a.text() for a in window.install_model_menu.actions()]


def test_setup_assistant_lists_turbo():
    from babelfishr.ui.setup_assistant import MODELS

    assert TURBO in [name for name, _note, _size in MODELS]


def test_install_from_local_engines_refreshes_without_restart(
        qt_app, window, fake_preparation, monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: shown.append(a[2]))
    asked = []
    monkeypatch.setattr(window, "_choose", lambda *a, **k: asked.append(a) or "Download")
    assert window.install_model_button.isEnabled()
    assert any("Large v3 turbo" in label for label in _menu_labels(window))
    index = window.whisper_model_box.findData(TURBO)
    assert not window.whisper_model_box.model().item(index).isEnabled()

    window._install_model(TURBO)
    _wait_installed(qt_app, window)
    assert fake_preparation["downloads"] == [TURBO]
    assert fake_preparation["warm"] == [TURBO], ("not verified offline", shown)
    assert asked and "1600" in asked[0][1]
    assert window.app.config.asr.model == "small", "the active model was changed"
    index = window.whisper_model_box.findData(TURBO)
    assert window.whisper_model_box.model().item(index).isEnabled()
    assert "Installed" in window.whisper_model_box.itemText(index)
    assert "installed and verified" in window.install_panel.status.text()
    assert not any("Large v3 turbo" in label for label in _menu_labels(window))
    window.whisper_model_box.setCurrentIndex(index)  # selectable now, no restart
    assert window.app.config.asr.model == TURBO


def test_an_incomplete_model_is_offered_as_repair(qt_app, window, fake_preparation,
                                                  monkeypatch):
    directory = window.app.whisper_model_directory(TURBO)
    directory.mkdir(parents=True)
    (directory / "config.json").write_text("{}")
    assert any(label.startswith("Repair Large v3 turbo") for label in _menu_labels(window))
    monkeypatch.setattr(QtWidgets.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(window, "_choose", lambda *a, **k: "Download")
    window._install_model(TURBO)
    _wait_installed(qt_app, window)
    assert fake_preparation["downloads"] == [TURBO]


def test_a_failed_download_is_reported_and_stays_unselectable(qt_app, window,
                                                              fake_preparation,
                                                              monkeypatch):
    shown = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: shown.append(a[2]))
    monkeypatch.setattr(window, "_choose", lambda *a, **k: "Download")
    fake_preparation["fail"] = "connection reset"
    window._install_model(TURBO)
    _wait_installed(qt_app, window)
    assert "was not installed" in window.install_panel.status.text() and "connection reset" in window.install_panel.status.text()
    index = window.whisper_model_box.findData(TURBO)
    assert not window.whisper_model_box.model().item(index).isEnabled()
    assert window.install_model_button.isEnabled(), "a retry is not possible"


def test_declining_the_question_downloads_nothing(qt_app, window, fake_preparation,
                                                  monkeypatch):
    monkeypatch.setattr(window, "_choose", lambda *a, **k: "")
    window._install_model(TURBO)
    pump(qt_app)
    assert window._install_worker is None and fake_preparation["downloads"] == []


@pytest.mark.parametrize("mode", [OperatingMode.FIELD_OFFLINE, OperatingMode.RECORD_ONLY])
def test_no_install_in_offline_modes(qt_app, window, fake_preparation, monkeypatch, mode):
    monkeypatch.setattr(window.app, "set_mode",
                        lambda value: setattr(window.app.config, "mode", value))
    window.app.config.mode = mode.value
    window._refresh_install_model_button()
    assert not window.install_model_button.isEnabled()
    assert "Online / Setup" in window.install_model_button.toolTip()
    monkeypatch.setattr(window, "_choose", lambda *a, **k: "Download")
    window._install_model(TURBO)
    pump(qt_app)
    assert fake_preparation["downloads"] == []


def test_the_preparation_step_itself_refuses_offline(config, tmp_path, fake_preparation):
    from babelfishr.preparation import install_asr_model

    config.app_home = str(tmp_path / "home")
    with pytest.raises(OfflineViolation):
        install_asr_model(config, TURBO, mode=OperatingMode.FIELD_OFFLINE)
    assert fake_preparation["downloads"] == []
    step, ok, detail = install_asr_model(config, "large-v9", mode=OperatingMode.ONLINE_SETUP)
    assert not ok and "unknown" in detail


def test_not_while_monitoring(qt_app, window, fake_preparation, monkeypatch):
    window.app.session = object()
    try:
        window._refresh_install_model_button()
        assert not window.install_model_button.isEnabled()
        assert "Stop monitoring" in window.install_model_button.toolTip()
    finally:
        window.app.session = None


# --- the name faster-whisper is given ------------------------------------------

@pytest.mark.parametrize("known,expected", [
    (["large-v3", "large-v3-turbo", "turbo"], "large-v3-turbo"),       # 1.1.0 and later
    (["large-v3", "distil-large-v3"], "mobiuslabsgmbh/faster-whisper-large-v3-turbo"),  # 1.0.x
])
def test_turbo_resolves_on_old_and_new_faster_whisper(monkeypatch, known, expected):
    import sys
    import types

    import babelfishr.providers.whisper_local as whisper_local

    utils = types.SimpleNamespace(available_models=lambda: known)
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(utils=utils, WhisperModel=object))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    assert whisper_local.download_source(TURBO) == expected
    assert whisper_local.download_source("small") == "small"


def test_the_local_folder_is_named_turbo_whatever_the_source(monkeypatch, tmp_path):
    """On a faster-whisper that does not know "large-v3-turbo" the canonical
    repository is fetched, into a folder still named after the model. The
    Hub is stood in for: no network."""
    import sys
    import types

    import huggingface_hub

    import babelfishr.providers.whisper_local as whisper_local

    asked = []

    class Api:
        def model_info(self, repo_id, files_metadata=False):
            asked.append(("info", repo_id))
            return types.SimpleNamespace(siblings=[
                types.SimpleNamespace(rfilename=name, size=4, lfs=None)
                for name in ("config.json", "model.bin", "tokenizer.json",
                             "README.md")])

    def hf_hub_download(repo_id, filename, local_dir, tqdm_class=None, **_):
        asked.append((repo_id, filename, local_dir))
        (whisper_local.pathlib.Path(local_dir) / filename).write_bytes(b"abcd")
        return str(whisper_local.pathlib.Path(local_dir) / filename)

    monkeypatch.setattr(huggingface_hub, "HfApi", Api)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
    utils = types.SimpleNamespace(available_models=lambda: ["large-v3"])
    monkeypatch.setitem(sys.modules, "faster_whisper",
                        types.SimpleNamespace(utils=utils, WhisperModel=object))
    monkeypatch.setitem(sys.modules, "faster_whisper.utils", utils)
    resolved = whisper_local.prepare_model(TURBO, tmp_path / "models")
    repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    folder = str(tmp_path / "models" / TURBO)
    assert asked[0] == ("info", repo)
    # The same files faster-whisper fetches (README.md is not one), weights last.
    assert asked[1:] == [(repo, "config.json", folder),
                         (repo, "tokenizer.json", folder),
                         (repo, "model.bin", folder)]
    assert resolved.name == TURBO
