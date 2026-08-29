"""Semantic, appearance-adaptive styling.

The previous UI hard-coded a GitHub-dark palette, which looked wrong in macOS
light appearance and ignored the operator's system setting entirely.  This
module derives every colour from the Qt palette the platform hands us, so the
window follows macOS light/dark automatically and inherits the system font.

Design intent: native-feeling and restrained rather than branded. Neutral
surfaces, one accent colour, generous spacing, rounded cards, and status
carried by an icon and a word as well as a colour - never by colour alone,
which matters both for accessibility and for daylight legibility in the field.
"""

from __future__ import annotations

from typing import Dict

from PySide6 import QtGui, QtWidgets

#: One accent, used for interactive emphasis only.
ACCENT_LIGHT = "#0a6cff"
ACCENT_DARK = "#4d9dff"

#: Status colours, chosen to stay distinguishable for common colour-vision
#: deficiencies. Every use is paired with a text label or symbol.
STATUS_LIGHT = {
    "idle": "#6b7280", "listening": "#1a7f37", "receiving": "#0a6cff",
    "working": "#9a6700", "error": "#cf222e", "ok": "#1a7f37",
}
STATUS_DARK = {
    "idle": "#9aa0a6", "listening": "#3fb950", "receiving": "#4d9dff",
    "working": "#d29922", "error": "#ff7b72", "ok": "#3fb950",
}


def is_dark(widget: QtWidgets.QWidget = None) -> bool:
    """True when the current appearance is dark, per the system palette."""
    palette = widget.palette() if widget is not None else \
        QtWidgets.QApplication.palette()
    window = palette.color(QtGui.QPalette.Window)
    # Perceived luminance; the system palette is the source of truth.
    luminance = (0.299 * window.red() + 0.587 * window.green()
                 + 0.114 * window.blue()) / 255.0
    return luminance < 0.5


def accent(widget: QtWidgets.QWidget = None) -> str:
    return ACCENT_DARK if is_dark(widget) else ACCENT_LIGHT


def status_color(name: str, widget: QtWidgets.QWidget = None) -> str:
    table = STATUS_DARK if is_dark(widget) else STATUS_LIGHT
    return table.get(name, table["idle"])


def tokens(widget: QtWidgets.QWidget = None) -> Dict[str, str]:
    """Colour tokens derived from the live system palette."""
    palette = widget.palette() if widget is not None else \
        QtWidgets.QApplication.palette()
    dark = is_dark(widget)
    window = palette.color(QtGui.QPalette.Window)
    text = palette.color(QtGui.QPalette.WindowText)

    def shift(color: QtGui.QColor, amount: int) -> str:
        factor = 1 + (amount / 100.0) * (1 if dark else -1)
        return QtGui.QColor(
            max(0, min(255, int(color.red() * factor))),
            max(0, min(255, int(color.green() * factor))),
            max(0, min(255, int(color.blue() * factor)))).name()

    return {
        "window": window.name(),
        "surface": shift(window, 8),
        "surface_raised": shift(window, 16),
        "border": shift(window, 30),
        "text": text.name(),
        "text_muted": palette.color(QtGui.QPalette.Disabled,
                                    QtGui.QPalette.WindowText).name(),
        "accent": accent(widget),
        "error": status_color("error", widget),
        "warning": status_color("working", widget),
        "ok": status_color("ok", widget),
        "dark": "1" if dark else "0",
    }


def stylesheet(widget: QtWidgets.QWidget = None) -> str:
    """Application stylesheet built from the current appearance.

    Regenerated when the system appearance changes, so the window follows the
    operator's light/dark setting instead of imposing one.
    """
    t = tokens(widget)
    return f"""
QWidget {{ color: {t['text']}; }}

/* --- cards: one rounded surface per received transmission ------------- */
#bubble {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
#bubble[state="failed"] {{ border: 1px solid {t['error']}; }}
#bubble[review="true"] {{ border: 1px solid {t['warning']}; }}
#bubble[skipped="true"] {{ background: {t['window']}; }}

#bubbleHeader {{ color: {t['text_muted']}; font-size: 11px; }}
#originalText {{ color: {t['text']}; font-size: 13px; }}
#translatedText {{ color: {t['accent']}; font-size: 13px; }}
#errorText {{ color: {t['error']}; font-size: 12px; }}
#noteText {{ color: {t['warning']}; font-size: 12px; }}
#provisional {{ color: {t['text_muted']}; font-style: italic; }}
#statusText {{ color: {t['text_muted']}; font-size: 11px; }}
#emptyState {{ color: {t['text_muted']}; padding: 48px 24px; font-size: 13px; }}

/* --- metadata chips ---------------------------------------------------- */
#chip {{
    background: {t['surface_raised']};
    border: 1px solid {t['border']};
    border-radius: 9px;
    padding: 2px 8px;
    font-size: 11px;
    color: {t['text_muted']};
}}
#chip[tone="accent"] {{ color: {t['accent']}; border-color: {t['accent']}; }}
#chip[tone="warning"] {{ color: {t['warning']}; border-color: {t['warning']}; }}
#chip[tone="error"] {{ color: {t['error']}; border-color: {t['error']}; }}

/* --- toolbar ----------------------------------------------------------- */
#modeBadge {{
    border-radius: 10px; padding: 4px 12px; font-size: 11px; font-weight: 600;
    border: 1px solid {t['border']}; background: {t['surface_raised']};
}}
#stateBadge {{ font-size: 13px; font-weight: 600; padding: 4px 10px; }}
#primaryButton {{
    background: {t['accent']}; color: white; border: none; border-radius: 8px;
    padding: 8px 20px; font-size: 13px; font-weight: 600;
}}
#primaryButton:hover {{ background: {t['accent']}; }}
#primaryButton:disabled {{ background: {t['border']}; color: {t['text_muted']}; }}

/* --- banners ----------------------------------------------------------- */
#warningBanner, #privacyBanner, #readyBanner {{
    border-radius: 8px; padding: 9px 12px; font-size: 12px;
    border: 1px solid {t['border']};
}}
#warningBanner {{ color: {t['warning']}; border-color: {t['warning']}; }}
#privacyBanner {{ color: {t['accent']}; border-color: {t['accent']}; }}
#readyBanner {{ color: {t['ok']}; border-color: {t['ok']}; }}

/* --- setup panel ------------------------------------------------------- */
#setupPanel {{
    background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: 12px;
}}
#sectionLabel {{ color: {t['text_muted']}; font-size: 11px; font-weight: 600; }}
QGroupBox {{ border: 1px solid {t['border']}; border-radius: 10px;
             margin-top: 10px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px;
                    color: {t['text_muted']}; }}
"""


def apply(app: QtWidgets.QApplication) -> None:
    """Use the platform font and let Qt follow the system appearance."""
    app.setStyle("Fusion")
    font = app.font()
    # The system UI font, whatever the platform calls it.
    for family in ("SF Pro Text", ".AppleSystemUIFont", "Helvetica Neue",
                   "Segoe UI", "Inter"):
        if family in QtGui.QFontDatabase.families():
            font.setFamily(family)
            break
    if font.pointSize() < 12:
        font.setPointSize(13)
    app.setFont(font)
