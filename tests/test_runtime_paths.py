"""Runtime path resolution and settings persistence.

Two defects are covered here.

*Paths*: ``Config.database`` and ``RecordingConfig.directory`` defaulted to the
relative strings ``babelfishr.sqlite3`` and ``recordings``, while readiness
checked Application Support. A double-clicked ``.app`` has a working directory
of ``/``, so the two never agreed and the database landed wherever Finder
happened to launch from.

*Settings*: ``set_mode()`` changed memory only, so an operator who selected
Field Offline was back in Online/Setup after relaunching - precisely the state
where a cloud engine becomes selectable again.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from babelfishr.config import Config
from babelfishr.modes import AppPaths, OperatingMode

pytestmark = pytest.mark.unit


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "Application Support" / "BabelFishR"
    monkeypatch.setenv("BABELFISHR_HOME", str(home))
    return home


# ---- defaults land in Application Support ------------------------------
def test_defaults_resolve_under_application_support(app_home):
    config = Config.load()
    assert pathlib.Path(config.database).is_absolute()
    assert pathlib.Path(config.recording.directory).is_absolute()
    assert str(app_home) in config.database
    assert str(app_home) in config.recording.directory


def test_defaults_match_apppaths_exactly(app_home):
    config = Config.load()
    paths = AppPaths.resolve()
    assert pathlib.Path(config.database) == paths.database
    assert pathlib.Path(config.recording.directory) == paths.recordings


def test_packaged_app_writes_nothing_to_the_working_directory(app_home, tmp_path,
                                                              monkeypatch):
    """The core defect: a Finder-launched app must not litter its cwd."""
    workdir = tmp_path / "somewhere-else"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    from babelfishr.storage import Store

    config = Config.load()
    store = Store(config.database, recordings_dir=config.recording.directory)
    session = store.save_session(
        __import__("babelfishr.models", fromlist=["Session"]).Session())
    assert session is not None
    store.close()

    leaked = [p.name for p in workdir.iterdir()]
    assert leaked == [], f"the app wrote into its working directory: {leaked}"
    assert pathlib.Path(config.database).exists()


def test_recorder_and_store_agree_with_readiness(app_home):
    """Store, Recorder and Field Check must resolve the same locations."""
    from babelfishr.pipeline import Recorder
    from babelfishr.readiness import field_check
    from babelfishr.storage import Store

    config = Config.load()
    store = Store(config.database, recordings_dir=config.recording.directory)
    recorder = Recorder(directory=config.recording.directory)
    paths = config.paths()

    assert pathlib.Path(store.path) == paths.database
    assert recorder.directory == paths.recordings

    report = field_check(config, run_smoke_tests=False,
                         mode=OperatingMode.FIELD_OFFLINE)
    check = report.get("Recording directory writable")
    assert str(paths.recordings) in check.detail
    store.close()


# ---- explicit overrides still win --------------------------------------
def test_absolute_override_wins(app_home, tmp_path):
    elsewhere = tmp_path / "external" / "db.sqlite3"
    config = Config.from_dict({"database": str(elsewhere)})
    config.resolve_runtime_paths()
    assert pathlib.Path(config.database) == elsewhere


def test_environment_override_wins(app_home, tmp_path, monkeypatch):
    elsewhere = tmp_path / "env" / "db.sqlite3"
    monkeypatch.setenv("BABELFISHR_DB", str(elsewhere))
    config = Config.load()
    assert pathlib.Path(config.database) == elsewhere


def test_relative_path_resolves_against_the_config_file_not_cwd(app_home,
                                                                tmp_path,
                                                                monkeypatch):
    """A relative path must not follow Finder's working directory around."""
    config_dir = tmp_path / "project"
    config_dir.mkdir()
    config_file = config_dir / "babelfishr.toml"
    config_file.write_text('database = "data/local.sqlite3"\n', encoding="utf-8")

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    config = Config.load(str(config_file))
    assert pathlib.Path(config.database) == config_dir / "data" / "local.sqlite3"
    assert str(unrelated) not in config.database


# ---- settings persistence ----------------------------------------------
def test_settings_save_to_application_support(app_home):
    config = Config.load()
    path = config.save()
    assert pathlib.Path(path) == AppPaths.resolve().settings
    assert pathlib.Path(path).exists()


def test_settings_are_written_atomically(app_home):
    """A crash mid-write must not leave a truncated settings file."""
    config = Config.load()
    target = pathlib.Path(config.save())
    assert target.exists()
    assert not target.with_name(target.name + ".tmp").exists(), (
        "the temporary file must be renamed, not left behind")


def test_mode_survives_restart(app_home):
    from babelfishr.app import BabelFishRApp
    from babelfishr.storage import Store

    config = Config.load()
    app = BabelFishRApp(config=config, store=Store(":memory:"))
    app.set_mode(OperatingMode.FIELD_OFFLINE.value)

    reloaded = Config.load()
    assert reloaded.operating_mode() is OperatingMode.FIELD_OFFLINE


def test_record_only_choice_survives_restart(app_home):
    from babelfishr.app import BabelFishRApp
    from babelfishr.storage import Store

    config = Config.load()
    app = BabelFishRApp(config=config, store=Store(":memory:"))
    app.set_mode(OperatingMode.RECORD_ONLY.value)
    config.record_setup(record_only=True)

    reloaded = Config.load()
    assert reloaded.operating_mode() is OperatingMode.RECORD_ONLY
    assert reloaded.setup.record_only_acknowledged
    assert not reloaded.needs_first_run_setup


def test_setup_choices_survive_restart(app_home):
    config = Config.load()
    config.record_setup(asr_model="small", language_pairs=[("es", "en"),
                                                           ("de", "en")],
                        audio_device="USB Audio CODEC")

    reloaded = Config.load()
    assert reloaded.setup.completed
    assert reloaded.setup.asr_model == "small"
    assert reloaded.asr.model == "small"
    assert reloaded.setup.language_pairs == ["es-en", "de-en"]
    assert reloaded.setup.audio_device == "USB Audio CODEC"
    assert reloaded.audio.device == "USB Audio CODEC"


def test_first_run_flag_starts_true_and_clears(app_home):
    assert Config.load().needs_first_run_setup
    Config.load().record_setup(asr_model="tiny")
    assert not Config.load().needs_first_run_setup


def test_saving_does_not_hijack_an_explicit_config_file(app_home, tmp_path):
    """An operator's own config file keeps ownership of its settings."""
    external = tmp_path / "mine.toml"
    external.write_text('log_level = "DEBUG"\n', encoding="utf-8")

    config = Config.load(str(external))
    assert config.settings_path() == external
    config.save()
    assert external.read_text(encoding="utf-8")
    assert not AppPaths.resolve().settings.exists(), (
        "an explicit config must not be silently redirected to the managed file")


def test_settings_round_trip_through_toml(app_home):
    config = Config.load()
    config.mode = OperatingMode.FIELD_OFFLINE.value
    config.setup.language_pairs = ["es-en"]
    path = config.save()

    reloaded = Config.load(path)
    assert reloaded.mode == OperatingMode.FIELD_OFFLINE.value
    assert reloaded.setup.language_pairs == ["es-en"]
