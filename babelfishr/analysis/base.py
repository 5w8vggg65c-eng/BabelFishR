"""Interface for external analysis engines that run over a saved recording."""

from __future__ import annotations

import abc
import dataclasses
import pathlib
from typing import Dict, List, Optional

from ..models import AnalysisAttempt, Transmission


@dataclasses.dataclass
class AnalysisRequest:
    """What to analyse and how."""

    transmission: Transmission
    protocol: str = ""
    """Empty means "let the engine decide"; otherwise a specific protocol."""

    options: Dict[str, object] = dataclasses.field(default_factory=dict)
    output_dir: Optional[str] = None
    timeout: float = 120.0


class AnalysisEngine(abc.ABC):
    """An external tool that inspects a recording and reports what it found.

    Analysis is always post-processing over a saved file, never a live tap, so
    a recording can be re-analysed as many times as the operator likes - with
    different protocol guesses - without needing the transmission again.
    """

    id: str = "analysis"
    name: str = "Analysis engine"

    @abc.abstractmethod
    def available(self) -> bool:
        ...

    @abc.abstractmethod
    def unavailable_reason(self) -> str:
        ...

    @abc.abstractmethod
    def version(self) -> str:
        ...

    @abc.abstractmethod
    def analyse(self, request: AnalysisRequest) -> AnalysisAttempt:
        """Run the engine. Must never raise, and never modify the original."""

    def supported_protocols(self) -> List[str]:
        return []
