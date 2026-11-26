"""Semantic, appearance-adaptive styling: the BabelFishR field console (v5).

The approved v5 concept (docs/ui-preview/README.md): a black/charcoal ground,
a teal identity/status band, turquoise-outlined selections, thin grey
rules, compact controls, and regular-weight monospaced technical type. Two
panes: Sessions, input and local engines on the left; the recording
waveform, the original/translation review list and the selected-audio
actions on the right.

The window still follows the operator's system appearance:

* **Dark appearance** - the console as approved, applied as the window's own
  palette so natively drawn controls match.
* **Light appearance** - the same structure on a light ground, for daylight
  legibility; the teal band, turquoise selection and amber review marker keep
  their meaning.

Status is carried by a word as well as a colour. No web fonts: the platform's
own monospace face (Menlo on macOS) with a system fixed-pitch fallback.
"""

from __future__ import annotations

from typing import Dict

from PySide6 import QtGui, QtWidgets

#: Turquoise, for the selection and the primary action - both appearances.
ACCENT_DARK = "#34CACD"
ACCENT_LIGHT = "#34CACD"

#: The approved v5 console (dark appearance). The band is a little deeper
#: than the concept's #338F99 so its off-white text reaches 4.7:1.
CONSOLE = {
    "window": "#0A0E11",
    "surface": "#0E1418",          # panes
    "surface_raised": "#1D282B",   # section and column header strips
    "band": "#287882",
    "on_band": "#FFFFFF",
    "border": "#2E3B40",           # thin rules
    "rule_strong": "#5E7076",      # pane divider, outlined buttons
    "text": "#E6ECEE",
    "text_muted": "#8E9BA1",
    "accent": ACCENT_DARK,
    "on_accent": "#0A0E11",
    "disabled_bg": "#303E42",
    "disabled_text": "#7A898E",
    "statusbar": "#131B1E",
    "selected_bg": "#11282C",      # a faint turquoise tint, not a fill
    # The live INPUT spectrum: orange bars, a paler cap for the held peak.
    "spectrum": "#FF9F43",         # 9.1:1 on the pane
    "spectrum_peak": "#FFD08A",
}

DAYLIGHT = {
    "window": "#F4F6F6",
    "surface": "#FFFFFF",
    "surface_raised": "#E1E7E8",
    "band": "#1F6F78",
    "on_band": "#FFFFFF",
    "border": "#C5CFD2",
    "rule_strong": "#7E8E93",
    "text": "#0A0E11",
    "text_muted": "#4B5A60",
    "accent": ACCENT_LIGHT,
    "on_accent": "#0A0E11",
    "disabled_bg": "#DDE3E4",
    "disabled_text": "#6B7A80",
    "statusbar": "#E9EDEE",
    "selected_bg": "#DCF1F2",
    "spectrum": "#B34E00",         # 4.8:1 on the window: a burnt orange
    "spectrum_peak": "#7A3500",
}

#: Status colours. Every use is paired with a text label or symbol.
STATUS_LIGHT = {
    "idle": "#4B5A60", "listening": "#1A7F37", "receiving": "#1F6F78",
    "working": "#8A5A00", "error": "#C62828", "ok": "#1A7F37",
}
STATUS_DARK = {
    "idle": "#8E9BA1", "listening": "#3FD68B", "receiving": "#34CACD",
    "working": "#EFC062", "error": "#FF6B6B", "ok": "#3FD68B",
}

#: Square, deliberate rectangles.
RADIUS = 0

#: Monospaced faces, in order of preference. Platform fonts only.
MONOSPACE = ("Menlo", "SF Mono", "Monaco", "DejaVu Sans Mono",
             "Liberation Mono", "Consolas")


# Backgrounds behind white lettering on the status band in both appearances.
BADGE_FILL = {"idle": "#196339", "listening": "#9D3039", "receiving": "#9D3039",
              "working": "#20588C", "ok": "#196339", "error": "#9D3039"}


def is_dark(widget: QtWidgets.QWidget = None) -> bool:
    """True when the operator's *system* appearance is dark.

    Read from the application palette - the system's - not from a widget's,
    because the window sets its own palette for the console and must still
    notice when the system switches back to light.
    """
    palette = QtWidgets.QApplication.palette()
    window = palette.color(QtGui.QPalette.Window)
    luminance = (0.299 * window.red() + 0.587 * window.green()
                 + 0.114 * window.blue()) / 255.0
    return luminance < 0.5


def accent(widget: QtWidgets.QWidget = None) -> str:
    return ACCENT_DARK if is_dark(widget) else ACCENT_LIGHT


