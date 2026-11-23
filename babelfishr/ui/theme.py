"""Semantic, appearance-adaptive styling: the field console.

The window follows the operator's system appearance, as before. What changes
with this pass is the look in each:

* **Dark appearance - the field console.** A fixed, high-contrast palette
  whose primary reference is OpenDMR: its cool navy blues and turquoise, and
  its status bands. The UV-5RM firmware contributes compact, terse structure
  only, not its colours. Navy ground, slightly lighter blue panels, off-white
  text, turquoise-cyan for the primary control and the selection, amber for
  anything that needs review. Red appears only as the semantic error colour.
  It is applied as the window's own palette, so
  native-drawn widgets (fields, menus, tabs, scroll bars) match rather than
  showing the operating system's grey.
* **Light appearance - daylight.** Colours still derive from the system
  palette, for legibility outdoors and for the platform's own accessibility
  settings.

Both share the same structure: a compact header band and footer band, square
rectangular controls with a minimal corner radius, strong readable text for
the original transcript and its translation, and status carried by a symbol
and a word as well as a colour - never by colour alone.

No web fonts, no animation. The system UI font is used.
"""

from __future__ import annotations

from typing import Dict

from PySide6 import QtGui, QtWidgets

#: One accent, used for the primary control and the selection.
ACCENT_LIGHT = "#0a6cff"
ACCENT_DARK = "#00B4D8"

#: The field console palette (dark appearance).
CONSOLE = {
    "window": "#07131F",
    "surface": "#0E2A44",
    "surface_raised": "#123756",
    "border": "#1C3448",
    "text": "#F4FAFF",
    "text_muted": "#9DB4C8",
    "accent": ACCENT_DARK,
    "on_accent": "#07131F",
}

#: Status colours, chosen to stay distinguishable for common colour-vision
#: deficiencies. Every use is paired with a text label or symbol.
STATUS_LIGHT = {
    "idle": "#6b7280", "listening": "#1a7f37", "receiving": "#0a6cff",
    "working": "#9a6700", "error": "#cf222e", "ok": "#1a7f37",
}
STATUS_DARK = {
    "idle": "#9DB4C8", "listening": "#3FD68B", "receiving": "#00B4D8",
    "working": "#FFC145", "error": "#FF6B6B", "ok": "#3FD68B",
}

#: Corner radius for every rectangle: square, deliberate, not rounded cards.
RADIUS = 3


def is_dark(widget: QtWidgets.QWidget = None) -> bool:
    """True when the operator's *system* appearance is dark.

    Read from the application palette - the system's - not from a widget's,
    because the window sets its own palette for the field console and must
    still notice when the system switches back to light.
    """
    palette = QtWidgets.QApplication.palette()
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
    """Colour tokens: the field console in dark appearance, the system
    palette in light."""
    if is_dark(widget):
        return dict(CONSOLE, error=STATUS_DARK["error"],
                    warning=STATUS_DARK["working"], ok=STATUS_DARK["ok"],
                    dark="1")

    palette = QtWidgets.QApplication.palette()
    window = palette.color(QtGui.QPalette.Window)
    text = palette.color(QtGui.QPalette.WindowText)

    def shift(color: QtGui.QColor, amount: int) -> str:
        factor = 1 - amount / 100.0
        return QtGui.QColor(
            max(0, min(255, int(color.red() * factor))),
            max(0, min(255, int(color.green() * factor))),
            max(0, min(255, int(color.blue() * factor)))).name()

    return {
        "window": window.name(),
        "surface": shift(window, 4),
        "surface_raised": shift(window, 8),
        "border": shift(window, 22),
        "text": text.name(),
        # A fixed secondary grey rather than the system's disabled colour,
        # which is too faint for labels an operator has to read outdoors.
        "text_muted": "#4b5563",
        "accent": ACCENT_LIGHT,
        "on_accent": "#ffffff",
        "error": STATUS_LIGHT["error"],
        "warning": STATUS_LIGHT["working"],
        "ok": STATUS_LIGHT["ok"],
        "dark": "0",
    }


