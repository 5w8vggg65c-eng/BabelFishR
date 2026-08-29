"""PySide6 desktop front-end."""

from __future__ import annotations

import sys
from typing import Optional


def run(config_path: Optional[str] = None, argv: Optional[list] = None) -> int:
    """Launch the desktop application."""
    try:
        from PySide6 import QtWidgets
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "PySide6 is not installed. Install the GUI extra:\n"
            "    pip install 'babelfishr[gui]'"
        ) from exc

    from ..app import BabelFishRApp
    from ..config import Config
    from .main_window import MainWindow

    qt_app = QtWidgets.QApplication(argv if argv is not None else sys.argv)
    qt_app.setApplicationName("BabelFishR")
    qt_app.setOrganizationName("BabelFishR")

    app = BabelFishRApp(config=Config.load(config_path))
    window = MainWindow(app)
    window.show()
    return qt_app.exec()


__all__ = ["run"]
