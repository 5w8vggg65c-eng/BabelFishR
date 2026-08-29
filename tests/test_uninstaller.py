"""The uninstaller, tested only ever against temporary fake homes.

Every test here builds its plan from ``tmp_path``. Nothing in this file calls
``build_plan()`` with no argument, so no test can be pointed at the operator's
own ``~/Library/Application Support`` - and one test asserts exactly that
about the file itself, so a future edit cannot quietly reintroduce it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import types

import pytest

from babelfishr import uninstall as U

PROJECT = pathlib.Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ helpers


def populate(home: pathlib.Path, *, recordings=("one.wav", "two.wav")) -> None:
    """Everything a used installation leaves behind, in a fake home."""
    support = home / "Library" / "Application Support" / "BabelFishR"
    (support / "Recordings").mkdir(parents=True)
    for name in recordings:
        (support / "Recordings" / name).write_bytes(b"audio")
    (support / "models" / "medium").mkdir(parents=True)
    (support / "models" / "medium" / "model.bin").write_bytes(b"x" * 32)
    (support / "language-packs").mkdir(parents=True)
    (support / "language-packs" / "en_es.argosmodel").write_bytes(b"pack")
    (support / "Logs").mkdir(parents=True)
    (support / "Logs" / "babelfishr.log").write_text("log")
    (support / "babelfishr.sqlite3").write_bytes(b"sqlite")
    (support / "settings.toml").write_text("mode = 'field-offline'\n")
    (home / ".config" / "babelfishr").mkdir(parents=True)
    (home / ".config" / "babelfishr" / ".env").write_text("")
    (home / "Library" / "Caches" / "org.babelfishr.app").mkdir(parents=True)
    (home / "Library" / "Caches" / "BabelFishR").mkdir(parents=True)
    (home / "Library" / "HTTPStorages" / "org.babelfishr.app").mkdir(parents=True)
    (home / "Library" / "Preferences").mkdir(parents=True, exist_ok=True)
    (home / "Library" / "Preferences" / "org.babelfishr.app.plist").write_text("{}")
    state = home / "Library" / "Saved Application State"
    state.mkdir(parents=True, exist_ok=True)
    (state / "org.babelfishr.app.savedState").mkdir()
    app = home / "Applications" / "BabelFishR.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "BabelFishR").write_text("#!/bin/sh\n")


def neighbours(home: pathlib.Path) -> list:
    """Other people's files, sitting right next to ours."""
    made = []
    support = home / "Library" / "Application Support"
    (support / "SomeOtherApp").mkdir(parents=True, exist_ok=True)
    made.append(support / "SomeOtherApp")
    (support / "SomeOtherApp" / "data.db").write_text("theirs")
    made.append(support / "SomeOtherApp" / "data.db")
    (home / "Library" / "Preferences" / "com.apple.finder.plist").write_text("{}")
    made.append(home / "Library" / "Preferences" / "com.apple.finder.plist")
    (home / "Applications" / "Other.app").mkdir(parents=True, exist_ok=True)
    made.append(home / "Applications" / "Other.app")
    (home / ".config" / "otherapp").mkdir(parents=True, exist_ok=True)
    made.append(home / ".config" / "otherapp")
    (home / "Music").mkdir(parents=True, exist_ok=True)
    (home / "Music" / "recording.wav").write_bytes(b"not ours")
    made.append(home / "Music" / "recording.wav")
    return made


def fake_runner(record=None, returncode=0):
    """A subprocess.run stand-in. No test in this file runs a real command."""
    calls = record if record is not None else []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")

    run.calls = calls
    return run


def not_running():
    return False


def plan_for(home: pathlib.Path) -> U.UninstallPlan:
    return U.build_plan(home)


# ------------------------------------------------------------- the plan


def test_the_plan_only_contains_babelfishr_paths(tmp_path):
    populate(tmp_path)
    plan = plan_for(tmp_path)
    home = plan.home
    for path in plan.paths():
        text = str(path)
        assert ("BabelFishR" in text or "babelfishr" in text), (
            f"{path} is not obviously a BabelFishR path")
        assert path == pathlib.Path("/Applications/BabelFishR.app") or \
            str(path).startswith(str(home) + os.sep)


