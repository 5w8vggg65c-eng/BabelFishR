"""The glossary must live where the rest of the application's data lives.

The defect: ``BabelFishRApp._load_glossary()`` and ``save_glossary()`` used
``~/.config/babelfishr/glossary.json`` while ``Config.glossary_file()``
resolved the managed Application Support location. An operator's callsign list
was therefore written somewhere the rest of the app, its exports and its
uninstall path knew nothing about.
"""

from __future__ import annotations

import pathlib

import pytest

from babelfishr.app import BabelFishRApp
from babelfishr.config import Config
from babelfishr.modes import AppPaths
from babelfishr.storage import Store

pytestmark = pytest.mark.unit

LEGACY = pathlib.Path.home() / ".config" / "babelfishr" / "glossary.json"


@pytest.fixture
def app_home(tmp_path, monkeypatch):
    home = tmp_path / "Application Support" / "BabelFishR"
    monkeypatch.setenv("BABELFISHR_HOME", str(home))
    return home


@pytest.fixture
def app(app_home):
    return BabelFishRApp(config=Config.load(), store=Store(":memory:"))


def test_glossary_path_comes_from_the_configuration(app, app_home):
    assert app.glossary_path() == Config.load().glossary_file()
    assert str(app_home) in str(app.glossary_path())


def test_glossary_path_is_under_application_support(app, app_home):
    path = app.glossary_path()
    assert path.parent == AppPaths.resolve().root
    assert path.name == "glossary.json"


def test_glossary_round_trips_through_the_managed_path(app, app_home):
    app.glossary.add("KD8XYZ", category="callsign", never_translate=True)
    app.glossary.add("Puente Nuevo", "New Bridge", category="place")
    saved = pathlib.Path(app.save_glossary())

    assert saved == app.glossary_path()
    assert saved.exists()

    # A fresh application instance must find it.
    reloaded = BabelFishRApp(config=Config.load(), store=Store(":memory:"))
    assert len(reloaded.glossary) == 2
    assert reloaded.glossary.protected() == ["KD8XYZ"]
    assert reloaded.glossary.mapping()["Puente Nuevo"] == "New Bridge"


def test_glossary_is_not_written_to_the_working_directory(app, app_home,
                                                          tmp_path,
                                                          monkeypatch):
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    app.glossary.add("N0CALL", category="callsign")
    app.save_glossary()

    assert list(workdir.iterdir()) == [], (
        "the glossary leaked into the working directory")


def test_glossary_is_not_written_to_the_legacy_location(app, app_home):
    existed = LEGACY.exists()
    before = LEGACY.stat().st_mtime if existed else None

    app.glossary.add("W1AW", category="callsign")
    saved = pathlib.Path(app.save_glossary())

    assert LEGACY not in (saved, saved.resolve())
    if existed:
        assert LEGACY.stat().st_mtime == before, "the legacy file was rewritten"
    else:
        assert not LEGACY.exists(), "a legacy glossary file was created"


def test_explicit_glossary_path_still_wins(app_home, tmp_path):
    external = tmp_path / "team" / "glossary.json"
    config = Config.load()
    config.translate.glossary_path = str(external)
    config.resolve_runtime_paths()

    app = BabelFishRApp(config=config, store=Store(":memory:"))
    assert app.glossary_path() == external

    app.glossary.add("QRZ", category="brevity")
    assert pathlib.Path(app.save_glossary()) == external
    assert external.exists()


def test_missing_glossary_loads_empty_without_error(app, app_home):
    assert not app.glossary_path().exists()
    assert len(app.glossary) == 0


def test_no_glossary_path_is_built_from_the_legacy_config_directory():
    """Guard against the old location creeping back in.

    Checked line by line: ``~/.config/babelfishr`` legitimately survives as a
    read-only fallback for a .env file and for locating a legacy config file,
    so a file-level search would flag those and prove nothing.
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "babelfishr"
    offenders = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            if '".config"' in line and "glossary" in line.lower():
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"legacy ~/.config glossary path at {offenders}"


def test_load_and_save_use_the_same_resolver(app, app_home, monkeypatch):
    """One path, consulted by both directions - not two that can drift apart."""
    seen = []
    original = app.glossary_path

    def spy():
        path = original()
        seen.append(path)
        return path

    monkeypatch.setattr(app, "glossary_path", spy)
    app.glossary.add("K1ABC", category="callsign")
    saved = pathlib.Path(app.save_glossary())
    loaded = app._load_glossary()

    assert saved in seen, "save did not go through the resolver"
    assert len(seen) >= 2, "load did not go through the resolver"
    assert len(set(seen)) == 1, "load and save resolved different paths"
    assert len(loaded) == 1
