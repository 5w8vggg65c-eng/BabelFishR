"""Select installed Whisper weights in the console's Local Engines section."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from ..model_integrity import Integrity, integrity_state
from ..providers.whisper_local import MODEL_SIZES, ModelState, inspect_model_directory


class WhisperModelPicker(QtWidgets.QComboBox):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.setAccessibleName("Whisper speech model")
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.refresh_choices()

    def refresh_choices(self) -> None:
        current = self.app.config.asr.model
        names = list(MODEL_SIZES)
        if current not in names:
            names.append(current)
        labels = {ModelState.COMPLETE: "Installed", ModelState.INCOMPLETE: "Incomplete",
                  ModelState.MISSING: "Not installed"}
        with QtCore.QSignalBlocker(self):
            self.clear()
            for name in names:
                directory = self.app.whisper_model_directory(name)
                try:
                    state, _ = inspect_model_directory(directory)
                    status, detail = labels[state], str(directory)
                    if state is ModelState.COMPLETE:
                        # From the install record only (cheap, no hashing).
                        checked, why = integrity_state(directory, name)
                        if (name == current and self.app.config.asr.model_path):
                            checked, why = Integrity.UNPINNED, "custom folder"
                        status += ", " + STATE_WORDS.get(checked, "unverified")
                        detail += f"\n{why}"
                except OSError as exc:
                    state, status, detail = None, "Unavailable", str(exc)
                label = name.replace("-", " ").capitalize()
                if name == current and self.app.config.asr.model_path:
                    label += " (custom folder)"
                self.addItem(f"{label} · {status}", name)
                item = self.model().item(self.count() - 1)
                item.setEnabled(state is ModelState.COMPLETE)
                item.setToolTip(detail)
            self.setCurrentIndex(self.findData(current))
        self.refresh_availability()

    def refresh_availability(self) -> None:
        enabled = self.app.can_change_whisper_model()
        self.setEnabled(enabled)
        self.setToolTip(
            f"{self.currentText()}\nSelect installed weights for the next "
            "Whisper transcription." if enabled else
            f"{self.currentText()}\nAvailable after monitoring and queued work finish.")

    def showPopup(self) -> None:
        # Preparation or removal outside this window can change the files.
        self.refresh_choices()
        if self.isEnabled():
            super().showPopup()


# ---- Verify checksums (local only, every mode) --------------------------------
from .download_panel import DownloadProgressPanel  # noqa: E402

#: What each cheap integrity state is called next to a model.
STATE_WORDS = {Integrity.VERIFIED: "verified", Integrity.UNVERIFIED: "unverified",
               Integrity.CHANGED: "changed since verified",
               Integrity.MISMATCH: "checksum mismatch"}


class VerifyProgressPanel(DownloadProgressPanel):
    """The install panel's look, for hashing: file k of n, Cancel throughout."""

    def __init__(self, parent=None):
        super().__init__(parent, cancellable=True)
        self.bar.setAccessibleName("Checksum verification progress")
        self.status.setAccessibleName("Checksum verification status")
        self.file.setAccessibleName("File being checked")
        self.cancel_button.setAccessibleName("Cancel the verification")

    def set_progress(self, progress) -> None:
        self.last = progress
        total = getattr(progress, "files_total", None)
        done = getattr(progress, "files_done", 0) or 0
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
            text = (f"Checking checksums (no network) — file "
                    f"{min(total, done + 1)} of {total}")
        else:
            self.bar.setRange(0, 0)
            text = "Checking checksums (no network)…"
        self.status.setText(text)
        self.file.set_full_text(getattr(progress, "file", "") or "")
        self.cancel_button.setEnabled(True)


class ChecksumVerifier(QtCore.QObject):
    """"Verify checksums…": hashes installed Whisper models against their
    pins with local files only. Unlike installing it is offered in every mode
    (Offline Field Use and Record Only included): no confirmation dialog, no
    free-space requirement, no network, not a registered transfer. Runs off
    the GUI thread with progress and Cancel, then updates the record, the
    model picker and readiness."""

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.worker = None
        self.button = QtWidgets.QToolButton()
        self.button.setText("Verify checksums…")
        self.button.setAccessibleName("Verify Whisper model checksums")
        self.button.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.menu = QtWidgets.QMenu(self.button)
        self.menu.aboutToShow.connect(self.fill_menu)
        self.button.setMenu(self.menu)
        self.panel = VerifyProgressPanel()
        self.panel.hide()
        self.panel.cancelRequested.connect(self.cancel)

    @property
    def app(self):
        return self.window.app

    def candidates(self):
        """``(name, label)`` for each installed (complete) pinned model."""
        found = []
        for name in MODEL_SIZES:
            directory = self.app.whisper_model_directory(name)
            try:
                state, _ = inspect_model_directory(directory)
            except OSError:
                continue
            if state is not ModelState.COMPLETE:
                continue
            checked, _why = integrity_state(directory, name)
            if checked is Integrity.UNPINNED:
                continue
            title = name.replace("-", " ").capitalize()
            found.append((name, f"Verify {title} ({STATE_WORDS[checked]})"))
        return found

    def problem(self) -> str:
        if self.worker is not None:
            return "Checksums are being verified."
        if getattr(self.app, "session", None) is not None:
            return "Stop monitoring first: checksums are verified between watches."
        if getattr(self.window, "_install_worker", None) is not None:
            return "A speech model is being installed."
        return ""

    def refresh(self) -> None:
        problem = self.problem()
        self.button.setEnabled(not problem)
        self.button.setToolTip(problem or (
            "Check installed Whisper models against their published "
            "checksums, using local files only (no network; works offline)."))

    def fill_menu(self) -> None:
        self.menu.clear()
        models = self.candidates()
        if not models:
            self.menu.addAction("No installed Whisper model to verify").setEnabled(False)
            return
        for name, label in models:
            action = self.menu.addAction(label)
            action.triggered.connect(lambda _c=False, n=name: self.start(n))

    def start(self, name: str) -> bool:
        from . import workers

        problem = self.problem()
        if problem:
            self.window.status.showMessage(problem, 10000)
            return False
        title = name.replace("-", " ").capitalize()
        expand = getattr(self.window, "_expand_section", None)
        if callable(expand):
            expand("engines")
        self.panel.last = None
        self.panel.start(f"Verifying {title} checksums (no network)…")
        self.worker = workers.run_in_background(
            workers.verify_model_job, self.app.config, name,
            on_finished=lambda result: self._done(title, result),
            on_failed=lambda message: self._done(title, None, error=message),
            on_cancelled=lambda: self._done(title, None, cancelled=True),
            on_message=self.panel.set_message,
            on_progress=self.panel.set_progress)
        self._changed()
        return True

    def cancel(self) -> None:
        worker = self.worker
        if worker is not None and hasattr(worker, "token"):
            worker.token.cancel()

    def _done(self, title, outcome, cancelled=False, error="") -> None:
        self.worker = None
        if cancelled:
            text, ok = (f"Verifying {title} was cancelled; its state is "
                        f"unchanged."), False
        elif error:
            text, ok = f"{title} could not be verified: {error}", False
        else:
            _step, ok, detail = outcome
            text = (f"{title} matches its published checksums (verified "
                    f"locally)." if ok else f"{title}: {detail}")
        self.panel.finish(text, ok)
        self.window.status.showMessage(text, 15000)
        self._changed()
        refresh = getattr(self.window, "_refresh_readiness", None)
        if callable(refresh):
            refresh(run_smoke_tests=False)

    def _changed(self) -> None:
        self.window.whisper_model_box.refresh_choices()
        self.window._refresh_install_model_button()   # also refreshes this
