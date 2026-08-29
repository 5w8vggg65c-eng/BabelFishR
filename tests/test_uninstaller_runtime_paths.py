"""The shape of the *real* removal plan, proven without deleting anything.

tests/test_uninstaller.py is the destructive file: everything in it runs
against a temporary fake home, and a guard there fails if any test could reach
a real installation. That guard means it cannot also check the one thing that
must be true of the real plan - that it includes the machine's own
/Applications/BabelFishR.app.

So that check lives here, in a file that never deletes anything. Building a
plan only joins path strings; nothing below removes, moves or writes a file,
and a guard at the end asserts that this file contains no removal call at all.
"""

from __future__ import annotations

import ast
import pathlib

from babelfishr import uninstall as U

THIS_FILE = pathlib.Path(__file__).resolve()


def test_the_runtime_plan_includes_both_application_locations():
    plan = U.build_plan()
    assert plan.system_applications == pathlib.Path("/Applications")
    paths = plan.paths()
    assert pathlib.Path("/Applications") / U.APP_BUNDLE_NAME in paths
    assert plan.home / "Applications" / U.APP_BUNDLE_NAME in paths


def test_the_runtime_plan_covers_the_managed_root():
    plan = U.build_plan()
    support = plan.home / "Library" / "Application Support" / "BabelFishR"
    for relative in ("Recordings", "models", "language-packs", "argos",
                     "Logs", "settings.toml", "babelfishr.sqlite3"):
        assert support / relative in plan.paths()
    assert support in plan.paths()


def test_the_runtime_plan_stays_within_its_two_roots():
    plan = U.build_plan()
    assert plan.roots() == [plan.home, pathlib.Path("/Applications")]
    assert plan.contains_only_paths_within_its_roots()


def test_an_injected_applications_root_replaces_the_real_one(tmp_path):
    """The escape hatch is explicit, and still cannot reach /Applications."""
    plan = U.build_plan(tmp_path, system_applications=tmp_path / "Volumes")
    assert plan.system_applications == tmp_path / "Volumes"
    assert pathlib.Path("/Applications") / U.APP_BUNDLE_NAME not in plan.paths()
    assert tmp_path / "Volumes" / U.APP_BUNDLE_NAME in plan.paths()


def test_this_file_cannot_delete_anything():
    """A guard on the guards, matching the one in test_uninstaller.py."""
    tree = ast.parse(THIS_FILE.read_text(encoding="utf-8"))
    forbidden = {"uninstall", "rmtree", "unlink", "rmdir", "remove", "rename",
                 "write_text", "write_bytes", "mkdir", "_remove_one",
                 "elevate_removal", "reset_microphone_permission"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", ""))
        assert name not in forbidden, (
            f"{name}() would make this file capable of changing the disk")
