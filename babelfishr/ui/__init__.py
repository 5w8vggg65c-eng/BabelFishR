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
    qt_app.setApplicationName("BabelFishR")
    qt_app.setOrganizationName("BabelFishR")
    apply_theme(qt_app)

    app = BabelFishRApp(config=config)
    window = MainWindow(app)
    window.show()

    # Guide the operator rather than leaving them at an app that cannot
    # transcribe and will not say why. Not only on a genuine first launch: a
    # model that was deleted, moved or left half-downloaded is the same
    # situation from the operator's side, and the flag alone would miss it.
    if config.needs_first_run_setup or not _has_usable_assets(config):
        from .setup_assistant import SetupAssistant

        def _first_run_setup() -> None:
            SetupAssistant(app, window).exec()
            # The same refresh the manually opened assistant gets. Without it
            # the operator finished preparation and the window still showed
            # the pre-setup mode, engines and readiness until they restarted.
            window.refresh_after_setup()

        QtCore.QTimer.singleShot(200, _first_run_setup)
    return qt_app.exec()


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