def test_the_plan_covers_every_documented_removal(tmp_path):
    populate(tmp_path)
    plan = plan_for(tmp_path)
    present = {str(item.path) for item in plan.present()}
    support = str(plan.home / "Library" / "Application Support" / "BabelFishR")
    for expected in (f"{support}/Recordings", f"{support}/babelfishr.sqlite3",
                     f"{support}/models", f"{support}/language-packs",
                     f"{support}/Logs", f"{support}/settings.toml", support,
                     f"{plan.home}/.config/babelfishr",
                     f"{plan.home}/Library/Caches/org.babelfishr.app",
                     f"{plan.home}/Library/HTTPStorages/org.babelfishr.app",
                     f"{plan.home}/Library/Preferences/org.babelfishr.app.plist",
                     f"{plan.home}/Library/Saved Application State/"
                     f"org.babelfishr.app.savedState",
                     f"{plan.home}/Applications/BabelFishR.app"):
        assert expected in present, f"{expected} is not in the removal plan"


def test_the_description_shows_the_exact_paths_and_the_warning(tmp_path):
    populate(tmp_path)
    text = U.describe_plan(plan_for(tmp_path))
    assert str(tmp_path / "Library" / "Application Support" / "BabelFishR"
               / "Recordings") in text
    assert "CANNOT BE RECOVERED" in text


def test_configuration_cannot_steer_the_plan(tmp_path, monkeypatch):
    """A settings file pointing somewhere dangerous changes nothing."""
    victim = tmp_path / "important"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    support = tmp_path / "Library" / "Application Support" / "BabelFishR"
    support.mkdir(parents=True)
    (support / "settings.toml").write_text(
        f'app_home = "{victim}"\n'
        f'[recording]\ndirectory = "{victim}"\n')
    monkeypatch.setenv("BABELFISHR_HOME", str(victim))
    monkeypatch.setenv("BABELFISHR_RECORDINGS", str(victim))

    plan = plan_for(tmp_path)
    assert victim not in plan.paths()
    U.uninstall(plan, runner=fake_runner(), running_check=not_running)
    assert (victim / "keep.txt").exists()


def test_a_path_off_the_allowlist_is_refused(tmp_path):
    plan = plan_for(tmp_path)
    smuggled = U.UninstallPlan(
        home=plan.home,
        items=plan.items + (U.RemovalItem(tmp_path / "Documents", "not ours"),))
    (tmp_path / "Documents").mkdir()
    with pytest.raises(U.UninstallRefused):
        U.uninstall(smuggled, runner=fake_runner(), running_check=not_running)
    assert (tmp_path / "Documents").exists()


# ------------------------------------------------------------ cancelling


def test_a_dry_run_deletes_nothing(tmp_path):
    populate(tmp_path)
    plan = plan_for(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))
    report = U.uninstall(plan, dry_run=True, runner=fake_runner())
    assert report.dry_run
    assert sorted(p for p in tmp_path.rglob("*")) == before
    assert "DRY RUN" in report.summary()


