"""Analyze-as-digital actions on a message (moved from
``babelfishr/ui/timeline.py``), added by the ``bubble_menu`` hook."""

from __future__ import annotations

from PySide6 import QtWidgets

from ..analysis.dsd import AUTO_ROTATION_SECONDS
from ..analysis.dsd import PRESETS as DSD_PRESETS


def add_analysis_actions(bubble, menu) -> None:
    bubble.analyze_action = menu.addAction(
        "Analyze as digital (hunt all profiles)",
        lambda: bubble.analyzeDigitalRequested.emit(bubble.tx.id, "auto"))
    bubble.analyze_action.setToolTip(
        f"Tries every profile in turn. A full rotation takes about "
        f"{int(AUTO_ROTATION_SECONDS)} seconds at 48 kHz, so a shorter "
        f"recording may finish before the right profile is reached - "
        f"pick a specific preset below if you know the mode.")

    protocols = QtWidgets.QMenu("Analyze as a specific mode...", menu)
    for preset in DSD_PRESETS:
        if preset.id == "auto":
            continue
        action = protocols.addAction(
            preset.label,
            lambda checked=False, name=preset.id:
            bubble.analyzeDigitalRequested.emit(bubble.tx.id, name))
        action.setToolTip(preset.describe())
    bubble.protocol_menu = menu.addMenu(protocols)
    has_audio = bool(bubble.tx.audio_path)
    bubble.analyze_action.setEnabled(has_audio)
    bubble.protocol_menu.setEnabled(has_audio)

    def _refresh() -> None:
        audio = bool(bubble.tx.audio_path)
        bubble.analyze_action.setEnabled(audio)
        bubble.protocol_menu.setEnabled(audio)

    menu.aboutToShow.connect(_refresh)
