"""Entry point for the packaged macOS application.

A frozen app has no terminal, so anything that would have gone to stderr goes
to a log file under Application Support instead - otherwise a first-run failure
is invisible to the operator.

The ``--selftest-*`` flags exist so the release pipeline can prove things about
the *frozen* bundle that cannot be proven about the source tree: that it
imports its own code, that nothing it loads comes from the build machine, and
that the main window really constructs.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import pathlib
import sys

# Modules the shipped app genuinely needs. If any of these resolves to a file
# outside the bundle, the app is not standalone and must not be released.
REQUIRED_MODULES = (
    "babelfishr",
    "babelfishr.cli",
    "babelfishr.config",
    "babelfishr.modes",
    "babelfishr.audio.devices",
    "babelfishr.ui.main_window",
    "numpy",
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "sounddevice",
    "faster_whisper",
    "ctranslate2",
    "argostranslate",
    "argostranslate.translate",
)

# Wanted, but the app degrades honestly without them (timeline.py falls back to
# the system player when QtMultimedia is absent).
OPTIONAL_MODULES = ("PySide6.QtMultimedia",)


def _setup_logging():
    from babelfishr.modes import AppPaths

    paths = AppPaths.resolve().ensure()
    handler = logging.handlers.RotatingFileHandler(
        paths.logs / "babelfishr.log", maxBytes=2_000_000, backupCount=3,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return paths


def _bundle_roots():
    """Directories a standalone bundle is allowed to load code from."""
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(pathlib.Path(meipass).resolve())
    exe = pathlib.Path(sys.executable).resolve()
    roots.append(exe.parent)
    # dist/BabelFishR.app/Contents/MacOS/BabelFishR -> .../Contents
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        roots.append(exe.parent.parent)
    # De-duplicate while keeping the most specific first.
    seen, unique = set(), []
    for root in roots:
        if root not in seen:
            seen.add(root)
            unique.append(root)
    return unique


def _inside(path, roots) -> bool:
    try:
        resolved = pathlib.Path(path).resolve()
    except Exception:  # noqa: BLE001
        return False
    return any(resolved == root or root in resolved.parents for root in roots)


def _module_origin(module) -> str:
    """Where a module's code came from, or '' when it lives in the PYZ archive.

    Pure-Python modules frozen into the PyInstaller archive legitimately have
    no ``__file__``; that is *inside* the bundle by construction.
    """
    origin = getattr(module, "__file__", None)
    if origin:
        return str(origin)
    paths = list(getattr(module, "__path__", []) or [])
    return str(paths[0]) if paths else ""


def _selftest_independence(verbose: bool = True) -> int:
    """Prove the frozen app loads nothing from the machine that built it."""
    roots = _bundle_roots()
    failures = []
    lines = [
        f"executable   : {sys.executable}",
        f"frozen       : {getattr(sys, 'frozen', False)}",
        f"_MEIPASS     : {getattr(sys, '_MEIPASS', '(none)')}",
        f"sys.prefix   : {sys.prefix}",
        f"cwd          : {os.getcwd()}",
        f"bundle roots : {', '.join(str(r) for r in roots)}",
    ]
    for name in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "CONDA_PREFIX"):
        lines.append(f"env {name:<12}: {os.environ.get(name, '(unset)')}")

    if not getattr(sys, "frozen", False):
        failures.append("not running from a frozen bundle")

    for name in REQUIRED_MODULES + OPTIONAL_MODULES:
        optional = name in OPTIONAL_MODULES
        try:
            module = __import__(name, fromlist=["__file__"])
        except Exception as exc:  # noqa: BLE001
            lines.append(f"IMPORT-FAIL  {name}: {exc}")
            if not optional:
                failures.append(f"required module {name} did not import: {exc}")
            continue
        origin = _module_origin(module)
        where = origin or "(frozen archive)"
        if origin and not _inside(origin, roots):
            lines.append(f"OUTSIDE      {name}: {where}")
            failures.append(f"{name} loaded from outside the bundle: {where}")
        else:
            lines.append(f"ok           {name}: {where}")

    # Qt has to find its own plugins inside the bundle or the app shows no
    # window at all on a machine with no Qt installed.
    try:
        from PySide6 import QtCore

        plugins = pathlib.Path(
            QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.PluginsPath))
        lines.append(f"qt plugins   : {plugins}")
        if not _inside(plugins, roots):
            failures.append(f"Qt plugin path is outside the bundle: {plugins}")
        cocoa = plugins / "platforms" / "libqcocoa.dylib"
        if sys.platform == "darwin":
            if cocoa.exists():
                lines.append(f"ok           qt platform plugin: {cocoa}")
            else:
                failures.append(f"missing Qt platform plugin: {cocoa}")
        multimedia = plugins / "multimedia"
        if multimedia.is_dir():
            found = sorted(p.name for p in multimedia.iterdir())
            lines.append(f"ok           qt multimedia plugins: {', '.join(found)}")
        else:
            lines.append("note         no Qt multimedia plugins bundled; "
                         "recording playback falls back to the system player")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"could not inspect Qt library paths: {exc}")

    if verbose:
        for line in lines:
            print(line)
    if failures:
        print("")
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"{len(failures)} independence failure(s)")
        return 1
    print("")
    print("Independence check passed: every required module loads from inside "
          "the bundle.")
    return 0


def _selftest_gui() -> int:
    """Construct and show the real main window, headless."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6 import QtWidgets

    from babelfishr.app import BabelFishRApp
    from babelfishr.config import Config
    from babelfishr.modes import bootstrap_environment
    from babelfishr.ui.main_window import MainWindow

    config = Config.load(None)
    bootstrap_environment(config)
    qt_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    app = BabelFishRApp(config=config)
    window = MainWindow(app)
    window.show()
    qt_app.processEvents()
    visible = window.isVisible()
    title = window.windowTitle()
    window.close()
    qt_app.processEvents()
    if not visible:
        print("FAIL: the main window did not become visible")
        return 1
    print(f"Main window opened offscreen: {title!r}")
    return 0


def main(argv=None) -> int:
    """Launch the GUI, or hand off to the CLI when arguments are supplied.

    The bundled binary is the same entry point either way, so
    ``BabelFishR.app/Contents/MacOS/BabelFishR devices`` works and the
    documentation can honestly say so.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    paths = _setup_logging()
    logging.info("BabelFishR starting; assets in %s", paths.root)

    if argv == ["--selftest-import"]:
        # Used by the build script to prove the frozen bundle can import its
        # own code before anything is signed or shipped.
        import babelfishr
        from babelfishr import cli  # noqa: F401

        print(f"babelfishr {babelfishr.__version__} imports cleanly")
        return 0

    if argv == ["--selftest-independence"]:
        return _selftest_independence()

    if argv == ["--selftest-gui"]:
        return _selftest_gui()

    if argv:
        from babelfishr.cli import main as cli_main

        return cli_main(argv)

    try:
        from babelfishr.ui import run

        return run()
    except Exception:  # noqa: BLE001 - a frozen app must not die silently
        logging.exception("BabelFishR failed to start")
        try:
            from PySide6 import QtWidgets

            app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
            QtWidgets.QMessageBox.critical(
                None, "BabelFishR could not start",
                f"Something went wrong during startup.\n\n"
                f"The log is at:\n{paths.logs / 'babelfishr.log'}")
        except Exception:  # noqa: BLE001
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