def status_color(name: str, widget: QtWidgets.QWidget = None) -> str:
    table = STATUS_DARK if is_dark(widget) else STATUS_LIGHT
    return table.get(name, table["idle"])


def tokens(widget: QtWidgets.QWidget = None) -> Dict[str, str]:
    """Colour tokens for the current system appearance."""
    dark = is_dark(widget)
    base = CONSOLE if dark else DAYLIGHT
    status = STATUS_DARK if dark else STATUS_LIGHT
    return dict(base, error=status["error"], warning=status["working"],
                ok=status["ok"], dark="1" if dark else "0")


def monospace_font(point_size: int = 0) -> QtGui.QFont:
    """The platform monospace face, regular width and weight."""
    families = set(QtGui.QFontDatabase.families())
    for family in MONOSPACE:
        if family in families:
            font = QtGui.QFont(family)
            break
    else:
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
    font.setStyleHint(QtGui.QFont.Monospace)
    font.setFixedPitch(True)
    font.setWeight(QtGui.QFont.Normal)
    font.setStretch(QtGui.QFont.Unstretched)
    if point_size:
        font.setPointSize(point_size)
    return font


def palette(widget: QtWidgets.QWidget = None) -> QtGui.QPalette:
    """The window's palette: the console's in dark appearance, the daylight
    variant's in light. Both are set explicitly so native controls match."""
    t = tokens(widget)
    p = QtGui.QPalette(QtWidgets.QApplication.palette())
    c = QtGui.QColor
    for group in (QtGui.QPalette.Active, QtGui.QPalette.Inactive):
        p.setColor(group, QtGui.QPalette.Window, c(t["window"]))
        p.setColor(group, QtGui.QPalette.WindowText, c(t["text"]))
        p.setColor(group, QtGui.QPalette.Base, c(t["window"]))
        p.setColor(group, QtGui.QPalette.AlternateBase, c(t["surface"]))
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
        p.setColor(QtGui.QPalette.Disabled, role, c(t["disabled_text"]))
    p.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Base, c(t["window"]))
    p.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Button, c(t["disabled_bg"]))
    return p