def palette(widget: QtWidgets.QWidget = None) -> QtGui.QPalette:
    """The palette the window should use: the console's in dark appearance,
    the system's (unchanged) in light."""
    if not is_dark(widget):
        return QtGui.QPalette()          # nothing set: inherit the system's
    t = tokens(widget)
    p = QtGui.QPalette(QtWidgets.QApplication.palette())
    c = QtGui.QColor
    for group in (QtGui.QPalette.Active, QtGui.QPalette.Inactive):
        p.setColor(group, QtGui.QPalette.Window, c(t["window"]))
        p.setColor(group, QtGui.QPalette.WindowText, c(t["text"]))
        p.setColor(group, QtGui.QPalette.Base, c(t["surface"]))
        p.setColor(group, QtGui.QPalette.AlternateBase, c(t["surface_raised"]))
        p.setColor(group, QtGui.QPalette.Text, c(t["text"]))
        p.setColor(group, QtGui.QPalette.Button, c(t["surface"]))
        p.setColor(group, QtGui.QPalette.ButtonText, c(t["text"]))
        p.setColor(group, QtGui.QPalette.Highlight, c(t["accent"]))
        p.setColor(group, QtGui.QPalette.HighlightedText, c(t["on_accent"]))
        p.setColor(group, QtGui.QPalette.ToolTipBase, c(t["surface_raised"]))
        p.setColor(group, QtGui.QPalette.ToolTipText, c(t["text"]))
        p.setColor(group, QtGui.QPalette.PlaceholderText, c(t["text_muted"]))
        p.setColor(group, QtGui.QPalette.Link, c(t["accent"]))
    for role in (QtGui.QPalette.WindowText, QtGui.QPalette.Text,
                 QtGui.QPalette.ButtonText):
        p.setColor(QtGui.QPalette.Disabled, role, c(t["text_muted"]))
    p.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Base, c(t["window"]))
    p.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Button, c(t["window"]))
    return p


