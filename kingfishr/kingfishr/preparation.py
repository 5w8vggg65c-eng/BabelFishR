"""KingFishR's optional DSD-neo line in the preparation result.

Moved from ``babelfishr/preparation.py``.
"""

from __future__ import annotations


def dsd_line(config, result, say) -> None:
    from .analysis import DsdNeoAnalyser

    analyser = DsdNeoAnalyser.from_config(config)
    if analyser.available():
        result.add("DSD-neo (optional)", True,
                   f"{analyser.resolve_executable()} version {analyser.version()}")
    else:
        say("DSD-neo not found (optional; digital post-processing unavailable)")