def stylesheet(widget: QtWidgets.QWidget = None) -> str:
    """Application stylesheet for the current appearance.

    Regenerated when the system appearance changes. Normal weight throughout:
    hierarchy comes from strips, rules, case and colour, not bold type.
    """
    t = tokens(widget)
    badge_rules = "\n".join(
        f'#statusBand #stateBadge[tone="{tone}"] {{ background-color: {fill}; }}'
        for tone, fill in BADGE_FILL.items())
    return f"""
QWidget {{ color: {t['text']}; font-weight: normal; }}
QMainWindow, #centralRoot {{ background: {t['window']}; }}

/* --- identity row and teal status band -------------------------------- */
#titleBar {{ background: {t['window']}; }}
#appTitle, #titleNote {{ font-size: 15px; }}
#statusBand {{ background: {t['band']}; }}
#statusBand QLabel, #statusBand QToolButton, #statusBand #stateBadge,
#statusBand #sourceLabel, #statusBand #modeButton {{
    color: {t['on_band']}; font-size: 15px; font-weight: 600;
    background: transparent;
}}
#statusBand #stateBadge {{ border-radius: 10px; padding: 2px 10px; }}
{badge_rules}
#modeButton {{ border: 1px solid transparent; padding: 2px 6px; }}
#modeButton:hover, #modeButton:focus {{ border: 1px solid {t['on_band']}; }}
#modeButton:disabled {{ color: {t['on_band']}; }}
#modeButton::menu-indicator {{ image: none; width: 0; }}

/* --- navigation: the in-window menu bar -------------------------------- */
QMenuBar {{
    background: {t['window']}; border-bottom: 1px solid {t['border']};
    font-size: 14px; padding: 2px 6px;
}}
QMenuBar::item {{ padding: 6px 14px; background: transparent;
                  border-bottom: 3px solid transparent; }}
QMenuBar::item:selected, QMenuBar::item:pressed {{
    border-bottom: 3px solid {t['accent']}; background: transparent;
}}
QMenu {{ background: {t['surface']}; border: 1px solid {t['rule_strong']}; }}
QMenu::item {{ padding: 5px 18px; }}
QMenu::item:selected {{ background: {t['accent']}; color: {t['on_accent']}; }}
QMenu::item:disabled {{ color: {t['disabled_text']}; }}

/* --- panes -------------------------------------------------------------- */
#leftPane {{ background: {t['window']};
            border-right: 1px solid {t['rule_strong']}; }}
#rightPane {{ background: {t['window']}; }}
#sectionHeader, #paneHeader, #columnHeader {{
    background: {t['surface_raised']}; padding: 6px 10px; font-size: 14px;
}}
#columnHeader QLabel {{ background: transparent; font-size: 13px; }}
#rule {{ background: {t['border']}; max-height: 1px; min-height: 1px; }}
#paneLine, #engineLine {{ padding: 1px 10px; }}
#mutedLine, #sectionLabel {{ color: {t['text_muted']}; }}
#paneLink {{ border: none; text-align: left; padding: 4px 10px;
            background: transparent; }}
#paneLink:hover, #paneLink:focus {{ color: {t['accent']}; }}
#paneLink:checked {{ color: {t['accent']}; }}

/* --- sessions list ------------------------------------------------------ */
#sessionTabs {{ background: transparent; }}

/* --- recording waveform -------------------------------------------------- */
#recordingWaveform {{ border: 1px solid {t['border']}; }}

/* --- filter row ----------------------------------------------------------- */
#filterButton {{
    border: none; padding: 4px 14px; background: transparent;
}}
#filterButton:checked {{ background: {t['accent']}; color: {t['on_accent']}; }}
#filterButton:hover:!checked, #filterButton:focus:!checked {{ color: {t['accent']}; }}
#searchField {{
    border: 1px solid {t['rule_strong']}; padding: 4px 8px;
    background: {t['window']}; selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
#searchField:focus {{ border: 1px solid {t['accent']}; }}

/* --- message rows ---------------------------------------------------------- */
#bubble {{
    background: {t['window']}; border: none;
    border-top: 2px solid transparent; border-right: 2px solid transparent;
    border-bottom: 1px solid {t['border']};
    border-left: 4px solid transparent;
}}
#bubble:focus {{ border-bottom: 1px solid {t['accent']}; }}
/* Selection: a turquoise outline and a faint tint - never a solid fill, so
   the text keeps its normal colour. Declared before the review and failed
   edges so that, on a selected row, the amber or red left edge still shows. */
#bubble[selected="true"] {{
    background: {t['selected_bg']};
    border-top: 2px solid {t['accent']}; border-right: 2px solid {t['accent']};
    border-bottom: 2px solid {t['accent']}; border-left: 4px solid {t['accent']};
}}
#bubble[review="true"] {{ border-left: 4px solid {t['warning']}; }}
#bubble[state="failed"] {{ border-left: 4px solid {t['error']}; }}
#bubble[skipped="true"] #originalText {{ color: {t['text_muted']}; }}

/* The transcript and translation boxes: thin rules, a small caption. */
#transcriptBox, #translationBox {{
    background: {t['surface']}; border: 1px solid {t['border']};
}}
#transcriptBox[held="held"] {{ border-left: 3px solid {t['warning']}; }}
#boxCaption {{ color: {t['text_muted']}; font-size: 11px;
              background: transparent; }}
#transcriptBox[held="held"] #boxCaption,
#transcriptBox[held="check"] #boxCaption {{ color: {t['warning']}; }}

#timeCell {{ font-size: 14px; }}
#bubbleHeader {{ color: {t['text_muted']}; font-size: 11px; }}
#originalText {{ color: {t['text']}; font-size: 14px; background: transparent; }}
#translatedText {{ color: {t['text']}; font-size: 14px; background: transparent; }}
#errorText {{ color: {t['error']}; font-size: 12px; }}
#noteText {{ color: {t['warning']}; font-size: 12px; }}
#provisional {{ color: {t['text_muted']}; font-style: italic; }}
#statusText {{ color: {t['text_muted']}; font-size: 11px; }}
/* The card's one audio + transcript status: quiet when all is well, the
   warning colour (with its own words, never colour alone) when not. */
#pairingStatus {{ color: {t['text_muted']}; font-size: 11px; }}
#pairingStatus[ok="false"] {{ color: {t['warning']}; }}
/* The card continuous playback is on. */
#bubble[nowPlaying="true"] {{ border-right: 4px solid {t['accent']}; }}
#emptyState {{ color: {t['text_muted']}; padding: 48px 24px; font-size: 13px; }}
#reviewMark {{ padding: 2px 8px; font-size: 13px; }}
#reviewMark[tone="check"] {{ color: {t['warning']}; background: {t['surface_raised']}; }}
#reviewMark[tone="failed"] {{ color: {t['error']}; }}
#playButton {{
    border: none; color: {t['text_muted']}; padding: 2px 0; background: transparent;
    font-size: 13px; text-align: left;
}}
#bubble QToolButton {{ padding: 1px 6px; }}
#bubble QToolButton::menu-indicator {{ image: none; width: 0; }}
#bubble QToolButton#menuButton {{ border: none; font-size: 16px; }}
#playButton:hover, #playButton:focus {{ color: {t['accent']}; }}
#playButton:disabled {{ color: {t['disabled_text']}; }}

/* --- chips ------------------------------------------------------------------ */
#chip {{
    border: 1px solid {t['border']}; padding: 1px 6px; font-size: 11px;
    color: {t['text_muted']};
}}
#chip[tone="accent"] {{ color: {t['accent']}; border-color: {t['accent']}; }}
#chip[tone="warning"] {{ color: {t['warning']}; border-color: {t['warning']}; }}
#chip[tone="error"] {{ color: {t['error']}; border-color: {t['error']}; }}

/* --- selected audio and the action row ------------------------------------ */
#selectedAudio {{ border-top: 1px solid {t['border']}; }}
#selectedTitle {{ font-size: 14px; }}
#primaryButton, #primaryAction {{
    background: {t['accent']}; color: {t['on_accent']}; border: none;
    padding: 7px 22px; font-size: 14px;
}}
#primaryButton:disabled, #primaryAction:disabled, #secondaryAction:disabled {{
    background: {t['disabled_bg']}; color: {t['disabled_text']}; border: none;
}}
#primaryButton:focus, #primaryAction:focus {{ border: 2px solid {t['text']}; }}
#secondaryAction {{
    background: transparent; border: 1px solid {t['rule_strong']};
    padding: 6px 22px; font-size: 14px;
}}
#secondaryAction:hover, #secondaryAction:focus {{ border: 1px solid {t['accent']}; }}
#secondaryAction:checked {{ border: 1px solid {t['accent']}; color: {t['accent']}; }}
#textAction {{ border: none; background: transparent; padding: 7px 16px;
              font-size: 14px; }}
#textAction:hover, #textAction:focus {{ color: {t['accent']}; }}
#textAction:checked {{ color: {t['accent']}; }}
#textAction:disabled {{ color: {t['disabled_text']}; }}
#actionRow {{ border-top: 1px solid {t['border']}; }}

/* --- banners and panels ------------------------------------------------------- */
#warningBanner, #privacyBanner, #readyBanner {{
    padding: 6px 10px; font-size: 12px; border: 1px solid {t['border']};
}}
#warningBanner {{ color: {t['warning']}; border-color: {t['warning']}; }}
#privacyBanner {{ color: {t['text_muted']}; }}
#readyBanner {{ color: {t['ok']}; border-color: {t['ok']}; }}
#readinessLink {{ border: none; text-align: left; padding: 1px 10px;
                 background: transparent; }}
#readinessLink:hover, #readinessLink:focus {{ text-decoration: underline; }}
#setupPanel, #inputPanel {{ border: 1px solid {t['border']}; }}
QGroupBox {{ border: 1px solid {t['border']}; margin-top: 12px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                    color: {t['text_muted']}; }}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {{
    border: 1px solid {t['rule_strong']}; padding: 3px 6px;
    background: {t['window']};
}}
QComboBox:focus, QLineEdit:focus {{ border: 1px solid {t['accent']}; }}
QComboBox QAbstractItemView {{
    background: {t['surface']}; selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
QPushButton, QToolButton {{
    border: 1px solid {t['rule_strong']}; padding: 3px 10px; background: transparent;
}}
QPushButton:hover, QToolButton:hover {{ border: 1px solid {t['accent']}; }}
QPushButton:disabled, QToolButton:disabled {{ color: {t['disabled_text']};
                                             border-color: {t['border']}; }}
QToolTip {{ background: {t['surface_raised']}; color: {t['text']};
           border: 1px solid {t['rule_strong']}; }}

/* --- footer status band ---------------------------------------------------- */
QStatusBar {{
    background: {t['statusbar']}; border-top: 1px solid {t['rule_strong']};
    color: {t['text']}; font-size: 13px;
}}
QStatusBar QLabel, #countsLine {{ color: {t['text']}; padding: 2px 8px;
                                font-size: 13px; }}
"""


def apply(app: QtWidgets.QApplication) -> None:
    """The platform monospace face at a readable size; Fusion for control."""
    app.setStyle("Fusion")
    base = app.font()
    size = base.pointSize() if base.pointSize() >= 12 else 13
    app.setFont(monospace_font(size))
