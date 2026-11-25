"""KingFishR runs with its own identity and data, never BabelFishR's.

Started from its own entry point, KingFishR resolves its settings, database,
recordings, models and logs under a KingFishR data folder and reads only
KINGFISHR_* variables. BabelFishR's data - even with BABELFISHR_HOME and
BABELFISHR_DB set - is not opened, created, copied or changed. Synthetic
temporary folders only.
"""

from __future__ import annotations

import hashlib
import pathlib

import pytest

from babelfishr import extensions
from babelfishr.config import Config
from babelfishr.modes import AppPaths


def _snapshot(root: pathlib.Path):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fake home with BabelFishR data in it, and BabelFishR's variables
    pointing at that data."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("KINGFISHR_HOME", raising=False)
    monkeypatch.delenv("KINGFISHR_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    bfr = tmp_path / "babelfishr-data"
    (bfr / "Recordings").mkdir(parents=True)
    (bfr / "babelfishr.sqlite3").write_bytes(b"BabelFishR database (synthetic)")
    (bfr / "settings.toml").write_text('[translate]\ntarget_language = "fr"\n')
    (bfr / "Recordings" / "tx.wav").write_bytes(b"RIFF synthetic")
    (bfr / "models").mkdir()
    (bfr / "models" / "model.bin").write_bytes(b"large model (synthetic)")
    monkeypatch.setenv("BABELFISHR_HOME", str(bfr))
    monkeypatch.setenv("BABELFISHR_DB", str(bfr / "babelfishr.sqlite3"))
    monkeypatch.setenv("BABELFISHR_RECORDINGS", str(bfr / "Recordings"))
    (tmp_path / "babelfishr.toml").write_text('[translate]\ntarget_language = "de"\n')
    return home, bfr


def test_kingfishr_has_its_own_identity():
    product = extensions.product()
    assert product.name == "KingFishR" and product.env_prefix == "KINGFISHR"
    assert product.database_name == "kingfishr.sqlite3"


def test_kingfishr_resolves_its_own_data_and_ignores_babelfishr(homes):
    home, bfr = homes
    before = _snapshot(bfr)
    config = Config.load()
    paths = config.paths()
    assert paths.root.name == "KingFishR"
    assert bfr not in paths.root.parents and paths.root != bfr
    assert pathlib.Path(config.database).name == "kingfishr.sqlite3"
    assert pathlib.Path(config.database).parent == paths.root
    assert pathlib.Path(config.recording.directory) == paths.recordings
    # Neither BabelFishR's settings file nor its working-directory config.
    assert config.source_path is None
    assert config.translate.target_language not in ("fr", "de")
    assert paths.settings.parent == paths.root
    # Nothing of BabelFishR's was opened into, created, copied or changed.
    paths.ensure()
    assert _snapshot(bfr) == before
    assert not any(paths.root.rglob("*.sqlite3"))
    assert not any(paths.models.iterdir())
    assert not any(paths.recordings.iterdir())


def test_kingfishr_reads_its_own_variables(homes, tmp_path, monkeypatch):
    home, bfr = homes
    own = tmp_path / "kingfishr-data"
    monkeypatch.setenv("KINGFISHR_HOME", str(own))
    config = Config.load()
    assert config.paths().root == own
    assert pathlib.Path(config.database) == own / "kingfishr.sqlite3"


def test_kingfishr_opens_its_own_database(homes):
    from babelfishr.app import BabelFishRApp

    home, bfr = homes
    before = _snapshot(bfr)
    app = BabelFishRApp(config=Config.load())
    try:
        assert pathlib.Path(app.store.path).name == "kingfishr.sqlite3"
        assert bfr not in pathlib.Path(app.store.path).parents
    finally:
        app.close()
    assert _snapshot(bfr) == before


def test_back_to_babelfishr_after_kingfishr(homes):
    """The test fixture's reset: the same process as BabelFishR again uses
    BabelFishR's names (so the identity is the product's, not global)."""
    home, bfr = homes
    extensions.reset()
    assert AppPaths.resolve().root == bfr
    assert Config.load().database == str(bfr / "babelfishr.sqlite3")


def test_the_window_says_kingfishr_and_adds_the_receiver_menu(homes):
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.app import BabelFishRApp
    from babelfishr.providers.mock import MockTranscriptionEngine, MockTranslationEngine
    from babelfishr.ui.main_window import MainWindow

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    config = Config.load()
    config.asr.engine = config.translate.engine = "mock"
    app = BabelFishRApp(config=config)
    app.transcription, app.translation = MockTranscriptionEngine(), MockTranslationEngine()
    window = MainWindow(app)
    try:
        assert window.windowTitle() == "KingFishR"
        menus = [a.text().replace("&", "") for a in window.menuBar().actions()]
        assert menus == ["File", "View", "Receiver", "Tools", "Help"]
        assert window.receiver_line.text().startswith("Receiver:")
    finally:
        window.close()
        app.close()


def test_kingfishr_adds_the_analyze_command():
    from babelfishr.cli import build_parser

    parser = build_parser()
    commands = parser._subparsers._group_actions[0].choices
    assert "analyze" in commands and parser.prog == "kingfishr"


def test_a_message_menu_offers_digital_analysis_in_kingfishr():
    pytest.importorskip("PySide6")
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.models import Transmission
    from babelfishr.ui.timeline import TimelineView

    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    view = TimelineView()          # kept: the bubble belongs to it
    bubble = view.add(Transmission(transcript="hi", audio_path="/x.wav"))
    labels = {a.text() for a in bubble.menu_button.menu().actions() if a.text()}
    assert any(label.startswith("Analyze as digital") for label in labels)
    assert any("specific mode" in label for label in labels)
    assert bubble.analyze_action.isEnabled()


def test_config_write_uses_kingfishrs_file_and_leaves_babelfishrs_alone(homes, tmp_path):
    """`kingfishr config --write` with no path writes kingfishr.toml in the
    working directory; an existing babelfishr.toml there is not changed."""
    from babelfishr.cli import main

    home, bfr = homes
    existing = tmp_path / "babelfishr.toml"
    before = existing.read_bytes()
    data_before = _snapshot(bfr)
    assert main(["config", "--write"]) == 0
    assert (tmp_path / "kingfishr.toml").is_file()
    assert existing.read_bytes() == before
    assert _snapshot(bfr) == data_before   # BabelFishR's data folder untouched
