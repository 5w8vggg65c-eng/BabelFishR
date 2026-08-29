"""Argos Translate's own directories, kept inside BabelFishR's managed root.

argostranslate.settings resolves a data, config and cache directory from the
XDG variables **at import time** and creates all three immediately.
ARGOS_PACKAGES_DIR moves only the installed packages, so up to alpha 2 the
package index, the downloads cache and the configuration still landed in three
folders in the operator's home that BabelFishR never mentioned - and that its
own uninstaller could not have found.

The first test here runs a real interpreter against the really-installed
argostranslate rather than a fake module, because the thing being verified is
argostranslate's behaviour, not our description of it.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from babelfishr import argos_home as A

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_the_real_argos_resolves_every_path_inside_the_injected_root(tmp_path):
    """Not a fake module: this imports the installed argostranslate 1.11."""
    pytest.importorskip("argostranslate.settings")

    home = tmp_path / "home"
    app_root = home / "Library" / "Application Support" / "BabelFishR"
    home.mkdir(parents=True)

    script = textwrap.dedent("""
        import json, os, pathlib
        from babelfishr.modes import bootstrap_environment
        applied = bootstrap_environment()
        import argostranslate.settings as settings
        print(json.dumps({
            "applied": applied,
            "data_dir": str(settings.data_dir),
            "config_dir": str(settings.config_dir),
            "cache_dir": str(settings.cache_dir),
            "downloads_dir": str(settings.downloads_dir),
            "package_data_dir": str(settings.package_data_dir),
            "local_package_index": str(settings.local_package_index),
            "xdg_left_set": [os.environ.get(name) for name in
                             ("XDG_DATA_HOME", "XDG_CONFIG_HOME",
                              "XDG_CACHE_HOME")],
        }))
    """)
    environment = dict(os.environ)
    environment.update({"HOME": str(home), "BABELFISHR_HOME": str(app_root),
                        "PYTHONPATH": str(ROOT)})
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        environment.pop(name, None)
    completed = subprocess.run([sys.executable, "-c", script], env=environment,
                               capture_output=True, text=True, timeout=180)
    assert completed.returncode == 0, completed.stderr
    resolved = json.loads(completed.stdout.strip().splitlines()[-1])

    for key in ("data_dir", "config_dir", "cache_dir", "downloads_dir",
                "package_data_dir", "local_package_index"):
        path = pathlib.Path(resolved[key])
        assert app_root in path.parents or path == app_root, (
            f"argos {key} resolved to {path}, outside {app_root}")

    # And the three folders alpha 2 created in the home directory are not
    # there at all - the point is that they are never made, not that they are
    # cleaned up afterwards.
    for legacy in (".local/share/argos-translate", ".config/argos-translate",
                   ".local/cache/argos-translate"):
        assert not (home / legacy).exists(), (
            f"{legacy} was still created in the home directory")

    # The environment is put back: leaving XDG_* set would relocate every
    # other XDG-aware library in the process and would be inherited by every
    # child process BabelFishR launches.
    assert resolved["xdg_left_set"] == [None, None, None]


def test_the_managed_layout_is_stated_once(tmp_path):
    expected = A.expected_argos_paths(tmp_path)
    for path in expected.values():
        assert tmp_path in path.parents
    assert expected["local_package_index"].name == "index.json"
    assert expected["downloads_dir"].parent == expected["cache_dir"]


def test_the_environment_is_restored_even_on_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", "/somewhere/else")
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    with pytest.raises(RuntimeError):
        with A.argos_environment_applied(tmp_path):
            assert os.environ["XDG_DATA_HOME"] != "/somewhere/else"
            raise RuntimeError("boom")
    assert os.environ["XDG_DATA_HOME"] == "/somewhere/else"
    assert "XDG_CACHE_HOME" not in os.environ


def test_nothing_here_touches_tls(tmp_path):
    source = (ROOT / "babelfishr" / "argos_home.py").read_text(encoding="utf-8")
    for forbidden in ("_create_unverified_context", "CERT_NONE",
                      "verify=False", "urlopen", "requests"):
        assert forbidden not in source


# ---- legacy directories ------------------------------------------------


def write_legacy_index(home: pathlib.Path) -> pathlib.Path:
    data = home / ".local" / "share" / "argos-translate"
    data.mkdir(parents=True, exist_ok=True)
    index = data / "index.json"
    index.write_text(json.dumps([{"code": "es", "from_code": "es",
                                  "to_code": "en"}]), encoding="utf-8")
    return index


def make_legacy_tree(home: pathlib.Path) -> None:
    write_legacy_index(home)
    (home / ".config" / "argos-translate").mkdir(parents=True, exist_ok=True)
    (home / ".local" / "cache" / "argos-translate" / "downloads").mkdir(
        parents=True, exist_ok=True)


def test_known_legacy_artifacts_are_cleaned(tmp_path):
    make_legacy_tree(tmp_path)
    report = A.clean_legacy_argos(tmp_path)
    assert report.complete, report.summary()
    for legacy in A.LEGACY_DIRECTORIES:
        assert not (tmp_path / legacy).exists(), f"{legacy} survived"
    assert not (tmp_path / ".local" / "share" / "argos-translate"
                / "index.json").exists()
    # The shared parents are other applications' too and are never touched.
    assert (tmp_path / ".local" / "share").exists()
    assert (tmp_path / ".config").exists()
    assert (tmp_path / ".local" / "cache").exists()


def test_a_dry_run_cleans_nothing(tmp_path):
    make_legacy_tree(tmp_path)
    report = A.clean_legacy_argos(tmp_path, dry_run=True)
    assert report.removed
    for legacy in A.LEGACY_DIRECTORIES:
        assert (tmp_path / legacy).exists()
    assert (tmp_path / ".local" / "share" / "argos-translate"
            / "index.json").exists()


def test_unknown_neighbouring_content_survives_and_is_reported(tmp_path):
    """The case that must never become a recursive delete.

    A second Argos installation - argospm, LibreTranslate, somebody's own
    script - keeps its packages in exactly these directories.
    """
    make_legacy_tree(tmp_path)
    data = tmp_path / ".local" / "share" / "argos-translate"
    stranger = data / "packages" / "translate-fr_en-1_9"
    stranger.mkdir(parents=True)
    (stranger / "model.bin").write_bytes(b"someone else's 200 MB model")
    (tmp_path / ".config" / "argos-translate" / "settings.json").write_text(
        '{"ARGOS_DEVICE_TYPE": "cuda"}', encoding="utf-8")

    report = A.clean_legacy_argos(tmp_path)

    assert (stranger / "model.bin").read_bytes() == b"someone else's 200 MB model"
    assert (tmp_path / ".config" / "argos-translate"
            / "settings.json").exists()
    assert not report.complete, "leftovers must not be called a clean sweep"
    kept = {str(path) for path, _ in report.kept}
    assert str(data) in kept
    assert str(tmp_path / ".config" / "argos-translate") in kept
    # Named by exact path, and the directory itself never described as removed.
    assert str(data) in report.summary()
    assert data not in report.removed
    # What genuinely was ours still went.
    assert not (data / "index.json").exists()
    assert not (tmp_path / ".local" / "cache" / "argos-translate").exists()


def test_a_file_that_is_not_our_index_is_left_alone(tmp_path):
    data = tmp_path / ".local" / "share" / "argos-translate"
    data.mkdir(parents=True)
    (data / "index.json").write_text('{"not": "a package list"}',
                                     encoding="utf-8")
    report = A.clean_legacy_argos(tmp_path)
    assert (data / "index.json").exists()
    assert any("not the file BabelFishR wrote" in why
               for _, why in report.kept)


def test_a_symlinked_legacy_directory_is_never_followed(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"keep")
    link = tmp_path / ".local" / "share" / "argos-translate"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside, target_is_directory=True)

    A.clean_legacy_argos(tmp_path)
    assert (outside / "keep.bin").read_bytes() == b"keep"
    assert link.is_symlink(), "the link itself is not ours to remove either"


def test_the_cleanup_can_never_recurse():
    """Read the syntax tree: there is no recursive delete, anywhere.

    A comment saying "never rmtree" is not a guarantee; the absence of the
    call is. Parsed rather than grepped so the prose explaining the rule is
    not mistaken for a breach of it.
    """
    import ast

    tree = ast.parse((ROOT / "babelfishr" / "argos_home.py").read_text(
        encoding="utf-8"))
    called = {node.func.attr if isinstance(node.func, ast.Attribute)
              else getattr(node.func, "id", "")
              for node in ast.walk(tree) if isinstance(node, ast.Call)}
    for recursive in ("rmtree", "removedirs", "system", "run"):
        assert recursive not in called, (
            f"{recursive} could delete content that is not BabelFishR's")
    assert "rmdir" in called, (
        "empty-directory removal is what makes this safe; it is missing")
