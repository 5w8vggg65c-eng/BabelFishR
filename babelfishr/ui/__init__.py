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

    # First launch: no completed setup, so guide the operator rather than
    # leaving them at an app that cannot transcribe and will not say why.
    if config.needs_first_run_setup:
        from .setup_assistant import SetupAssistant

        QtCore.QTimer.singleShot(
            200, lambda: SetupAssistant(app, window).exec())
    return qt_app.exec()


__all__ = ["run"]