def stylesheet(widget: QtWidgets.QWidget = None) -> str:
    """Application stylesheet built from the current appearance.

    Regenerated when the system appearance changes, so the window follows the
    operator's light/dark setting instead of imposing one.
    """
    t = tokens(widget)
    r = RADIUS
    return f"""
QWidget {{ color: {t['text']}; }}
QMainWindow, #centralRoot {{ background: {t['window']}; }}

/* --- header band: start, state, mode, readiness, level ----------------- */
#headerBand {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: {r}px;
}}
#stateBadge {{ font-size: 15px; font-weight: 700; padding: 4px 10px; }}
#primaryButton {{
    background: {t['accent']}; color: {t['on_accent']}; border: none;
    border-radius: {r}px; padding: 9px 22px; font-size: 14px; font-weight: 700;
}}
#primaryButton:hover {{ background: {t['accent']}; }}
#primaryButton:focus {{ border: 2px solid {t['text']}; }}
#primaryButton:disabled {{ background: {t['border']}; color: {t['text_muted']}; }}
#modeButton {{
    border: 1px solid {t['border']}; border-radius: {r}px;
    background: {t['window']}; padding: 6px 10px;
    font-size: 12px; font-weight: 700;
}}
#modeButton[mode="field-offline"] {{ border: 1px solid {t['ok']}; color: {t['ok']}; }}
#modeButton[mode="record-only"] {{ border: 1px solid {t['warning']}; color: {t['warning']}; }}
#modeButton[mode="online-setup"] {{ border: 1px solid {t['accent']}; color: {t['accent']}; }}
#modeButton:focus {{ border-width: 2px; }}
/* Disabled (e.g. while monitoring runs): no mode colour, so it can't look live. */
#modeButton:disabled, #playButton:disabled {{
    border: 1px dashed {t['border']}; color: {t['text_muted']};
    background: {t['window']};
}}

/* --- footer band: the status bar --------------------------------------- */
QStatusBar {{
    background: {t['surface']}; border-top: 1px solid {t['border']};
    color: {t['text_muted']}; font-size: 11px;
}}

/* --- message cards: one per received transmission ---------------------- */
#bubble {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-left: 3px solid {t['border']};
    border-radius: {r}px;
}}
#bubble[review="true"] {{ border-left: 3px solid {t['warning']}; }}
#bubble[state="failed"] {{ border: 1px solid {t['error']};
                           border-left: 3px solid {t['error']}; }}
#bubble[skipped="true"] {{ background: {t['window']}; }}

#bubbleHeader {{ color: {t['text_muted']}; font-size: 11px; font-weight: 600; }}
#originalText {{ color: {t['text']}; font-size: 15px; font-weight: 600; }}
#translatedText {{ color: {t['accent']}; font-size: 15px; }}
#errorText {{ color: {t['error']}; font-size: 12px; }}
#noteText {{ color: {t['warning']}; font-size: 12px; }}
#provisional {{ color: {t['text_muted']}; font-style: italic; }}
#statusText {{ color: {t['text_muted']}; font-size: 11px; }}
#emptyState {{ color: {t['text_muted']}; padding: 48px 24px; font-size: 13px; }}
#playButton {{
    border: 1px solid {t['accent']}; color: {t['accent']}; border-radius: {r}px;
    padding: 4px 12px; font-weight: 700;
}}
#playButton:focus {{ border-width: 2px; }}

/* --- metadata chips ---------------------------------------------------- */
#chip {{
    background: {t['surface_raised']};
    border: 1px solid {t['border']};
    border-radius: {r}px;
    padding: 2px 8px;
    font-size: 11px;
    color: {t['text_muted']};
}}
#chip[tone="accent"] {{ color: {t['accent']}; border-color: {t['accent']}; }}
#chip[tone="warning"] {{ color: {t['warning']}; border-color: {t['warning']}; }}
#chip[tone="error"] {{ color: {t['error']}; border-color: {t['error']}; }}

/* --- the selected input: always visible, always legible ---------------- */
#inputStatus {{ font-size: 14px; }}

/* --- banners ----------------------------------------------------------- */
#warningBanner, #privacyBanner, #readyBanner {{
    border-radius: {r}px; padding: 9px 12px; font-size: 12px;
    border: 1px solid {t['border']}; background: {t['surface']};
}}
#warningBanner {{ color: {t['warning']}; border-color: {t['warning']}; }}
#privacyBanner {{ color: {t['accent']}; border-color: {t['accent']}; }}
#readyBanner {{ color: {t['ok']}; border-color: {t['ok']}; }}

/* --- panels: input, session options ------------------------------------ */
#setupPanel, #inputPanel {{
    background: {t['surface']}; border: 1px solid {t['border']};
    border-radius: {r}px;
}}
#sectionLabel {{ color: {t['text_muted']}; font-size: 11px; font-weight: 600; }}
QGroupBox {{ border: 1px solid {t['border']}; border-radius: {r}px;
             margin-top: 12px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                    color: {t['text_muted']}; font-weight: 700; }}

/* --- sessions: rectangular tabs, the selected one in the accent -------- */
#sessionTabs::tab {{
    background: {t['surface']}; border: 1px solid {t['border']};
    border-bottom: none; border-radius: 0; padding: 6px 12px; margin-right: 2px;
}}
#sessionTabs::tab:selected {{
    background: {t['surface_raised']}; border-top: 3px solid {t['accent']};
    font-weight: 700;
}}

/* --- menus: a solid selection bar ------------------------------------- */
QMenuBar {{ background: {t['window']}; }}
QMenuBar::item:selected, QMenu::item:selected {{
    background: {t['accent']}; color: {t['on_accent']};
}}
QMenu {{ background: {t['surface']}; border: 1px solid {t['border']}; }}
QMenu::item {{ padding: 5px 18px; }}
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