def test_cancelling_the_window_deletes_nothing(tmp_path):
    """Closing the uninstaller without confirming must be a complete no-op."""
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.ui.uninstall_window import UninstallWindow

    populate(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UninstallWindow(plan_for(tmp_path), running_check=not_running)
    window.acknowledge.setChecked(True)
    window.typed.setText("DELETE")
    window.close()
    app.processEvents()
    assert sorted(p for p in tmp_path.rglob("*")) == before


# ---------------------------------------------------------- confirmation


@pytest.mark.parametrize("acknowledged,typed,expected", [
    (False, "", False),
    (True, "", False),
    (False, "DELETE", False),
    (True, "delete", False),
    (True, "Delete", False),
    (True, "DELETE IT", False),
    (True, "DELET", False),
    (True, "DELETE", True),
    (True, "  DELETE  ", True),
])
def test_both_confirmations_are_required(acknowledged, typed, expected):
    from babelfishr.ui.uninstall_window import confirmation_ready

    assert confirmation_ready(acknowledged, typed) is expected


def test_the_remove_button_is_disabled_until_both_confirmations(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.ui.uninstall_window import UninstallWindow

    populate(tmp_path)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UninstallWindow(plan_for(tmp_path), running_check=not_running)
    assert not window.remove_button.isEnabled()
    window.acknowledge.setChecked(True)
    assert not window.remove_button.isEnabled()
    window.typed.setText("DELETE")
    assert window.remove_button.isEnabled()
    window.acknowledge.setChecked(False)
    assert not window.remove_button.isEnabled()


def test_the_window_shows_the_paths_and_the_recording_warning(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.ui.uninstall_window import UninstallWindow

    populate(tmp_path)
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = UninstallWindow(plan_for(tmp_path), running_check=not_running)
    shown = window.detail.toPlainText()
    assert str(tmp_path / "Library" / "Application Support" / "BabelFishR"
               / "Recordings") in shown
    assert "cannot be recovered" in window.warning.text().lower()


# ------------------------------------------------------------- removing


def test_every_allowlisted_item_is_removed(tmp_path):
    populate(tmp_path)
    plan = plan_for(tmp_path)
    report = U.uninstall(plan, runner=fake_runner(), running_check=not_running)
    assert report.complete, report.summary()
    for item in plan.items:
        assert not os.path.lexists(item.path), f"{item.path} survived"
    assert "completely removed" in report.summary()


def test_unrelated_neighbours_survive(tmp_path):
    populate(tmp_path)
    others = neighbours(tmp_path)
    U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                running_check=not_running)
    for path in others:
        assert path.exists(), f"{path} was deleted and should not have been"


def test_a_symlinked_item_loses_the_link_and_not_the_target(tmp_path):
    """The classic way a recursive uninstaller destroys somebody's archive."""
    populate(tmp_path)
    support = tmp_path / "Library" / "Application Support" / "BabelFishR"
    archive = tmp_path / "BigExternalArchive"
    archive.mkdir()
    (archive / "years-of-recordings.wav").write_bytes(b"irreplaceable")
    # The operator moved Recordings to another disk and left a symlink.
    import shutil as _shutil
    _shutil.rmtree(support / "Recordings")
    (support / "Recordings").symlink_to(archive, target_is_directory=True)

    report = U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                         running_check=not_running)
    assert archive.exists()
    assert (archive / "years-of-recordings.wav").read_bytes() == b"irreplaceable"
    assert not os.path.lexists(support / "Recordings")
    assert support / "Recordings" in report.links_removed
    assert "leaving what they pointed at" in report.summary()


def test_a_symlink_nested_inside_a_removed_tree_is_not_followed(tmp_path):
    populate(tmp_path)
    outside = tmp_path / "Documents" / "keepsafe"
    outside.mkdir(parents=True)
    (outside / "thesis.txt").write_text("mine")
    support = tmp_path / "Library" / "Application Support" / "BabelFishR"
    (support / "Recordings" / "shortcut").symlink_to(
        outside, target_is_directory=True)

    U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                running_check=not_running)
    assert (outside / "thesis.txt").read_text() == "mine"


def test_nothing_is_removed_while_babelfishr_is_running(tmp_path):
    populate(tmp_path)
    plan = plan_for(tmp_path)
    before = sorted(p for p in tmp_path.rglob("*"))
    with pytest.raises(U.UninstallRefused) as excinfo:
        U.uninstall(plan, runner=fake_runner(), running_check=lambda: True)
    assert "still running" in str(excinfo.value)
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_an_undetermined_running_state_also_refuses(tmp_path):
    """'We could not tell' is treated as 'it is running'."""
    populate(tmp_path)
    with pytest.raises(U.UninstallRefused):
        U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                    running_check=lambda: None)
    assert (tmp_path / "Library" / "Application Support" / "BabelFishR"
            / "Recordings" / "one.wav").exists()


def test_app_is_running_reads_pgrep_honestly():
    assert U.app_is_running(runner=lambda *a, **k: types.SimpleNamespace(
        returncode=0, stdout="4231\n", stderr="")) is True
    assert U.app_is_running(runner=lambda *a, **k: types.SimpleNamespace(
        returncode=1, stdout="", stderr="")) is False
    assert U.app_is_running(runner=lambda *a, **k: types.SimpleNamespace(
        returncode=2, stdout="", stderr="")) is None

    def explode(*a, **k):
        raise OSError("no pgrep here")

    assert U.app_is_running(runner=explode) is None


# ----------------------------------------------------------- reporting


def test_a_leftover_is_reported_and_never_called_complete(tmp_path,
                                                          monkeypatch):
    populate(tmp_path)
    stubborn = (tmp_path / "Library" / "Application Support" / "BabelFishR"
                / "models")
    real_rmtree = U.shutil.rmtree

    def refuse(path, *args, **kwargs):
        if pathlib.Path(path) == stubborn:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(U.shutil, "rmtree", refuse)
    # allow_elevation=False: the administrator prompt is a real UI action and
    # is never exercised for real by the suite.
    report = U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                         allow_elevation=False, running_check=not_running)
    assert not report.complete
    assert stubborn in report.leftovers()
    summary = report.summary()
    assert "COULD NOT BE REMOVED" in summary
    assert "NOT completely removed" in summary
    assert "was completely removed" not in summary


