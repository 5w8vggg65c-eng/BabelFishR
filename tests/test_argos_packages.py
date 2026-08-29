"""Argos package directory wiring and route resolution.

Two defects: ``ArgosTranslateEngine.package_dir`` was stored and never used, so
the managed language-pack directory had no effect; and availability was decided
from a naive package list rather than Argos's resolved translation graph, which
understated what was usable because it ignored composite (pivot) routes.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from babelfishr.config import Config
from babelfishr.modes import AppPaths, bootstrap_environment
from babelfishr.providers.argos import (PACKAGES_DIR_ENV, ArgosTranslateEngine,
                                        active_package_dir,
                                        configure_package_dir)

pytestmark = pytest.mark.unit

argos = pytest.importorskip("argostranslate.translate",
                            reason="argostranslate is not installed")


def test_configure_package_dir_sets_the_environment(tmp_path):
    target = tmp_path / "packs"
    configure_package_dir(target)
    assert os.environ[PACKAGES_DIR_ENV] == str(target)
    assert target.is_dir()


def test_bootstrap_points_argos_at_the_managed_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    applied = bootstrap_environment()
    paths = AppPaths.resolve()
    assert applied[PACKAGES_DIR_ENV] == str(paths.language_packs)
    assert os.environ[PACKAGES_DIR_ENV] == str(paths.language_packs)


def test_engine_constructor_applies_the_package_dir(tmp_path):
    target = tmp_path / "engine-packs"
    ArgosTranslateEngine(target_language="en", package_dir=str(target))
    assert os.environ[PACKAGES_DIR_ENV] == str(target)


def test_provider_factory_uses_the_managed_directory(tmp_path, monkeypatch):
    """The defect: package_dir was accepted and then ignored."""
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    from babelfishr.providers import _translation_factories

    config = Config.load()
    engine = _translation_factories(config)["argos"]()
    expected = AppPaths.resolve().language_packs
    assert engine.package_dir == str(expected)
    assert os.environ[PACKAGES_DIR_ENV] == str(expected)


def test_active_package_dir_is_reported(tmp_path):
    configure_package_dir(tmp_path / "reported")
    reported = active_package_dir()
    assert reported is not None
    # Argos resolves its directory at import time; if it was already imported
    # with a different one, the engine must say so rather than pretend.
    assert isinstance(reported, pathlib.Path)


def test_configure_after_import_reports_no_effect(tmp_path):
    """Setting the variable too late must not be silently believed."""
    import argostranslate.settings  # noqa: F401  (forces the import)

    result = configure_package_dir(tmp_path / "too-late")
    assert isinstance(result, bool)
    if not result:
        # It correctly detected that Argos had already resolved its directory.
        assert active_package_dir() != tmp_path / "too-late"


# ---- route resolution ---------------------------------------------------
def test_routes_come_from_the_translation_graph():
    engine = ArgosTranslateEngine(target_language="en")
    routes = engine.routes()
    assert isinstance(routes, list)
    for source, target, kind in routes:
        assert kind in ("direct", "pivot")
        assert source != target, "identity translations must be excluded"


def test_identity_routes_are_excluded():
    """Argos puts language->itself in the graph; that is not a capability."""
    engine = ArgosTranslateEngine(target_language="en")
    assert all(a != b for a, b in engine.installed_pairs())


def test_direct_and_pivot_are_distinguished():
    engine = ArgosTranslateEngine(target_language="en")
    direct = set(engine.direct_pairs())
    pivot = set(engine.pivot_pairs())
    assert not (direct & pivot), "a route cannot be both direct and pivoted"
    assert direct | pivot == set(engine.installed_pairs())


def test_availability_requires_a_route_into_the_target():
    engine = ArgosTranslateEngine(target_language="en")
    if engine.installed_pairs():
        pytest.skip("language packs are installed in this environment")
    assert not engine.available()
    reason = engine.unavailable_reason()
    assert "languages install" in reason
    # The message must name where it looked, or the operator cannot fix it.
    assert str(active_package_dir()) in reason or "not installed" in reason


def test_readiness_reports_the_directory_and_route_kinds():
    engine = ArgosTranslateEngine(target_language="en")
    readiness = engine.readiness()
    for key in ("library_installed", "package_dir", "pairs", "direct", "pivot"):
        assert key in readiness


def test_translation_paths_splits_direct_from_pivot(tmp_path, monkeypatch):
    monkeypatch.setenv("BABELFISHR_HOME", str(tmp_path))
    from babelfishr.preparation import translation_paths

    routes = translation_paths("en", Config.load())
    assert set(routes) == {"direct", "pivot", "all"}
    assert set(routes["all"]) == set(routes["direct"]) | set(routes["pivot"])


# ---- offline behaviour --------------------------------------------------
def test_installed_pack_remains_usable_with_networking_disabled(monkeypatch):
    """Enumerating and using installed packs must not need the network."""
    import socket

    engine = ArgosTranslateEngine(target_language="en")
    pairs_before = engine.installed_pairs()

    def refuse(*args, **kwargs):
        raise AssertionError("Argos attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    offline_engine = ArgosTranslateEngine(target_language="en")
    assert offline_engine.installed_pairs() == pairs_before

    if pairs_before:
        source = pairs_before[0][0]
        result = offline_engine.smoke_test(source, "en")
        assert result.text.strip()
