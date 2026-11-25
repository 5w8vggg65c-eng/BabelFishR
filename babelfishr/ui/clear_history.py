"""Clear Session History: one dialog, three scopes, one deliberate confirmation.

Opened from a Session tab's right-click menu. The operator picks
Transcripts only, Audio files only or Both. The dialog lists what will be
lost and what will be kept, for the scope picked and with this Session's
real counts. Clear permanently stays disabled until the confirmation box is
ticked, and Cancel is the default button. The Session, its tab, its name and
its colour always stay.
"""

from __future__ import annotations

from typing import List

from PySide6 import QtCore, QtWidgets

SCOPES = (
    ("text", "Transcripts only"),
    ("audio", "Audio files only"),
    ("both", "Both: messages and audio"),
)
CLEAR_BUTTON = "Clear permanently"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(scope: str, inventory, name: str) -> str:
    """What *scope* deletes and keeps, as plain lines."""
    n = inventory.messages
    hidden = (f" ({inventory.hidden} of them removed from view)"
              if inventory.hidden else "")
    owned, shared, external = (len(inventory.owned), len(inventory.shared),
                               len(inventory.external))
    lost: List[str] = []
    kept: List[str] = [f"The “{name}” tab, its name and colour"]
    if scope == "text":
        lost += [f"Recognised and translated text of {_plural(inventory.with_text, 'message')}"
                 f"{hidden}",
                 f"Corrections and earlier corrections ({inventory.corrections})",
                 "Timed text segments and the search entries for that text"]
        kept += [f"All {_plural(n, 'message')} with time and signal details",
                 f"Recordings: {_plural(owned + shared + external, 'file')} stay playable",
                 f"Notes ({inventory.notes}), tags ({inventory.tags}) and "
                 f"bookmarks ({inventory.bookmarks})"]
    elif scope == "audio":
        lost += [f"{_plural(owned, 'audio file')} in BabelFishR's Recordings "
                 f"folder (original, processed and decoded audio){hidden}"]
        if inventory.missing:
            lost.append(f"References to {_plural(len(inventory.missing), 'file')} "
                        f"already missing from disk")
        kept += [f"All {_plural(n, 'message')} with transcripts, translations "
                 f"and corrections",
                 f"Notes ({inventory.notes}), tags ({inventory.tags}) and "
                 f"bookmarks ({inventory.bookmarks})"]
        if shared:
            kept.append(f"{_plural(shared, 'file')} another Session still uses "
                        f"(left on disk)")
        if external:
            kept.append(f"{_plural(external, 'file')} outside the Recordings "
                        f"folder (left on disk)")
    else:
        lost += [f"All {_plural(n, 'message')}{hidden}: transcripts, translations, "
                 f"corrections, notes ({inventory.notes}), tags and "
                 f"bookmarks ({inventory.bookmarks})",
                 f"{_plural(owned, 'audio file')} in BabelFishR's Recordings folder"]
        if shared:
            kept.append(f"{_plural(shared, 'file')} another Session still uses")
        if external:
            kept.append(f"{_plural(external, 'file')} outside the Recordings folder")
    kept += ["Every other Session", "Copies you exported or shared elsewhere"]
    lines = ["Deleted permanently:"] + [f"• {line}" for line in lost]
    lines += ["", "Kept:"] + [f"• {line}" for line in kept]
    return "\n".join(lines)


class ClearHistoryDialog(QtWidgets.QDialog):
    def __init__(self, name: str, inventory, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clear Session History")
        self.setObjectName("clearHistoryDialog")
        self.setSizeGripEnabled(True)
        self._name, self._inventory = name, inventory
        layout = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel(
            f"Clear the history of “{name}”? The Session stays and can "
            f"be used again.")
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.scope_group = QtWidgets.QButtonGroup(self)
        self.scope_buttons = {}
        for key, label in SCOPES:
            button = QtWidgets.QRadioButton(label)
            button.setProperty("scope", key)
            self.scope_group.addButton(button)
            self.scope_buttons[key] = button
            layout.addWidget(button)
        self.scope_buttons["text"].setChecked(True)
        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.summary.setAccessibleName("What will be deleted and kept")
        layout.addWidget(self.summary, 1)
        self.confirm = QtWidgets.QCheckBox(
            "I understand this is permanent and cannot be undone")
        layout.addWidget(self.confirm)
        self.buttons = QtWidgets.QDialogButtonBox()
        self.cancel_button = self.buttons.addButton(QtWidgets.QDialogButtonBox.Cancel)
        self.clear_button = self.buttons.addButton(
            CLEAR_BUTTON, QtWidgets.QDialogButtonBox.DestructiveRole)
        self.clear_button.setEnabled(False)
        self.clear_button.setAutoDefault(False)
        self.cancel_button.setDefault(True)
        self.buttons.rejected.connect(self.reject)
        self.clear_button.clicked.connect(self.accept)
        layout.addWidget(self.buttons)
        self.scope_group.buttonToggled.connect(lambda *_: self._refresh())
        self.confirm.toggled.connect(self.clear_button.setEnabled)
        self._refresh()
        self.resize(560, 440)

    def scope(self) -> str:
        checked = self.scope_group.checkedButton()
        return checked.property("scope") if checked else "text"

    def _refresh(self) -> None:
        self.summary.setText(describe(self.scope(), self._inventory, self._name))
        # A new scope is a new decision: confirm it again.
        self.confirm.setChecked(False)
