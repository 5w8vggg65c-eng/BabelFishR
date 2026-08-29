"""Entry point for the packaged macOS application.

A frozen app has no terminal, so anything that would have gone to stderr goes
to a log file under Application Support instead - otherwise a first-run failure
is invisible to the operator.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys


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
