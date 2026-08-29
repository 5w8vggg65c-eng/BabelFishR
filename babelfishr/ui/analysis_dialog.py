"""Explaining why digital analysis is unavailable, without dead ends."""

from __future__ import annotations

from typing import Optional

from PySide6 import QtWidgets


def show_dsd_missing(parent: Optional[QtWidgets.QWidget], app) -> None:
    """Say plainly that DSD is optional and the recording is unaffected."""
    from ..analysis import DsdNeoAnalyser

    analyser = DsdNeoAnalyser.from_config(app.config)
    QtWidgets.QMessageBox.information(
        parent, "Digital analysis unavailable",
        f"{analyser.unavailable_reason()}\n\n"
        "This is optional. Recording, transcription and translation are "
        "unaffected, and the recording is kept so it can be analysed later "
        "once DSD-neo is installed.\n\n"
        "Set its location under analysis.dsd_path in the configuration file.")


def describe_attempt(attempt) -> str:
    """Human-readable detail for one analysis run."""
    lines = [
        f"Engine:   {attempt.engine} {attempt.engine_version}",
        f"Outcome:  {attempt.outcome.label}",
    ]
    if attempt.protocol:
        lines.append(f"Protocol: {attempt.protocol}")
    if attempt.metadata:
        lines.append("Metadata: " + ", ".join(
            f"{k}={v}" for k, v in attempt.metadata.items()))
    lines.append(f"Input:    {attempt.input_path}"
                 + (" (derived copy)" if attempt.input_is_derived
                    else " (original)"))
    lines.append(f"Runtime:  {attempt.runtime_seconds:.2f}s "
                 f"(exit {attempt.exit_status})")
    if attempt.error:
        lines.append(f"Error:    {attempt.error}")
    lines.append("")
    lines.append("The original recording is unchanged.")
    return "\n".join(lines)