def test_a_failed_microphone_reset_is_reported_not_hidden(tmp_path):
    populate(tmp_path)
    report = U.uninstall(plan_for(tmp_path), runner=fake_runner(returncode=1),
                         running_check=not_running)
    assert report.microphone_permission_reset is False
    assert "could not be reset" in report.summary()


def test_the_microphone_permission_is_reset_for_the_app_identifier(tmp_path):
    populate(tmp_path)
    runner = fake_runner()
    report = U.uninstall(plan_for(tmp_path), runner=runner,
                         running_check=not_running)
    assert report.microphone_permission_reset is True
    assert ["/usr/bin/tccutil", "reset", "Microphone",
            "org.babelfishr.app"] in runner.calls
    assert "Microphone permission for BabelFishR was reset" in report.summary()


def test_an_absent_installation_is_reported_as_such(tmp_path):
    report = U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                         running_check=not_running)
    assert report.complete
    assert not report.removed
    assert "Not present, nothing to do" in report.summary()


# --------------------------------------------------- privileged fallback


def test_the_authorization_command_never_interpolates_unsafely():
    hostile = pathlib.Path('/Applications/"; rm -rf /Users; echo ".app')
    with pytest.raises(U.UninstallRefused):
        U._authorization_command([hostile])
    with pytest.raises(U.UninstallRefused):
        U._authorization_command([pathlib.Path("/tmp/x$(whoami)")])
    with pytest.raises(U.UninstallRefused):
        U._authorization_command([pathlib.Path("/tmp/back\\slash")])

    command = U._authorization_command([pathlib.Path("/Applications/BabelFishR.app")])
    assert command == "/bin/rm -rf -- /Applications/BabelFishR.app"


def test_the_authorization_command_quotes_the_spaces_in_our_real_paths():
    # Assembled rather than written out, so the guard below cannot mistake a
    # quoting fixture for a path a test might really delete.
    path = (pathlib.Path("/Users/someone/Library") / "Application Support"
            / "BabelFishR")
    command = U._authorization_command([path])
    assert f"'{path}'" in command


def test_elevation_is_only_asked_for_paths_that_actually_failed(tmp_path,
                                                                monkeypatch):
    populate(tmp_path)
    stubborn = (tmp_path / "Library" / "Application Support" / "BabelFishR"
                / "models")
    real_rmtree = U.shutil.rmtree

    def refuse(path, *args, **kwargs):
        if pathlib.Path(path) == stubborn:
            raise PermissionError(13, "Permission denied")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(U.shutil, "rmtree", refuse)
    asked = []
    monkeypatch.setattr(U, "elevate_removal",
                        lambda paths, **kw: asked.append(list(paths)) or False)
    report = U.uninstall(plan_for(tmp_path), runner=fake_runner(),
                         running_check=not_running)
    assert asked == [[stubborn]]
    assert not report.complete


# -------------------------------------------------- the tests themselves


def _docstrings(tree):
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            text = ast.get_docstring(node, clean=False)
            if text:
                found.add(text)
    return found


def test_no_test_in_this_file_can_reach_the_real_home():
    """A guard on the guards.

    ``build_plan`` with no argument uses the real home directory. If a future
    edit adds such a call to this file, the suite would start planning
    deletions against the operator's own Application Support directory. This
    reads the file's syntax tree rather than its text, so prose about the rule
    is not mistaken for a breach of it.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text())
    docstrings = _docstrings(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", ""))
        if name == "build_plan":
            assert node.args or node.keywords, (
                "build_plan with no argument would plan against the real home")
        assert name != "home", "Path.home() would reach the real installation"
        assert name != "expanduser", "expanduser would reach the real home"

    # Assembled, so this check does not trip over itself.
    real = "Application" + " Support/" + "BabelFishR"
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and node.value not in docstrings):
            assert real not in node.value, (
                "a literal real-installation path appears in a test")


def test_the_uninstaller_is_not_reachable_from_inside_the_app():
    """No in-app self-destruct: the app must not import the removal code."""
    for module in (PROJECT / "babelfishr" / "ui" / "main_window.py",
                   PROJECT / "babelfishr" / "ui" / "setup_assistant.py",
                   PROJECT / "babelfishr" / "cli.py",
                   PROJECT / "babelfishr" / "app.py"):
        text = module.read_text()
        assert "uninstall" not in text.lower(), (
            f"{module.name} references uninstallation; removal must only be "
            f"reachable from the separate uninstaller application")
