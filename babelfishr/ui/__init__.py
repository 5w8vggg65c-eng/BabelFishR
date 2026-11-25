"""PySide6 desktop front-end."""

from __future__ import annotations

import sys
from typing import Optional


def run(config_path: Optional[str] = None, argv: Optional[list] = None) -> int:
    """Launch the desktop application."""
    try:
        from PySide6 import QtCore, QtWidgets
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "PySide6 is not installed. Install the GUI extra:\n"
            "    pip install 'babelfishr[gui]'"
        ) from exc

    from ..config import Config
    from ..modes import bootstrap_environment

    # Before any provider import - Argos resolves its package directory once.
    config = Config.load(config_path)
    bootstrap_environment(config)

    from ..app import BabelFishRApp
    from .main_window import MainWindow
    from .theme import apply as apply_theme

    qt_app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    from ..extensions import product

    qt_app.setApplicationName(product().name)
    qt_app.setOrganizationName(product().name)
    apply_theme(qt_app)

    launch(qt_app, BabelFishRApp(config=config))
    return qt_app.exec()


def launch(qt_app, app):
    """Show the first window(s) and return the console.

    A fresh profile (no completed setup) sees Set up BabelFishR first, as
    the only window: the console is not even built until the operator has
    made their choices, so it cannot flash up behind the assistant. Nothing
    is downloaded and nothing is monitored unless the operator asks. The
    mode stays Online / Setup unless they choose another (Record only).
    A returning profile keeps its saved mode and goes straight to the
    console; if its speech model or language packs have gone missing, the
    assistant follows over the console as before.
    """
    from PySide6 import QtCore

    from .main_window import MainWindow
    from .setup_assistant import SetupAssistant

    config = app.config
    first_run = config.needs_first_run_setup
    if first_run:
        quits = qt_app.quitOnLastWindowClosed()
        qt_app.setQuitOnLastWindowClosed(False)   # closing it must not end the app
        try:
            SetupAssistant(app, None, first_run=True).exec()
        finally:
            qt_app.setQuitOnLastWindowClosed(quits)
    window = MainWindow(app)
    window.show()

    # Guide the operator rather than leaving them at an app that cannot
    # transcribe and will not say why: a model that was deleted, moved or
    # left half-downloaded is the same situation from their side.
    if not first_run and not _has_usable_assets(config):
        def _repair_setup() -> None:
            SetupAssistant(app, window).exec()
            # The same refresh the manually opened assistant gets. Without it
            # the operator finished preparation and the window still showed
            # the pre-setup mode, engines and readiness until they restarted.
            window.refresh_after_setup()

        QtCore.QTimer.singleShot(200, _repair_setup)
    return window


def _has_usable_assets(config) -> bool:
    """Is there a complete speech model AND at least one translation route?"""
    try:
        from ..preparation import installed_routes
        from ..providers.whisper_local import (ModelState,
                                               inspect_model_directory,
                                               model_directory_for)

        directory = model_directory_for(config.paths().models, config.asr.model)
        state, _ = inspect_model_directory(directory)
        if state is not ModelState.COMPLETE:
            return False
        return bool(installed_routes(config))
    except Exception:  # noqa: BLE001 - never block startup on this check
        return False


__all__ = ["run"]
