"""One copy-and-paste block describing what is actually installed and working.

When first-run preparation fails, the operator needs to be able to hand
somebody a complete description of their machine without opening a terminal,
finding a log directory, or knowing what any of it means. That is what this
produces: everything relevant, in plain text, from inside the application.

Nothing here reaches the network, and nothing here reads a credential.
"""

from __future__ import annotations

import datetime as _dt
import platform
import shutil
import sys
from typing import List, Optional

MAX_LOG_LINES = 120


def _section(title: str) -> List[str]:
    return ["", title, "-" * len(title)]


def diagnostic_report(config, *, extra: str = "",
                      readiness=None, include_log: bool = True) -> str:
    """A complete, self-contained description of this installation."""
    from . import __version__

    lines: List[str] = [
        "BabelFishR diagnostic report",
        f"generated : {_dt.datetime.now(_dt.timezone.utc).isoformat(timespec='seconds')}",
        f"version   : {__version__}",
    ]

    lines += _section("This computer")
    lines += [
        f"  platform      : {platform.platform()}",
        f"  machine       : {platform.machine()}",
        f"  python        : {platform.python_version()}",
        f"  frozen bundle : {bool(getattr(sys, 'frozen', False))}",
        f"  executable    : {sys.executable}",
    ]

    lines += _section("Where things are kept")
    try:
        paths = config.paths()
        for name in ("root", "models", "language_packs", "recordings", "logs"):
            value = getattr(paths, name, None)
            lines.append(f"  {name:<14}: {value}")
        lines.append(f"  {'database':<14}: {config.database}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  could not resolve paths: {exc}")
    try:
        # Separate: a directory that does not exist yet is normal on a first
        # run, and must not swallow the paths listed above.
        usage = shutil.disk_usage(str(config.paths().root))
        lines.append(f"  {'free space':<14}: {usage.free / 1e9:.1f} GB")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  {'free space':<14}: not measured ({exc.__class__.__name__})")

    lines += _section("Settings")
    lines += [
        f"  mode           : {config.mode}",
        f"  settings file  : {config.settings_path()}",
        f"  asr engine     : {config.asr.engine} / model {config.asr.model}",
        f"  translate      : {config.translate.engine} "
        f"-> {config.translate.target_language}",
        f"  setup complete : {config.setup.completed}",
    ]

    lines += _section("Audio input")
    selection = config.audio.input
    lines += [
        f"  chosen         : {selection.label or '(nothing chosen yet)'}",
        f"  confirmed      : {selection.confirmed}",
        f"  system default : {selection.use_system_default}",
        f"  identity       : {config.selected_input().describe()}",
        f"  identified by  : {config.selected_input().basis}",
    ]
    try:
        from .audio.devices import (backend_status, list_input_devices,
                                    resolve_input)

        lines.append(f"  backend        : {backend_status()}")
        connected = list_input_devices()
        lines.append(f"  connected      : {len(connected)} input(s)")
        for device in connected:
            kind = "built-in" if device.is_builtin else "external"
            lines.append(f"     [{device.index}] {device.name} "
                         f"({device.max_input_channels} ch, {kind}"
                         f"{', uid ' + device.uid if device.uid else ''})")
        resolution = resolve_input(config.selected_input())
        if resolution.resolved:
            lines.append(f"  chosen present : yes, as input "
                         f"{resolution.device.index}")
        elif resolution.ambiguous:
            lines.append(f"  chosen present : AMBIGUOUS - "
                         f"{len(resolution.candidates)} connected inputs are "
                         f"indistinguishable from it, so none was selected")
        else:
            lines.append("  chosen present : NO - it is not connected")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  could not enumerate inputs: {exc}")

    lines += _section("Installed speech models")
    try:
        from .providers.whisper_local import (inspect_model_directory,
                                              model_directory_for)

        models_root = config.paths().models
        for name in sorted({config.asr.model, "tiny", "base", "small",
                            "medium", "large-v3"}):
            directory = model_directory_for(models_root, name)
            state, missing = inspect_model_directory(directory)
            note = f", missing {', '.join(missing)}" if missing else ""
            lines.append(f"  {name:<10}: {state.value}{note}  ({directory})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  could not inspect models: {exc}")

    lines += _section("Installed translation routes")
    try:
        from .preparation import installed_routes

        routes = installed_routes(config)
        if not routes:
            lines.append("  none installed - nothing can be translated offline")
        for source, target, kind in routes:
            lines.append(f"  {source} -> {target} ({kind})")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"  could not inspect language packs: {exc}")

    if readiness is not None:
        lines += _section("Field check")
        lines += ["  " + line for line in
                  str(readiness.summary()).splitlines()]

    if extra:
        lines += _section("Setup log")
        lines += ["  " + line for line in extra.splitlines()]

    if include_log:
        lines += _section(f"Application log (last {MAX_LOG_LINES} lines)")
        lines += ["  " + line for line in _log_tail(config)]

    return "\n".join(lines) + "\n"


def _log_tail(config, limit: int = MAX_LOG_LINES) -> List[str]:
    try:
        path = config.paths().logs / "babelfishr.log"
        if not path.exists():
            return ["(no log file yet)"]
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-limit:] or ["(log is empty)"]
    except Exception as exc:  # noqa: BLE001
        return [f"(could not read the log: {exc})"]


__all__ = ["diagnostic_report"]
