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
    # Before argostranslate is imported below: its data, config and cache
    # directories are resolved at import time, and importing it first would
    # create three folders in the operator's home that BabelFishR does not
    # manage and its uninstaller would not find.
    from babelfishr.modes import bootstrap_environment

    bootstrap_environment()
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


def _selftest_coreaudio() -> int:
    """Exercise the real CoreAudio ABI from inside the shipped bundle.

    This is the strongest honest check available without hardware: it proves
    the frameworks load and the property selectors and struct layout are
    correct on this machine, from the frozen app rather than from a source
    checkout. It proves nothing about audio flowing - a hosted runner has no
    audio hardware at all - and says so.
    """
    from babelfishr.audio import coreaudio

    report = coreaudio.probe()
    print(coreaudio.format_probe(report))
    if not report["ok"]:
        return 1
    if sys.platform == "darwin" and not report["frameworks_loaded"]:
        print("FAIL: running on macOS but CoreAudio did not load")
        return 1
    return 0


def _selftest_https() -> int:
    """Prove the frozen bundle can complete a real, verified HTTPS request.

    This is the check that mocked unit tests cannot stand in for. The failure
    it exists for - "unable to get local issuer certificate" - happens only in
    a bundle that has no trust store, so it can only be disproved by a bundle
    making a real connection.

    Exit codes are deliberately distinct:
      0  a verified HTTPS fetch succeeded, or the network is simply absent
      1  the certificate could not be verified - the defect is back
    A runner with no egress must not turn into a red build, but a runner that
    reaches the server and cannot verify it must.
    """
    import json
    import ssl
    import urllib.error
    import urllib.request

    from babelfishr.certificates import configure_certificates, describe
    from babelfishr.modes import bootstrap_environment

    bootstrap_environment()
    configure_certificates()
    print(describe())

    url = ("https://raw.githubusercontent.com/argosopentech/"
           "argospm-index/main/index.json")
    print(f"fetching {url}")
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            payload = response.read()
        packages = json.loads(payload)
        print(f"ok: verified HTTPS fetch, {len(payload)} bytes, "
              f"{len(packages)} packages in the index")
        return 0
    except ssl.SSLCertVerificationError as exc:
        print(f"FAIL: certificate verification failed: {exc}")
        print("The bundle has no usable CA trust store. This is the exact "
              "defect that made every Argos route fail on a real Mac.")
        return 1
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLCertVerificationError):
            print(f"FAIL: certificate verification failed: {reason}")
            return 1
        print(f"skip: no network access from this environment ({reason}). "
              f"Certificate verification was NOT exercised.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"skip: the fetch did not complete ({type(exc).__name__}: {exc}). "
              f"Certificate verification was NOT exercised.")
        return 0


def _selftest_argos_paths() -> int:
    """Prove every Argos directory resolves inside Application Support.

    argostranslate resolves its data, config and cache roots once, at import,
    from the XDG variables, and creates them immediately. ARGOS_PACKAGES_DIR
    moves only the installed packages - so up to alpha 2 the package index,
    the downloads cache and the configuration lived in the operator's home,
    outside anything BabelFishR managed or could remove.
    """
    from babelfishr.argos_home import resolved_argos_paths, verify_argos_paths
    from babelfishr.modes import AppPaths, bootstrap_environment

    bootstrap_environment()
    root = AppPaths.resolve().root
    print(f"managed root : {root}")
    try:
        resolved = resolved_argos_paths()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: argostranslate did not import: {exc}")
        return 1
    for name, path in sorted(resolved.items()):
        print(f"{name:<20}: {path}")
    stray = verify_argos_paths(root)
    if stray:
        print("FAIL: these Argos paths are outside the managed root:")
        for item in stray:
            print(f"  {item}")
        return 1
    home = pathlib.Path.home()
    for legacy in (".local/share/argos-translate", ".config/argos-translate",
                   ".local/cache/argos-translate"):
        if (home / legacy).exists():
            print(f"note: a legacy directory still exists: {home / legacy}")
    print("ok:   every Argos path resolves inside the managed root")
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

    if argv == ["--selftest-coreaudio"]:
        return _selftest_coreaudio()

    if argv == ["--selftest-https"]:
        return _selftest_https()

    if argv == ["--selftest-argos-paths"]:
        return _selftest_argos_paths()

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
